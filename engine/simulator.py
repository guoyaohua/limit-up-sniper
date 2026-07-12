"""Persistent simulation/shadow execution adapter.

The strategy still submits its legacy Chinese-keyed order dictionaries. This
adapter converts them to deterministic ``PaperBroker`` fills, mirrors the
ledger back into shared_data, and keeps pending limit-up queue orders cancellable.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from queue import Empty
from typing import Any, Mapping

from loguru import logger

from config import (
    MAX_HOLDING_COUNT,
    STOP_TIME,
    VOLATILITY_RATIO_MAX,
    VOLATILITY_RATIO_MIN,
    VOLATILITY_TARGET,
    WATCHLIST_POSITION_RATIO,
)
from engine.paper_broker import BrokerConfig, Fill, PaperBroker
from infra.common_enums import OrderType, StockOrderStatusInt


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value in (None, "") else float(value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def paper_broker_config(shadow_signal_mode: bool = False) -> BrokerConfig:
    default_cash = 10_000_000 if shadow_signal_mode else 1_000_000
    return BrokerConfig(
        initial_cash=_env_float("LIMIT_UP_PAPER_INITIAL_CASH", default_cash),
        commission_rate=_env_float("LIMIT_UP_PAPER_COMMISSION_RATE", 0.0003),
        minimum_commission=_env_float("LIMIT_UP_PAPER_MIN_COMMISSION", 5.0),
        stamp_duty_rate=_env_float("LIMIT_UP_PAPER_STAMP_DUTY_RATE", 0.0005),
        transfer_fee_rate=_env_float("LIMIT_UP_PAPER_TRANSFER_FEE_RATE", 0.00001),
        slippage_bps=_env_float("LIMIT_UP_PAPER_SLIPPAGE_BPS", 2.0),
        participation_rate=_env_float("LIMIT_UP_PAPER_PARTICIPATION_RATE", 0.10),
        allow_t0=_env_bool("LIMIT_UP_PAPER_ALLOW_T0", False),
    )


def paper_database_path(shadow_signal_mode: bool = False) -> Path:
    configured = os.getenv("LIMIT_UP_PAPER_DB")
    if configured:
        base = Path(configured).expanduser()
        if base.suffix:
            return base.resolve()
        return (base / ("shadow.sqlite3" if shadow_signal_mode else "simulation.sqlite3")).resolve()
    return (
        Path("output")
        / "paper_trading"
        / ("shadow.sqlite3" if shadow_signal_mode else "simulation.sqlite3")
    ).resolve()


def _set_status(shared_data: Mapping[str, Any], stock_code: str, value: int) -> None:
    try:
        status = shared_data["股票状态信号"][stock_code]["下单状态"]
        if hasattr(status, "get_lock"):
            with status.get_lock():
                status.value = value
        else:
            status.value = value
    except (KeyError, TypeError):
        logger.debug(f"[模拟] 无法更新 {stock_code} 下单状态")


def _sync_positions(broker: PaperBroker, shared_data: Mapping[str, Any]) -> None:
    target = shared_data["持仓状态"]
    latest = broker.to_shared_positions()
    for code in list(target.keys()):
        if code not in latest:
            target.pop(code, None)
    for code, position_json in latest.items():
        target[code] = position_json

    # Optional dashboard/read-model fields. Older restored shared_data instances
    # simply do not contain them, so the adapter remains backwards compatible.
    snapshot = broker.snapshot()
    paper_account = shared_data.get("模拟账户")
    if paper_account is not None:
        for key, value in snapshot.items():
            paper_account[key] = value


def _seed_existing_positions(
    broker: PaperBroker, shared_data: Mapping[str, Any]
) -> None:
    """Preserve restored/overnight holdings when creating a fresh paper database."""
    if broker.positions or broker.fills:
        return
    for stock_code, encoded in list(shared_data["持仓状态"].items()):
        try:
            position = json.loads(encoded) if isinstance(encoded, str) else dict(encoded)
            broker.restore_position(
                stock_code,
                int(position.get("持仓数量", 0) or 0),
                float(position.get("成本价", 0) or 0),
                available_quantity=int(position.get("可用数量", 0) or 0),
                mark_price=(
                    float(position.get("市值", 0) or 0)
                    / max(1, int(position.get("持仓数量", 0) or 0))
                ),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning(f"[模拟] 无法导入既有持仓: {stock_code}")


def mark_paper_positions(
    broker: PaperBroker,
    shared_data: Mapping[str, Any],
    datas: Mapping[str, Mapping[str, Any]],
) -> None:
    """Mark positions from live/replayed ticks and refresh the shared view."""
    broker.mark_many(datas)
    _sync_positions(broker, shared_data)


def _requested_buy_quantity(
    broker: PaperBroker,
    order_req: Mapping[str, Any],
    shared_data: Mapping[str, Any],
    shadow_signal_mode: bool,
) -> int:
    requested = int(order_req.get("委托数量", 0) or 0)
    if requested > 0:
        return requested
    price = float(order_req.get("委托价格", 0) or 0)
    if price <= 0:
        tick = order_req.get("快照", {})
        price = float(tick.get("lastPrice", 0) or 0)
    if price <= 0:
        return 0

    max_position_value = (
        100_000.0
        if shadow_signal_mode
        else broker.config.initial_cash / max(1, MAX_HOLDING_COUNT)
    )
    amplitude = float(
        shared_data.get("股票信息", {})
        .get(order_req["股票代码"], {})
        .get("近20日平均振幅", VOLATILITY_TARGET)
        or VOLATILITY_TARGET
    )
    volatility_ratio = VOLATILITY_TARGET / max(amplitude, 0.01)
    volatility_ratio = max(
        VOLATILITY_RATIO_MIN, min(VOLATILITY_RATIO_MAX, volatility_ratio)
    )
    max_position_value *= volatility_ratio
    if order_req["股票代码"] in shared_data.get("观察名单", {}):
        max_position_value *= WATCHLIST_POSITION_RATIO
    budget = min(max_position_value, broker.cash)
    return int(budget / price / broker.config.lot_size) * broker.config.lot_size


def _requested_sell_quantity(
    broker: PaperBroker, order_req: Mapping[str, Any]
) -> int:
    position = broker.positions.get(order_req["股票代码"])
    if not position:
        return 0
    current = int(position["quantity"])
    target = int(order_req.get("剩余仓位", 0) or 0)
    return max(0, current - target)


def execute_paper_order(
    broker: PaperBroker,
    order_req: Mapping[str, Any],
    shared_data: Mapping[str, Any],
    *,
    shadow_signal_mode: bool = False,
) -> Fill | None:
    """Execute one legacy order request and update strategy-compatible state."""
    stock_code = str(order_req["股票代码"])
    order_type = order_req.get("委托类型")
    tick = order_req.get("快照") or {}
    pending = shared_data["委托状态"]
    signal_id = str(order_req.get("signal_id", ""))
    reason = str(order_req.get("操作原因", order_req.get("委托备注", "")))

    if order_type == OrderType.CANCEL:
        if stock_code not in pending:
            return None
        pending.pop(stock_code, None)
        _set_status(shared_data, stock_code, StockOrderStatusInt.CANCELLED)
        return None

    if order_type == OrderType.BUY and order_req.get("买入类型") == "排板":
        if stock_code in broker.positions or stock_code in pending:
            return None
        quantity = _requested_buy_quantity(
            broker, order_req, shared_data, shadow_signal_mode
        )
        if quantity <= 0:
            return None
        pending[stock_code] = json.dumps(
            [
                {
                    "委托类型": OrderType.BUY.value,
                    "证券代码": stock_code,
                    "委托价格": float(order_req["委托价格"]),
                    "委托数量": quantity,
                    "订单编号": f"paper_pending_{stock_code}_{_event_suffix(tick)}",
                    "操作原因": reason,
                    "signal_id": signal_id,
                }
            ],
            ensure_ascii=False,
        )
        status = shared_data["股票状态信号"][stock_code]
        if tick:
            for key, value in (
                ("下单时成交量", float(tick.get("volume", 0) or 0)),
                ("下单时封单量", float((tick.get("bidVol") or [0])[0] or 0)),
            ):
                target = status.get(key)
                if target is not None:
                    with target.get_lock():
                        target.value = value
        _set_status(shared_data, stock_code, StockOrderStatusInt.ORDERED_BUY)
        return None

    if order_type == OrderType.BUY:
        if order_req.get("买入类型") == "模拟成交":
            encoded = pending.get(stock_code)
            if not encoded:
                return None
            pending_order = json.loads(encoded)[0]
            quantity = int(pending_order["委托数量"])
            limit_price = float(pending_order["委托价格"])
            reason = str(pending_order.get("操作原因", reason))
            signal_id = str(pending_order.get("signal_id", signal_id))
            respect_liquidity = False
        else:
            quantity = _requested_buy_quantity(
                broker, order_req, shared_data, shadow_signal_mode
            )
            limit_price = float(order_req.get("委托价格", 0) or 0) or None
            respect_liquidity = True
        fill = broker.buy(
            stock_code,
            quantity,
            tick,
            limit_price=limit_price,
            reason=reason,
            signal_id=signal_id,
            respect_liquidity=respect_liquidity,
        )
        if fill:
            pending.pop(stock_code, None)
            _set_status(shared_data, stock_code, StockOrderStatusInt.POSITION_HOLDING)
            _sync_positions(broker, shared_data)
        return fill

    if order_type == OrderType.SELL:
        quantity = _requested_sell_quantity(broker, order_req)
        fill = broker.sell(
            stock_code,
            quantity,
            tick,
            limit_price=float(order_req.get("委托价格", 0) or 0) or None,
            reason=reason,
            signal_id=signal_id,
        )
        if fill:
            state = (
                StockOrderStatusInt.POSITION_HOLDING
                if stock_code in broker.positions
                else StockOrderStatusInt.NOT_ORDERED
            )
            _set_status(shared_data, stock_code, state)
            _sync_positions(broker, shared_data)
        return fill

    logger.error(f"[模拟] 未知委托类型: {order_type!r}")
    return None


def update_paper_market(
    broker: PaperBroker,
    shared_data: Mapping[str, Any],
    datas: Mapping[str, Mapping[str, Any]],
) -> None:
    """Public helper for a live shadow task or an offline replay loop."""
    mark_paper_positions(broker, shared_data, datas)


def _event_suffix(tick: Mapping[str, Any]) -> str:
    event_ms = int(tick.get("time", 0) or 0)
    return str(event_ms or int(datetime.now().timestamp() * 1000))


def run_xt_trader_simulator(
    order_queue,
    shared_data,
    shadow_signal_mode: bool = False,
    market_queue=None,
):
    """Consume strategy orders and live marks into a persistent paper account."""
    mode = "shadow" if shadow_signal_mode else "simulation"
    database_path = paper_database_path(shadow_signal_mode)
    config = paper_broker_config(shadow_signal_mode)
    logger.info(
        f"[{mode}] 纸面账户启动: 初始资金={config.initial_cash:,.0f}, "
        f"账本={database_path}"
    )
    try:
        with PaperBroker(
            config,
            database_path=database_path,
            account_id=mode,
        ) as broker:
            _seed_existing_positions(broker, shared_data)
            _sync_positions(broker, shared_data)
            last_checkpoint_at = 0.0
            while True:
                try:
                    marked = False
                    if market_queue is not None:
                        latest_ticks: dict[str, Mapping[str, Any]] = {}
                        while True:
                            try:
                                latest_ticks.update(market_queue.get_nowait())
                            except Empty:
                                break
                        if latest_ticks:
                            update_paper_market(broker, shared_data, latest_ticks)
                            event_ms = max(
                                int(tick.get("time", 0) or 0)
                                for tick in latest_ticks.values()
                            )
                            if event_ms:
                                now = time.monotonic()
                                if now - last_checkpoint_at >= 5:
                                    broker.checkpoint_equity(event_ms)
                                    last_checkpoint_at = now
                            marked = True

                    order_req = order_queue.get(timeout=0.1 if marked else 1)
                    fill = execute_paper_order(
                        broker,
                        order_req,
                        shared_data,
                        shadow_signal_mode=shadow_signal_mode,
                    )
                    if fill:
                        logger.warning(
                            f"[{mode}] {fill.side} {fill.stock_code} "
                            f"{fill.quantity}@{fill.price:.3f}, fees={fill.fees:.2f}, "
                            f"equity={broker.snapshot()['equity']:,.2f}"
                        )
                except Empty:
                    if datetime.now().time() >= STOP_TIME:
                        logger.info(f"[{mode}] 纸面账户收盘退出: {broker.snapshot()}")
                        return
                except Exception:
                    logger.exception(f"[{mode}] 处理模拟订单失败")
    except Exception:
        logger.exception(f"[{mode}] 纸面账户启动失败")
        raise
