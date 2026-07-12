"""Deterministic paper broker shared by live shadow mode and offline backtests."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


@dataclass(frozen=True)
class BrokerConfig:
    """Execution assumptions. Rates are decimal fractions, not percentages."""

    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    slippage_bps: float = 2.0
    lot_size: int = 100
    participation_rate: float = 0.10
    allow_t0: bool = False

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        for field in (
            "commission_rate",
            "minimum_commission",
            "stamp_duty_rate",
            "transfer_fee_rate",
            "slippage_bps",
        ):
            if getattr(self, field) < 0:
                raise ValueError(f"{field} cannot be negative")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if not 0 < self.participation_rate <= 1:
            raise ValueError("participation_rate must be in (0, 1]")


@dataclass(frozen=True)
class Fill:
    fill_id: int
    timestamp_ms: int
    stock_code: str
    side: str
    quantity: int
    price: float
    gross_amount: float
    commission: float
    stamp_duty: float
    transfer_fee: float
    reason: str = ""
    signal_id: str = ""

    @property
    def fees(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee


def _book_value(tick: Mapping[str, Any], field: str, index: int = 0) -> float:
    values = tick.get(field)
    if isinstance(values, (list, tuple)) and len(values) > index:
        try:
            return float(values[index] or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(values or 0)
    except (TypeError, ValueError):
        return 0.0


def _event_time_ms(tick: Mapping[str, Any] | None) -> int:
    if tick:
        try:
            value = int(tick.get("time", 0) or 0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return int(datetime.now(CHINA_TZ).timestamp() * 1000)


class PaperBroker:
    """Cash/position ledger with A-share lots, T+1 and realistic transaction fees.

    State is checkpointed to SQLite after every fill. A crash/restart therefore
    cannot silently reset shadow P&L or create duplicate fills when signal_id is
    stable.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        config: BrokerConfig | None = None,
        *,
        database_path: str | os.PathLike[str] | None = None,
        account_id: str = "paper",
        reset: bool = False,
    ) -> None:
        self.config = config or BrokerConfig()
        self.account_id = account_id
        self.database_path = Path(database_path).expanduser().resolve() if database_path else None
        self._cash = float(self.config.initial_cash)
        self._realized_pnl = 0.0
        self._positions: dict[str, dict[str, Any]] = {}
        self._marks: dict[str, float] = {}
        self._fills: list[Fill] = []
        self._fill_sequence = 0
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

        if self.database_path is not None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self.database_path, timeout=30, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._create_schema()
            if reset:
                self._reset_database()
            self._load_state()

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    @property
    def positions(self) -> dict[str, dict[str, Any]]:
        return {code: dict(position) for code, position in self._positions.items()}

    def _create_schema(self) -> None:
        assert self._connection is not None
        self._connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                cash REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                account_id TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                available_quantity INTEGER NOT NULL,
                average_cost REAL NOT NULL,
                total_cost REAL NOT NULL,
                opened_date TEXT NOT NULL,
                mark_price REAL NOT NULL,
                PRIMARY KEY (account_id, stock_code)
            );
            CREATE TABLE IF NOT EXISTS fills (
                account_id TEXT NOT NULL,
                fill_id INTEGER NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                gross_amount REAL NOT NULL,
                commission REAL NOT NULL,
                stamp_duty REAL NOT NULL,
                transfer_fee REAL NOT NULL,
                reason TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                PRIMARY KEY (account_id, fill_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS fills_signal_id
            ON fills (account_id, signal_id) WHERE signal_id <> '';
            CREATE TABLE IF NOT EXISTS equity (
                account_id TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                cash REAL NOT NULL,
                market_value REAL NOT NULL,
                equity REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                PRIMARY KEY (account_id, timestamp_ms)
            );
            """
        )
        self._connection.commit()

    def _reset_database(self) -> None:
        assert self._connection is not None
        for table in ("positions", "fills", "equity", "accounts"):
            self._connection.execute(
                f"DELETE FROM {table} WHERE account_id = ?", (self.account_id,)
            )
        self._connection.commit()

    def _load_state(self) -> None:
        if self._connection is None:
            return
        account = self._connection.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (self.account_id,)
        ).fetchone()
        if account is None:
            self._persist()
            return
        if int(account["schema_version"]) != self.SCHEMA_VERSION:
            raise RuntimeError("unsupported paper broker database schema")
        stored_config = BrokerConfig(**json.loads(account["config_json"]))
        if stored_config != self.config:
            raise ValueError(
                "paper account already exists with different execution assumptions; "
                "use a new account_id or reset it explicitly"
            )
        self._cash = float(account["cash"])
        self._realized_pnl = float(account["realized_pnl"])
        for row in self._connection.execute(
            "SELECT * FROM positions WHERE account_id = ?", (self.account_id,)
        ):
            code = str(row["stock_code"])
            self._positions[code] = {
                "quantity": int(row["quantity"]),
                "available_quantity": int(row["available_quantity"]),
                "average_cost": float(row["average_cost"]),
                "total_cost": float(row["total_cost"]),
                "opened_date": str(row["opened_date"]),
            }
            self._marks[code] = float(row["mark_price"])
        for row in self._connection.execute(
            "SELECT * FROM fills WHERE account_id = ? ORDER BY fill_id",
            (self.account_id,),
        ):
            values = dict(row)
            values.pop("account_id")
            self._fills.append(Fill(**values))
        self._fill_sequence = self._fills[-1].fill_id if self._fills else 0

    def _persist(self, timestamp_ms: int | None = None) -> None:
        if self._connection is None:
            return
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO accounts (
                    account_id, schema_version, config_json, cash, realized_pnl, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    config_json=excluded.config_json,
                    cash=excluded.cash,
                    realized_pnl=excluded.realized_pnl,
                    updated_at=excluded.updated_at
                """,
                (
                    self.account_id,
                    self.SCHEMA_VERSION,
                    json.dumps(asdict(self.config), sort_keys=True),
                    self._cash,
                    self._realized_pnl,
                    datetime.now(CHINA_TZ).isoformat(),
                ),
            )
            self._connection.execute(
                "DELETE FROM positions WHERE account_id = ?", (self.account_id,)
            )
            for code, position in self._positions.items():
                self._connection.execute(
                    """
                    INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.account_id,
                        code,
                        position["quantity"],
                        position["available_quantity"],
                        position["average_cost"],
                        position["total_cost"],
                        position["opened_date"],
                        self._marks.get(code, position["average_cost"]),
                    ),
                )
            if self._fills:
                fill = self._fills[-1]
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (self.account_id, *asdict(fill).values()),
                )
            if timestamp_ms is not None:
                snapshot = self.snapshot(timestamp_ms)
                self._connection.execute(
                    """
                    INSERT INTO equity VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, timestamp_ms) DO UPDATE SET
                        cash=excluded.cash, market_value=excluded.market_value,
                        equity=excluded.equity, realized_pnl=excluded.realized_pnl,
                        unrealized_pnl=excluded.unrealized_pnl
                    """,
                    (
                        self.account_id,
                        timestamp_ms,
                        snapshot["cash"],
                        snapshot["market_value"],
                        snapshot["equity"],
                        snapshot["realized_pnl"],
                        snapshot["unrealized_pnl"],
                    ),
                )

    def close(self) -> None:
        if self._connection is not None:
            self._persist()
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "PaperBroker":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _commission(self, amount: float) -> float:
        return max(self.config.minimum_commission, amount * self.config.commission_rate)

    def _date_key(self, timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, CHINA_TZ).strftime("%Y%m%d")

    def roll_trading_day(self, timestamp_ms: int) -> None:
        """Make prior-day buys sellable (A-share T+1)."""
        if self.config.allow_t0:
            for position in self._positions.values():
                position["available_quantity"] = position["quantity"]
            return
        today = self._date_key(timestamp_ms)
        for position in self._positions.values():
            if position["opened_date"] < today:
                position["available_quantity"] = position["quantity"]

    def mark(self, stock_code: str, tick: Mapping[str, Any]) -> None:
        """Update last mark and T+1 availability from a market tick."""
        timestamp_ms = _event_time_ms(tick)
        self.roll_trading_day(timestamp_ms)
        price = float(tick.get("lastPrice", 0) or 0)
        if price > 0:
            self._marks[stock_code] = price

    def mark_many(self, ticks: Mapping[str, Mapping[str, Any]]) -> None:
        with self._lock:
            for stock_code, tick in ticks.items():
                self.mark(stock_code, tick)

    def _book_levels(
        self,
        side: str,
        tick: Mapping[str, Any],
        limit_price: float | None,
    ) -> list[tuple[float, int]]:
        price_field = "askPrice" if side == "BUY" else "bidPrice"
        volume_field = "askVol" if side == "BUY" else "bidVol"
        prices = tick.get(price_field) or []
        volumes = tick.get(volume_field) or []
        if not isinstance(prices, (list, tuple)):
            prices = [prices]
        if not isinstance(volumes, (list, tuple)):
            volumes = [volumes]
        levels: list[tuple[float, int]] = []
        for raw_price, raw_lots in zip(prices, volumes):
            try:
                price = float(raw_price or 0)
                shares = int(max(0.0, float(raw_lots or 0))) * self.config.lot_size
            except (TypeError, ValueError, OverflowError):
                continue
            if price <= 0 or shares <= 0:
                continue
            if limit_price is not None and limit_price > 0:
                if side == "BUY" and price > limit_price:
                    continue
                if side == "SELL" and price < limit_price:
                    continue
            levels.append((price, shares))
        return levels

    def _fill_price(
        self,
        side: str,
        tick: Mapping[str, Any],
        limit_price: float | None,
        quantity: int,
        *,
        respect_liquidity: bool,
    ) -> float:
        levels = self._book_levels(side, tick, limit_price)
        remaining = quantity
        notional = 0.0
        matched = 0
        for level_price, level_quantity in levels:
            take = min(remaining, level_quantity)
            notional += level_price * take
            matched += take
            remaining -= take
            if remaining <= 0:
                break
        if respect_liquidity and matched < quantity:
            raise ValueError("visible order book cannot fill requested quantity")
        if matched:
            reference = notional / matched
        else:
            reference = float(tick.get("lastPrice", 0) or 0)
            if limit_price is not None and limit_price > 0:
                if side == "BUY" and reference > limit_price:
                    raise ValueError("buy limit price is below executable market price")
                if side == "SELL" and reference < limit_price:
                    raise ValueError("sell limit price is above executable market price")
        if reference <= 0:
            raise ValueError("tick has no executable price")
        slippage = self.config.slippage_bps / 10_000
        price = reference * (1 + slippage if side == "BUY" else 1 - slippage)
        if limit_price is not None and limit_price > 0:
            # A limit order cannot pay above / sell below its own limit even when a
            # conservative slippage assumption is enabled.
            price = min(price, limit_price) if side == "BUY" else max(price, limit_price)
        return round(price, 3)

    def _liquidity_cap(
        self, side: str, tick: Mapping[str, Any], limit_price: float | None
    ) -> int:
        visible_shares = sum(
            quantity for _, quantity in self._book_levels(side, tick, limit_price)
        )
        cap = int(visible_shares * self.config.participation_rate)
        return cap // self.config.lot_size * self.config.lot_size

    def buy(
        self,
        stock_code: str,
        quantity: int,
        tick: Mapping[str, Any],
        *,
        limit_price: float | None = None,
        reason: str = "",
        signal_id: str = "",
        respect_liquidity: bool = True,
    ) -> Fill | None:
        with self._lock:
            if signal_id and any(fill.signal_id == signal_id for fill in self._fills):
                return None
            self.mark(stock_code, tick)
            quantity = max(0, int(quantity) // self.config.lot_size * self.config.lot_size)
            if respect_liquidity:
                quantity = min(
                    quantity, self._liquidity_cap("BUY", tick, limit_price)
                )
            if quantity <= 0:
                return None
            price = self._fill_price(
                "BUY",
                tick,
                limit_price,
                quantity,
                respect_liquidity=respect_liquidity,
            )
            while quantity > 0:
                gross = price * quantity
                commission = self._commission(gross)
                transfer_fee = gross * self.config.transfer_fee_rate
                if gross + commission + transfer_fee <= self._cash + 1e-8:
                    break
                quantity -= self.config.lot_size
            if quantity <= 0:
                return None
            timestamp_ms = _event_time_ms(tick)
            gross = price * quantity
            commission = self._commission(gross)
            transfer_fee = gross * self.config.transfer_fee_rate
            self._cash -= gross + commission + transfer_fee
            position = self._positions.setdefault(
                stock_code,
                {
                    "quantity": 0,
                    "available_quantity": 0,
                    "average_cost": 0.0,
                    "total_cost": 0.0,
                    "opened_date": self._date_key(timestamp_ms),
                },
            )
            position["quantity"] += quantity
            position["total_cost"] += gross + commission + transfer_fee
            position["average_cost"] = position["total_cost"] / position["quantity"]
            position["opened_date"] = max(
                str(position["opened_date"]), self._date_key(timestamp_ms)
            )
            if self.config.allow_t0:
                position["available_quantity"] += quantity
            self._fill_sequence += 1
            fill = Fill(
                self._fill_sequence,
                timestamp_ms,
                stock_code,
                "BUY",
                quantity,
                price,
                gross,
                commission,
                0.0,
                transfer_fee,
                reason,
                signal_id,
            )
            self._fills.append(fill)
            self._persist(timestamp_ms)
            return fill

    def sell(
        self,
        stock_code: str,
        quantity: int,
        tick: Mapping[str, Any],
        *,
        limit_price: float | None = None,
        reason: str = "",
        signal_id: str = "",
        respect_liquidity: bool = True,
    ) -> Fill | None:
        with self._lock:
            if signal_id and any(fill.signal_id == signal_id for fill in self._fills):
                return None
            self.mark(stock_code, tick)
            position = self._positions.get(stock_code)
            if not position:
                return None
            quantity = max(0, int(quantity) // self.config.lot_size * self.config.lot_size)
            quantity = min(quantity, int(position["available_quantity"]))
            if respect_liquidity:
                quantity = min(
                    quantity, self._liquidity_cap("SELL", tick, limit_price)
                )
            if quantity <= 0:
                return None
            price = self._fill_price(
                "SELL",
                tick,
                limit_price,
                quantity,
                respect_liquidity=respect_liquidity,
            )
            timestamp_ms = _event_time_ms(tick)
            gross = price * quantity
            commission = self._commission(gross)
            stamp_duty = gross * self.config.stamp_duty_rate
            transfer_fee = gross * self.config.transfer_fee_rate
            unit_cost = position["total_cost"] / position["quantity"]
            cost_released = unit_cost * quantity
            self._cash += gross - commission - stamp_duty - transfer_fee
            self._realized_pnl += (
                gross - commission - stamp_duty - transfer_fee - cost_released
            )
            position["quantity"] -= quantity
            position["available_quantity"] -= quantity
            position["total_cost"] -= cost_released
            if position["quantity"] <= 0:
                self._positions.pop(stock_code, None)
            else:
                position["average_cost"] = position["total_cost"] / position["quantity"]
            self._fill_sequence += 1
            fill = Fill(
                self._fill_sequence,
                timestamp_ms,
                stock_code,
                "SELL",
                quantity,
                price,
                gross,
                commission,
                stamp_duty,
                transfer_fee,
                reason,
                signal_id,
            )
            self._fills.append(fill)
            self._persist(timestamp_ms)
            return fill

    def snapshot(self, timestamp_ms: int | None = None) -> dict[str, float | int]:
        market_value = sum(
            position["quantity"] * self._marks.get(code, position["average_cost"])
            for code, position in self._positions.items()
        )
        cost_basis = sum(position["total_cost"] for position in self._positions.values())
        equity = self._cash + market_value
        return {
            "timestamp_ms": int(timestamp_ms or 0),
            "cash": round(self._cash, 4),
            "market_value": round(market_value, 4),
            "equity": round(equity, 4),
            "realized_pnl": round(self._realized_pnl, 4),
            "unrealized_pnl": round(market_value - cost_basis, 4),
            "total_return": round(equity / self.config.initial_cash - 1, 8),
            "position_count": len(self._positions),
        }

    def restore_position(
        self,
        stock_code: str,
        quantity: int,
        average_cost: float,
        *,
        available_quantity: int | None = None,
        mark_price: float | None = None,
        opened_date: str = "19700101",
        debit_cash: bool = True,
    ) -> None:
        """Seed a paper account from an existing holding without fabricating a fill.

        This is intended for a paper account restored from a prior holding view.
        By default the position cost is deducted from cash so account equity is not
        silently inflated by adding holdings on top of configured initial capital.
        """
        quantity = int(quantity)
        if quantity <= 0 or average_cost <= 0:
            return
        available = quantity if available_quantity is None else int(available_quantity)
        with self._lock:
            total_cost = float(average_cost) * quantity
            if debit_cash and total_cost > self._cash + 1e-8:
                raise ValueError("restored position cost exceeds paper cash")
            if debit_cash:
                self._cash -= total_cost
            self._positions[stock_code] = {
                "quantity": quantity,
                "available_quantity": max(0, min(quantity, available)),
                "average_cost": float(average_cost),
                "total_cost": total_cost,
                "opened_date": opened_date,
            }
            self._marks[stock_code] = float(mark_price or average_cost)
            self._persist()

    def checkpoint_equity(self, timestamp_ms: int) -> dict[str, float | int]:
        with self._lock:
            snapshot = self.snapshot(timestamp_ms)
            self._persist(timestamp_ms)
            return snapshot

    def to_shared_positions(self) -> dict[str, str]:
        """Return the legacy JSON position view consumed by strategy decisions."""
        result: dict[str, str] = {}
        for code, position in self._positions.items():
            mark = self._marks.get(code, position["average_cost"])
            result[code] = json.dumps(
                {
                    "证券代码": code,
                    "持仓数量": position["quantity"],
                    "可用数量": position["available_quantity"],
                    "开仓价": position["average_cost"],
                    "市值": position["quantity"] * mark,
                    "冻结数量": position["quantity"] - position["available_quantity"],
                    "在途股份": 0,
                    "昨夜拥股": position["available_quantity"],
                    "成本价": position["average_cost"],
                },
                ensure_ascii=False,
            )
        return result

    def equity_curve(self) -> list[dict[str, float | int]]:
        if self._connection is None:
            return []
        return [
            dict(row)
            for row in self._connection.execute(
                """
                SELECT timestamp_ms, cash, market_value, equity, realized_pnl,
                       unrealized_pnl
                FROM equity WHERE account_id = ? ORDER BY timestamp_ms
                """,
                (self.account_id,),
            )
        ]

    def closed_trades(self) -> list[dict[str, Any]]:
        """FIFO match fills for win-rate and trade-level return statistics."""
        lots: dict[str, list[dict[str, Any]]] = {}
        trades: list[dict[str, Any]] = []
        for fill in self._fills:
            if fill.side == "BUY":
                lots.setdefault(fill.stock_code, []).append(
                    {
                        "quantity": fill.quantity,
                        "unit_cost": (
                            fill.gross_amount + fill.commission + fill.transfer_fee
                        )
                        / fill.quantity,
                        "entry_time_ms": fill.timestamp_ms,
                    }
                )
                continue
            remaining = fill.quantity
            sell_unit_net = (
                fill.gross_amount
                - fill.commission
                - fill.stamp_duty
                - fill.transfer_fee
            ) / fill.quantity
            queue = lots.setdefault(fill.stock_code, [])
            while remaining and queue:
                lot = queue[0]
                matched = min(remaining, lot["quantity"])
                cost = lot["unit_cost"] * matched
                proceeds = sell_unit_net * matched
                pnl = proceeds - cost
                trades.append(
                    {
                        "stock_code": fill.stock_code,
                        "entry_time_ms": lot["entry_time_ms"],
                        "exit_time_ms": fill.timestamp_ms,
                        "quantity": matched,
                        "entry_price": lot["unit_cost"],
                        "exit_price": sell_unit_net,
                        "pnl": pnl,
                        "return": pnl / cost if cost else 0.0,
                    }
                )
                remaining -= matched
                lot["quantity"] -= matched
                if lot["quantity"] == 0:
                    queue.pop(0)
        return trades
