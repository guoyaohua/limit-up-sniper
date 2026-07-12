"""Conservative fill evidence for limit-up queue orders."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class QueueFillProgress:
    """Auditable progress of one queued buy, expressed in XTQuant lots."""

    confirmed: bool
    traded_lots: float = 0.0
    required_lots: float = 0.0
    reason: str = ""


def queued_buy_fill_progress(
    pending_order: Mapping[str, Any],
    tick: Mapping[str, Any],
    *,
    is_limit_up: bool,
) -> QueueFillProgress:
    """Return full-fill evidence without treating cancellations as trades.

    A snapshot cannot distinguish a reduction in ``bidVol`` caused by trades
    from one caused by cancelled orders.  Only cumulative exchange volume is
    therefore allowed to consume the queue that was ahead at submission.
    Partial fills are intentionally not inferred: the strategy may only book
    the order after trades cover the queue ahead *and* the complete order.
    """
    if not is_limit_up:
        return QueueFillProgress(False, reason="limit-up opened")
    if pending_order.get("排队已失效"):
        return QueueFillProgress(False, reason="queue position invalidated")

    try:
        submitted_volume = float(pending_order["下单累计成交手数"])
        queue_ahead = float(pending_order["前方队列手数"])
        order_lots = float(pending_order["本单手数"])
        current_volume = float(tick["volume"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return QueueFillProgress(False, reason="incomplete queue snapshot")

    values = (submitted_volume, queue_ahead, order_lots, current_volume)
    if not all(math.isfinite(value) for value in values):
        return QueueFillProgress(False, reason="non-finite queue snapshot")
    if submitted_volume < 0 or queue_ahead < 0 or order_lots <= 0:
        return QueueFillProgress(False, reason="invalid queue snapshot")

    traded_lots = max(0.0, current_volume - submitted_volume)
    required_lots = queue_ahead + order_lots
    return QueueFillProgress(
        traded_lots >= required_lots,
        traded_lots=traded_lots,
        required_lots=required_lots,
        reason=(
            "cumulative trades crossed complete queue"
            if traded_lots >= required_lots
            else "insufficient cumulative trades"
        ),
    )
