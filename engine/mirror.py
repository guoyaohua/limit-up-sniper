"""Paper mirror fed only by broker-accepted production orders."""

from __future__ import annotations

from datetime import datetime
from queue import Empty
from typing import Any, Mapping

from loguru import logger

from config import STOP_TIME
from core.market_microstructure import is_sealed_limit_up_quote
from engine.paper_broker import PaperBroker
from engine.queue_fill import queued_buy_fill_progress
from engine.simulator import paper_broker_config, paper_database_path
from infra.common_enums import OrderType


class LiveMirrorAccount:
    """Mirror accepted live submissions without running a second strategy."""

    def __init__(self, broker: PaperBroker) -> None:
        self.broker = broker
        self.pending: dict[str, dict[str, Any]] = {}

    def submit(self, order: Mapping[str, Any]) -> None:
        stock_code = str(order["股票代码"])
        order_type = order.get("委托类型")
        tick = dict(order.get("快照") or {})
        signal_id = str(order.get("signal_id") or "")
        reason = str(order.get("操作原因", order.get("委托备注", "")))
        if order_type == OrderType.CANCEL:
            self.pending.pop(stock_code, None)
            return
        if order_type == OrderType.BUY:
            quantity = int(order.get("委托数量", 0) or 0)
            price = float(order.get("委托价格", 0) or 0)
            if quantity <= 0 or price <= 0:
                return
            if order.get("买入类型") == "排板":
                self.pending[stock_code] = {
                    "委托数量": quantity,
                    "委托价格": price,
                    "下单累计成交手数": float(tick.get("volume", 0) or 0),
                    "前方队列手数": float((tick.get("bidVol") or [0])[0] or 0),
                    "本单手数": quantity / self.broker.config.lot_size,
                    "signal_id": signal_id,
                    "reason": reason,
                }
                return
            self.broker.buy(
                stock_code, quantity, tick, limit_price=price, reason=reason,
                signal_id=signal_id,
            )
            return
        if order_type == OrderType.SELL:
            quantity = int(order.get("委托数量", 0) or 0)
            if quantity <= 0:
                position = self.broker.positions.get(stock_code, {})
                quantity = int(position.get("available_quantity", 0) or 0)
            self.broker.sell(
                stock_code, quantity, tick,
                limit_price=float(order.get("委托价格", 0) or 0) or None,
                reason=reason, signal_id=signal_id,
            )

    def mark(self, datas: Mapping[str, Mapping[str, Any]]) -> None:
        self.broker.mark_many(datas)
        for stock_code in list(self.pending):
            tick = datas.get(stock_code)
            if not tick:
                continue
            pending = self.pending[stock_code]
            price = float(pending["委托价格"])
            sealed = is_sealed_limit_up_quote(tick, price, tolerance=0.0001)
            progress = queued_buy_fill_progress(pending, tick, is_limit_up=sealed)
            if not sealed:
                self.pending.pop(stock_code, None)
                continue
            if not progress.confirmed:
                continue
            execution_tick = dict(tick)
            execution_tick["lastPrice"] = price
            execution_tick["askPrice"] = [price]
            execution_tick["askVol"] = [
                int(pending["委托数量"]) / self.broker.config.lot_size
            ]
            self.broker.buy(
                stock_code, int(pending["委托数量"]), execution_tick,
                limit_price=price, reason=str(pending.get("reason", "")),
                signal_id=str(pending.get("signal_id", "")),
                respect_liquidity=False,
            )
            self.pending.pop(stock_code, None)


def run_live_mirror(order_queue, market_queue, stop_event=None) -> None:
    lane = "live_mirror"
    database_path = paper_database_path(lane)
    with PaperBroker(
        paper_broker_config(lane), database_path=database_path, account_id=lane
    ) as broker:
        mirror = LiveMirrorAccount(broker)
        # Mirror validates intraday accepted-order execution.  It cannot infer
        # production holdings created before Mirror was enabled, so make the
        # limitation explicit instead of silently treating those sells as
        # successful mirror exits.
        if not broker.positions and broker.fills:
            logger.warning('[live_mirror] 历史成交存在但当前无持仓，继续校验当日新委托')
        last_checkpoint_at = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info(f"[live_mirror] 收到停止信号: {broker.snapshot()}")
                return
            try:
                latest: dict[str, Mapping[str, Any]] = {}
                while True:
                    try:
                        latest.update(market_queue.get_nowait())
                    except Empty:
                        break
                if latest:
                    mirror.mark(latest)
                    event_ms = max(int(t.get("time", 0) or 0) for t in latest.values())
                    if event_ms and event_ms - last_checkpoint_at >= 5_000:
                        broker.checkpoint_equity(event_ms)
                        last_checkpoint_at = event_ms
                mirror.submit(order_queue.get(timeout=0.1 if latest else 1))
            except Empty:
                if datetime.now().time() >= STOP_TIME:
                    logger.info(f"[live_mirror] 收盘退出: {broker.snapshot()}")
                    return
            except Exception:
                logger.exception("[live_mirror] 处理镜像订单失败")
