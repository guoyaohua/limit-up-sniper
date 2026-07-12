"""Event-driven Tick replay and performance reporting.

The engine intentionally knows nothing about a particular alpha model. A strategy
receives each historical Tick batch and returns normalized BUY/SELL signals. This
keeps execution assumptions identical between research and live shadow accounts.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Callable, Iterable, Mapping, Protocol

from data.tick_archive import (
    TickBatch,
    discover_tick_files,
    iter_tick_batches,
    verify_tick_archive,
)
from engine.paper_broker import BrokerConfig, Fill, PaperBroker


@dataclass(frozen=True)
class BacktestSignal:
    stock_code: str
    side: str
    quantity: int | None = None
    target_value: float | None = None
    target_remaining: int | None = None
    limit_price: float | None = None
    reason: str = ""
    signal_id: str = ""
    limit_up_entry: bool = False

    def __post_init__(self) -> None:
        if self.side.upper() not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")


class ReplayStrategy(Protocol):
    def on_tick_batch(
        self, batch: TickBatch, broker: PaperBroker
    ) -> Iterable[BacktestSignal]: ...


@dataclass(frozen=True)
class BacktestConfig:
    sample_equity_every_batches: int = 100
    close_positions_at_end: bool = False
    fail_on_missing_tick: bool = True
    validate_archives: bool = True
    execute_on_next_tick: bool = True

    def __post_init__(self) -> None:
        if self.sample_equity_every_batches <= 0:
            raise ValueError("sample_equity_every_batches must be positive")


def _maximum_drawdown(values: list[float]) -> float:
    peak = 0.0
    maximum = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            maximum = max(maximum, (peak - value) / peak)
    return maximum


def _is_limit_up_tick(tick: Mapping[str, Any]) -> bool:
    """Infer a sealed board from the quote without future information.

    At a sealed limit-up, best ask is empty and the best bid equals last price.
    The strategy can optionally provide ``limitUpPrice``/``upperLimitPrice`` in
    archived ticks for an exact price check.  Otherwise the empty-ask condition
    keeps this metric conservative and separate from ordinary successful trades.
    """
    last_price = float(tick.get("lastPrice", 0) or 0)
    bid_price = float(_first_book_value(tick.get("bidPrice")) or 0)
    ask_price = float(_first_book_value(tick.get("askPrice")) or 0)
    limit_price = float(
        tick.get("limitUpPrice", tick.get("upperLimitPrice", 0)) or 0
    )
    if last_price <= 0 or bid_price <= 0 or ask_price > 0:
        return False
    if abs(bid_price - last_price) > 0.001:
        return False
    return limit_price <= 0 or abs(bid_price - limit_price) <= 0.001


def _first_book_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else 0
    return value


def calculate_performance(
    broker: PaperBroker,
    equity_curve: list[dict[str, Any]],
    *,
    total_batches: int = 0,
) -> dict[str, Any]:
    """Calculate fee-inclusive metrics without assuming a fixed bar interval."""
    final_snapshot = broker.snapshot(
        int(equity_curve[-1]["timestamp_ms"]) if equity_curve else None
    )
    closed = broker.closed_trades()
    winners = [trade for trade in closed if trade["pnl"] > 0]
    losers = [trade for trade in closed if trade["pnl"] < 0]
    gross_profit = sum(trade["pnl"] for trade in winners)
    gross_loss = -sum(trade["pnl"] for trade in losers)
    equities = [broker.config.initial_cash]
    equities.extend(float(point["equity"]) for point in equity_curve)
    if equities[-1] != float(final_snapshot["equity"]):
        equities.append(float(final_snapshot["equity"]))
    period_returns = [
        current / previous - 1
        for previous, current in zip(equities, equities[1:])
        if previous > 0
    ]
    volatility = pstdev(period_returns) if len(period_returns) >= 2 else 0.0
    sharpe_per_observation = (
        mean(period_returns) / volatility if volatility > 0 else 0.0
    )
    fees = sum(fill.fees for fill in broker.fills)
    buy_codes = {fill.stock_code for fill in broker.fills if fill.side == "BUY"}
    limit_up_stats = getattr(broker, "_backtest_limit_up_stats", {})
    entries = list(limit_up_stats.get("entries", ()))
    limit_up_entries = len(entries)
    sealed_at_least_once = sum(bool(entry["ever_sealed"]) for entry in entries)
    sealed_through_close = sum(bool(entry["sealed_at_close"]) for entry in entries)
    broken_after_seal = sum(bool(entry["broken_after_seal"]) for entry in entries)
    return {
        "initial_cash": broker.config.initial_cash,
        "final_equity": final_snapshot["equity"],
        "total_return": final_snapshot["total_return"],
        "realized_pnl": final_snapshot["realized_pnl"],
        "unrealized_pnl": final_snapshot["unrealized_pnl"],
        "maximum_drawdown": round(_maximum_drawdown(equities), 8),
        "closed_trade_count": len(closed),
        "win_count": len(winners),
        "loss_count": len(losers),
        "win_rate": round(len(winners) / len(closed), 8) if closed else 0.0,
        "average_trade_return": round(
            mean(trade["return"] for trade in closed), 8
        )
        if closed
        else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 8)
        if gross_loss > 0
        else None,
        "sharpe_per_observation": round(sharpe_per_observation, 8),
        "fees_paid": round(fees, 4),
        "fill_count": len(broker.fills),
        "open_position_count": len(broker.positions),
        "tick_batch_count": total_batches,
        "bought_stock_count": len(buy_codes),
        "limit_up_entry_count": limit_up_entries,
        "sealed_at_least_once_count": sealed_at_least_once,
        "sealed_through_close_count": sealed_through_close,
        "broken_after_seal_count": broken_after_seal,
        "seal_success_rate": (
            round(sealed_through_close / limit_up_entries, 8)
            if limit_up_entries
            else None
        ),
    }


class BacktestEngine:
    def __init__(
        self,
        strategy: ReplayStrategy | Callable[[TickBatch, PaperBroker], Iterable[BacktestSignal]],
        *,
        broker_config: BrokerConfig | None = None,
        config: BacktestConfig | None = None,
        database_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.strategy = strategy
        self.broker_config = broker_config or BrokerConfig()
        self.config = config or BacktestConfig()
        self.database_path = database_path

    def _signals(
        self, batch: TickBatch, broker: PaperBroker
    ) -> Iterable[BacktestSignal]:
        method = getattr(self.strategy, "on_tick_batch", self.strategy)
        return method(batch, broker) or ()

    def _quantity(
        self,
        signal: BacktestSignal,
        broker: PaperBroker,
        tick: Mapping[str, Any],
    ) -> int:
        if signal.quantity is not None:
            return int(signal.quantity)
        if signal.side.upper() == "SELL" and signal.target_remaining is not None:
            position = broker.positions.get(signal.stock_code, {})
            return max(0, int(position.get("quantity", 0)) - signal.target_remaining)
        if signal.target_value is not None:
            price = float(tick.get("lastPrice", 0) or 0)
            return int(signal.target_value / price) if price > 0 else 0
        if signal.side.upper() == "SELL":
            return int(
                broker.positions.get(signal.stock_code, {}).get(
                    "available_quantity", 0
                )
            )
        return 0

    def _execute(
        self, signal: BacktestSignal, batch: TickBatch, broker: PaperBroker
    ) -> Fill | None:
        tick = batch.ticks.get(signal.stock_code)
        if tick is None:
            if self.config.fail_on_missing_tick:
                raise KeyError(
                    f"signal for {signal.stock_code} has no Tick in batch {batch.batch_id}"
                )
            return None
        quantity = self._quantity(signal, broker, tick)
        kwargs = {
            "limit_price": signal.limit_price,
            "reason": signal.reason,
            "signal_id": signal.signal_id,
        }
        if signal.side.upper() == "BUY":
            return broker.buy(signal.stock_code, quantity, tick, **kwargs)
        return broker.sell(signal.stock_code, quantity, tick, **kwargs)

    def run(
        self,
        tick_paths: str | os.PathLike[str] | list[str | os.PathLike[str]],
        *,
        stock_codes: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        curve: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        batches = 0
        last_batch: TickBatch | None = None
        latest_ticks: dict[str, Mapping[str, Any]] = {}
        limit_up_entries: list[dict[str, Any]] = []
        pending_signals: list[tuple[str, BacktestSignal]] = []
        expired_signal_count = 0
        current_trade_date = ""
        paths = discover_tick_files(tick_paths)
        if not paths:
            raise ValueError("no Tick archive files found")
        if self.config.validate_archives:
            invalid = []
            for path in paths:
                quality = verify_tick_archive(path)
                if not quality["valid"]:
                    invalid.append(f"{path}: {quality}")
            if invalid:
                raise ValueError(
                    "Tick archive failed integrity checks:\n" + "\n".join(invalid)
                )
        with PaperBroker(
            self.broker_config,
            database_path=self.database_path,
            account_id="backtest",
            reset=True,
        ) as broker:
            curve.append(broker.snapshot(0))
            for batch in iter_tick_batches(paths, stock_codes=stock_codes):
                if current_trade_date and batch.trade_date != current_trade_date:
                    for entry in limit_up_entries:
                        if (
                            entry["trade_date"] == current_trade_date
                            and entry["sealed_at_close"] is None
                        ):
                            closing_tick = latest_ticks.get(entry["stock_code"])
                            entry["sealed_at_close"] = bool(
                                closing_tick and _is_limit_up_tick(closing_tick)
                            )
                    expired_signal_count += len(pending_signals)
                    pending_signals.clear()
                    latest_ticks.clear()
                current_trade_date = batch.trade_date
                last_batch = batch
                batches += 1
                latest_ticks.update(batch.ticks)
                broker.mark_many(batch.ticks)
                for entry in limit_up_entries:
                    if entry["trade_date"] != batch.trade_date:
                        continue
                    tick = batch.ticks.get(entry["stock_code"])
                    if tick is None:
                        continue
                    if _is_limit_up_tick(tick):
                        entry["ever_sealed"] = True
                    elif entry["ever_sealed"]:
                        entry["broken_after_seal"] = True

                ready: list[BacktestSignal] = []
                waiting: list[BacktestSignal] = []
                for signal_date, signal in pending_signals:
                    if signal_date != batch.trade_date:
                        expired_signal_count += 1
                    elif signal.stock_code in batch.ticks:
                        ready.append(signal)
                    else:
                        waiting.append((signal_date, signal))
                pending_signals = waiting
                current_signals = list(self._signals(batch, broker))
                if self.config.execute_on_next_tick:
                    pending_signals.extend(
                        (batch.trade_date, signal) for signal in current_signals
                    )
                else:
                    ready.extend(current_signals)

                for signal in ready:
                    fill = self._execute(signal, batch, broker)
                    if fill:
                        fills.append(asdict(fill))
                        if fill.side == "BUY" and signal.limit_up_entry:
                            is_sealed = _is_limit_up_tick(
                                batch.ticks[fill.stock_code]
                            )
                            limit_up_entries.append({
                                "stock_code": fill.stock_code,
                                "trade_date": batch.trade_date,
                                "entry_time_ms": fill.timestamp_ms,
                                "signal_id": signal.signal_id,
                                "ever_sealed": is_sealed,
                                "broken_after_seal": False,
                                "sealed_at_close": None,
                            })
                if batches % self.config.sample_equity_every_batches == 0:
                    curve.append(broker.checkpoint_equity(batch.event_time_ms))

            if self.config.close_positions_at_end and last_batch is not None:
                for code, position in list(broker.positions.items()):
                    tick = latest_ticks.get(code)
                    if tick and position["available_quantity"]:
                        fill = broker.sell(
                            code,
                            position["available_quantity"],
                            tick,
                            reason="backtest_end_liquidation",
                            respect_liquidity=False,
                        )
                        if fill:
                            fills.append(asdict(fill))
            if last_batch is not None:
                curve.append(broker.checkpoint_equity(last_batch.event_time_ms))
            for entry in limit_up_entries:
                if entry["sealed_at_close"] is None:
                    closing_tick = latest_ticks.get(entry["stock_code"])
                    entry["sealed_at_close"] = bool(
                        closing_tick and _is_limit_up_tick(closing_tick)
                    )
            broker._backtest_limit_up_stats = {
                "entries": limit_up_entries,
            }
            metrics = calculate_performance(broker, curve, total_batches=batches)
            trades = broker.closed_trades()
            positions = broker.positions

        return {
            "schema_version": 1,
            "broker_config": asdict(self.broker_config),
            "backtest_config": asdict(self.config),
            "metrics": metrics,
            "equity_curve": curve,
            "fills": fills,
            "closed_trades": trades,
            "open_positions": positions,
            "unexecuted_signal_count": len(pending_signals),
            "expired_signal_count": expired_signal_count,
            "limit_up_entries": limit_up_entries,
        }


def write_backtest_result(
    result: Mapping[str, Any], output_path: str | os.PathLike[str]
) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)
    return path
