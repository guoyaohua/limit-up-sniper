"""Pure helpers for quote-state and exposure checks.

Keeping these rules outside the live loop gives production, simulation and
backtest paths one auditable definition instead of three subtly different
ones.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from infra.xtconstant_compat import xtconstant


# A synchronous XTQuant order can be accepted before a following cancellable-
# order query exposes it.  Reservations bridge that short acknowledgement gap
# so a stale broker refresh cannot temporarily reopen an already consumed slot.
LOCAL_BUY_RESERVATION_FIELD = "本地预占"
LOCAL_BUY_RESERVATION_TIME_FIELD = "本地预占时间"
LOCAL_BUY_RESERVATION_TTL_SECONDS = 30.0


def first_book_value(value: Any) -> Any:
    """Return level one from list/tuple/scalar quote representations."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else 0
    try:
        # XTQuant may expose numpy arrays; avoid importing numpy in this helper.
        if value is not None and not isinstance(value, (str, bytes, Mapping)):
            return value[0]
    except (IndexError, KeyError, TypeError):
        pass
    return value


def is_sealed_limit_up_quote(
    tick: Mapping[str, Any],
    limit_up_price: float | None = None,
    *,
    tolerance: float = 0.001,
) -> bool:
    """Return whether level one proves a currently sealed daily limit-up.

    A best bid at the limit is not sufficient: the last trade must also be at
    the limit and the best ask must be empty.  This is the same conservative
    evidence required by queue-fill simulation and seal-rate backtests.
    """
    try:
        last_price = float(tick.get("lastPrice", 0) or 0)
        bid_price = float(first_book_value(tick.get("bidPrice")) or 0)
        ask_price = float(first_book_value(tick.get("askPrice")) or 0)
        raw_limit = (
            limit_up_price
            if limit_up_price is not None
            else tick.get("limitUpPrice", tick.get("upperLimitPrice"))
        )
        explicit_limit = float(raw_limit or 0)
    except (TypeError, ValueError):
        return False
    if last_price <= 0 or bid_price <= 0 or ask_price > 0:
        return False
    if abs(bid_price - last_price) > tolerance:
        return False
    # An empty ask plus a matched bid/last trade can also occur outside a daily
    # price limit (illiquid securities, auction transitions, stale books).  A
    # positive explicit daily limit is therefore mandatory for seal accounting.
    return explicit_limit > 0 and abs(bid_price - explicit_limit) <= tolerance


def _is_buy_order(order: Mapping[str, Any]) -> bool:
    order_type = order.get("委托类型")
    if isinstance(order_type, str):
        normalized = order_type.strip()
        if normalized.casefold() == "buy" or normalized == "买入":
            return True
        try:
            order_type = int(normalized)
        except ValueError:
            return False
    return order_type == xtconstant.STOCK_BUY


def _is_sell_order(order: Mapping[str, Any]) -> bool:
    order_type = order.get("委托类型")
    if isinstance(order_type, str):
        normalized = order_type.strip()
        if normalized.casefold() == "sell" or normalized == "卖出":
            return True
        try:
            order_type = int(normalized)
        except ValueError:
            return False
    return order_type == xtconstant.STOCK_SELL


def build_local_buy_reservation(
    stock_code: str,
    order_id: Any,
    *,
    created_at: float | None = None,
    **fields: Any,
) -> str:
    """Return a strategy-compatible pending BUY reservation record."""
    record = {
        "委托类型": xtconstant.STOCK_BUY,
        "证券代码": stock_code,
        "订单编号": order_id,
        LOCAL_BUY_RESERVATION_FIELD: True,
        LOCAL_BUY_RESERVATION_TIME_FIELD: (
            time.time() if created_at is None else float(created_at)
        ),
        **fields,
    }
    return json.dumps([record], ensure_ascii=False)


def _matching_order_id(orders: Any, order_id: Any) -> bool:
    if isinstance(orders, Mapping):
        orders = [orders]
    if not isinstance(orders, (list, tuple)):
        return False
    expected = str(order_id)
    return any(
        isinstance(order, Mapping)
        and str(order.get("订单编号")) == expected
        and _is_buy_order(order)
        for order in orders
    )


def merge_active_orders_with_local_reservations(
    active_orders: Mapping[str, Any],
    cached_orders: Mapping[str, Any],
    *,
    now: float | None = None,
    ttl_seconds: float = LOCAL_BUY_RESERVATION_TTL_SECONDS,
) -> dict[str, Any]:
    """Keep recent accepted BUY reservations missing from one broker refresh.

    The broker result remains authoritative once it contains the stock.  A
    reservation expires quickly if QMT never acknowledges it, preventing a
    rejected/terminal order from blocking capacity for the whole session.
    Malformed reservation records are not carried forward.
    """
    merged = dict(active_orders)
    reference_time = time.time() if now is None else float(now)
    for stock_code, encoded in cached_orders.items():
        try:
            orders = json.loads(encoded) if isinstance(encoded, str) else encoded
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(orders, Mapping):
            orders = [orders]
        if not isinstance(orders, (list, tuple)) or not orders:
            continue
        reservation = orders[0]
        if (
            not isinstance(reservation, Mapping)
            or reservation.get(LOCAL_BUY_RESERVATION_FIELD) is not True
            or not _is_buy_order(reservation)
        ):
            continue
        try:
            age = reference_time - float(
                reservation[LOCAL_BUY_RESERVATION_TIME_FIELD]
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= age <= ttl_seconds:
            continue
        broker_encoded = merged.get(stock_code)
        if broker_encoded is not None:
            try:
                broker_orders = (
                    json.loads(broker_encoded)
                    if isinstance(broker_encoded, str)
                    else broker_encoded
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                broker_orders = None
            # A matching order id is QMT's acknowledgement.  Otherwise a stock
            # may legitimately have an older sell order while the new BUY is
            # still crossing the acknowledgement gap.  Keep the reservation
            # as the cache value because should_cancel() assumes one direction
            # per stock and must see the newly submitted BUY first.
            if _matching_order_id(broker_orders, reservation.get("订单编号")):
                continue
            merged[stock_code] = encoded
        else:
            merged[stock_code] = encoded
    return merged


def _has_pending_buy_or_unknown_order(encoded: Any) -> bool:
    """Fail closed when a supposedly active-order record is malformed.

    ``order_status`` is populated from XTQuant's cancelable-order query, so a
    stock-code key is evidence of live exposure.  Known sell orders do not use
    a buy slot; unreadable or incomplete records do until the next successful
    broker refresh repairs the cache.
    """
    try:
        orders = json.loads(encoded) if isinstance(encoded, str) else encoded
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    if isinstance(orders, Mapping):
        orders = [orders]
    if not isinstance(orders, (list, tuple)) or not orders:
        return True

    has_buy = False
    for order in orders:
        if not isinstance(order, Mapping) or "委托类型" not in order:
            return True
        if _is_buy_order(order):
            has_buy = True
        if not _is_sell_order(order):
            if not _is_buy_order(order):
                return True
    return has_buy


def exposure_slot_count(
    holding_status: Mapping[str, Any], order_status: Mapping[str, Any]
) -> int:
    """Return occupied slots including positions and pending buy exposure."""
    held_codes = set(holding_status.keys())
    pending_codes = set()
    for stock_code, encoded in order_status.items():
        if _has_pending_buy_or_unknown_order(encoded):
            pending_codes.add(stock_code)
    return len(held_codes | pending_codes)
