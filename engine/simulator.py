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
from engine.queue_fill import queued_buy_fill_progress
from infra.common_enums import OrderType, StockOrderStatusInt
from core.market_microstructure import (
    exposure_slot_count, is_sealed_limit_up_quote,
)


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


_PAPER_LANES = {"primary_simulation", "live_mirror", "coverage",
                "baseline", "challenger"}


def _normalize_lane(lane: str | bool | None) -> str:
    if isinstance(lane, bool):
        return "coverage" if lane else "primary_simulation"
    normalized = str(lane or "primary_simulation").strip().lower()
    if normalized not in _PAPER_LANES:
        raise ValueError(f"未知纸面账户通道: {normalized!r}")
    return normalized


def _lane_env_float(lane: str, suffix: str, default: float) -> float:
    prefix = lane.upper().replace("-", "_")
    return _env_float(
        f"LIMIT_UP_{prefix}_{suffix}",
        _env_float(f"LIMIT_UP_PAPER_{suffix}", default),
    )


def paper_broker_config(lane: str | bool = "primary_simulation") -> BrokerConfig:
    lane = _normalize_lane(lane)
    if lane == "coverage":
        # Coverage is a fixed-notional signal census.  Its cash pool is only a
        # technical ceiling and must not silently inherit a small production
        # simulation balance from LIMIT_UP_PAPER_INITIAL_CASH.
        initial_cash = _env_float("LIMIT_UP_COVERAGE_INITIAL_CASH", 1_000_000_000)
    elif lane in {"baseline", "challenger"}:
        initial_cash = _env_float(
            "LIMIT_UP_EXPERIMENT_INITIAL_CASH",
            _env_float("LIMIT_UP_PAPER_INITIAL_CASH", 1_000_000),
        )
    else:
        initial_cash = _lane_env_float(lane, "INITIAL_CASH", 1_000_000)
    return BrokerConfig(
        initial_cash=initial_cash,
        commission_rate=_env_float("LIMIT_UP_PAPER_COMMISSION_RATE", 0.0003),
        minimum_commission=_env_float("LIMIT_UP_PAPER_MIN_COMMISSION", 5.0),
        stamp_duty_rate=_env_float("LIMIT_UP_PAPER_STAMP_DUTY_RATE", 0.0005),
        transfer_fee_rate=_env_float("LIMIT_UP_PAPER_TRANSFER_FEE_RATE", 0.00001),
        slippage_bps=_env_float("LIMIT_UP_PAPER_SLIPPAGE_BPS", 2.0),
        participation_rate=_env_float("LIMIT_UP_PAPER_PARTICIPATION_RATE", 0.10),
        allow_t0=_env_bool("LIMIT_UP_PAPER_ALLOW_T0", False),
    )


def paper_database_path(
    lane: str | bool = "primary_simulation", experiment_id: str = ""
) -> Path:
    lane = _normalize_lane(lane)
    configured = os.getenv("LIMIT_UP_PAPER_DB")
    base = Path(configured).expanduser() if configured else Path("output") / "paper_trading"
    if base.suffix:
        if lane != "primary_simulation":
            raise ValueError(
                "LIMIT_UP_PAPER_DB 指向单个文件时不能启用多个纸面通道"
            )
        return base.resolve()
    if lane in {"baseline", "challenger"}:
        from core.runtime_lanes import validate_experiment_id
        experiment_id = validate_experiment_id(experiment_id)
        return (base / "experiments" / experiment_id / f"{lane}.sqlite3").resolve()
    return (base / f"{lane}.sqlite3").resolve()


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
            available_quantity = int(position.get("可用数量", 0) or 0)
            # Legacy shared views do not persist opened_date.  A restored
            # position necessarily came from an earlier session/day, so seed
            # it as prior inventory instead of freezing it forever at T+1.
            opened_date = str(position.get("开仓日期", "19700101") or "19700101")
            if opened_date < datetime.now().strftime("%Y%m%d"):
                available_quantity = int(position.get("持仓数量", 0) or 0)
            broker.restore_position(
                stock_code,
                int(position.get("持仓数量", 0) or 0),
                float(position.get("成本价", 0) or 0),
                available_quantity=available_quantity,
                mark_price=(
                    float(position.get("市值", 0) or 0)
                    / max(1, int(position.get("持仓数量", 0) or 0))
                ),
                opened_date=opened_date,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning(f"[模拟] 无法导入既有持仓: {stock_code}")


def _register_opening_positions(
    broker: PaperBroker, shared_data: Mapping[str, Any]
) -> None:
    pre_market = shared_data.get("盘前持仓")
    if pre_market is None:
        return
    existing = set(pre_market)
    for stock_code in broker.positions:
        if stock_code not in existing:
            pre_market.append(stock_code)


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
    lane: str = "primary_simulation",
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

    if lane == "coverage":
        max_position_value = _lane_env_float(
            "coverage", "POSITION_VALUE", 100_000.0)
        budget = min(max_position_value, broker.cash)
        return int(budget / price / broker.config.lot_size) * broker.config.lot_size

    max_position_value = broker.config.initial_cash / max(1, MAX_HOLDING_COUNT)
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
    lane: str = "primary_simulation",
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
        cancel_count = shared_data.get("撤单次数")
        if cancel_count is not None:
            if hasattr(cancel_count, "get_lock"):
                with cancel_count.get_lock():
                    cancel_count.value += 1
            elif hasattr(cancel_count, "value"):
                cancel_count.value += 1
        return None

    if order_type == OrderType.BUY and order_req.get("买入类型") == "排板":
        if stock_code in broker.positions or stock_code in pending:
            return None
        # review 20260714: pending buys consume exposure too; otherwise several
        # worker signals can exceed the documented maximum holding count.
        if (lane != "coverage" and
                exposure_slot_count(shared_data["持仓状态"], pending) >= MAX_HOLDING_COUNT):
            _set_status(shared_data, stock_code, StockOrderStatusInt.NOT_ORDERED)
            return None
        quantity = _requested_buy_quantity(
            broker, order_req, shared_data, shadow_signal_mode, lane
        )
        if quantity <= 0:
            return None
        pending[stock_code] = json.dumps(
            [
                {
                    # Match the real-trader order schema so should_cancel() can
                    # identify this as a cancellable buy order.
                    "委托类型": OrderType.买入.value,
                    "证券代码": stock_code,
                    "委托价格": float(order_req["委托价格"]),
                    "委托数量": quantity,
                    "订单编号": f"paper_pending_{stock_code}_{_event_suffix(tick)}",
                    "操作原因": reason,
                    "signal_id": signal_id,
                    # XTQuant quote volumes are lots.  These immutable fields
                    # are the only evidence accepted by the queue fill model.
                    "下单累计成交手数": float(tick.get("volume", 0) or 0),
                    "前方队列手数": float((tick.get("bidVol") or [0])[0] or 0),
                    "本单手数": quantity / broker.config.lot_size,
                }
            ],
            ensure_ascii=False,
        )
        status = shared_data["股票状态信号"][stock_code]
        if tick:
            for key, value in (
                ("下单时成交量", int(tick.get("volume", 0) or 0)),
                ("下单时封单量", int((tick.get("bidVol") or [0])[0] or 0)),
            ):
                target = status.get(key)
                if target is not None:
                    with target.get_lock():
                        target.value = value
        _set_status(shared_data, stock_code, StockOrderStatusInt.ORDERED_BUY)
        return None

    if order_type == OrderType.BUY:
        if (lane != "coverage"
                and stock_code not in broker.positions
                and order_req.get("买入类型") != "模拟成交"
                and exposure_slot_count(shared_data["持仓状态"], pending)
                >= MAX_HOLDING_COUNT):
            _set_status(shared_data, stock_code, StockOrderStatusInt.NOT_ORDERED)
            return None
        execution_tick = tick
        if order_req.get("买入类型") == "模拟成交":
            encoded = pending.get(stock_code)
            if not encoded:
                return None
            pending_order = json.loads(encoded)[0]
            quantity = int(pending_order["委托数量"])
            limit_price = float(pending_order["委托价格"])
            reason = str(pending_order.get("操作原因", reason))
            signal_id = str(pending_order.get("signal_id", signal_id))
            # A queue fill must still be observed on a sealed limit-up book.
            # Merely trading at the limit price after the ask queue reappears
            # is an opened board and cannot prove our bid was reached.
            is_limit_up = is_sealed_limit_up_quote(tick, limit_price, tolerance=0.0001)
            progress = queued_buy_fill_progress(
                pending_order, tick, is_limit_up=is_limit_up
            )
            if not progress.confirmed:
                logger.warning(
                    f"[模拟] 拒绝无充分排队证据的成交 {stock_code}: "
                    f"{progress.reason}"
                )
                return None
            # Queue evidence has already established a full fill at the daily
            # limit.  Do not let a sparse/stale ask snapshot grant an impossible
            # price improvement below the original limit order.
            execution_tick = dict(tick)
            execution_tick["lastPrice"] = limit_price
            execution_tick["askPrice"] = [limit_price]
            execution_tick["askVol"] = [
                quantity / broker.config.lot_size
            ]
            respect_liquidity = False
        else:
            quantity = _requested_buy_quantity(
                broker, order_req, shared_data, shadow_signal_mode, lane
            )
            limit_price = float(order_req.get("委托价格", 0) or 0) or None
            respect_liquidity = True
        fill = broker.buy(
            stock_code,
            quantity,
            execution_tick,
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
    lane: str | None = None,
    experiment_id: str = "",
    stop_event=None,
):
    """Consume strategy orders and live marks into a persistent paper account."""
    mode = _normalize_lane(lane if lane is not None else shadow_signal_mode)
    database_path = paper_database_path(mode, experiment_id)
    config = paper_broker_config(mode)
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
            _register_opening_positions(broker, shared_data)
            last_checkpoint_at = 0.0
            while True:
                if stop_event is not None and stop_event.is_set():
                    logger.info(f"[{mode}] 收到停止信号: {broker.snapshot()}")
                    return
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
                        lane=mode,
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
