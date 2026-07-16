"""Replay normalized BUY/SELL decisions recorded in ``events.jsonl``."""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from data.tick_archive import TickBatch
from core.market_microstructure import is_sealed_limit_up_quote
from engine.backtest import BacktestSignal, _limit_up_price_for_entry
from engine.paper_broker import PaperBroker
from engine.queue_fill import queued_buy_fill_progress


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
SUPPORTED_EVENT_TYPES = {"buy_decision", "cancel_decision", "sell_decision"}
SUPPORTED_EVENT_SOURCES = {
    "primary", "shadow", "coverage", "baseline", "challenger"
}


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, (list, tuple)) and value else value


@dataclass(frozen=True)
class ReplayEvent:
    event_type: str
    timestamp_ms: int
    stock_code: str
    source: str
    buy_type: str = ""
    target_remaining: int | None = None
    reason: str = ""
    raw: Mapping[str, Any] | None = None


def _parse_timestamp_ms(event: Mapping[str, Any]) -> int:
    value = event.get("timestamp")
    if value:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid event timestamp: {value!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CHINA_TZ)
        return int(parsed.timestamp() * 1000)
    snapshot = event.get("snapshot") or {}
    try:
        timestamp_ms = int(snapshot.get("time", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("event has no valid timestamp") from exc
    if timestamp_ms <= 0:
        raise ValueError("event has no valid timestamp")
    return timestamp_ms


def load_replay_events(
    paths: str | Path | Sequence[str | Path],
    *,
    source: str = "primary",
    accept_legacy_unlabelled: bool = False,
) -> tuple[list[ReplayEvent], dict[str, int]]:
    """Load decision events, failing closed on ambiguous legacy lanes by default."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    if source not in SUPPORTED_EVENT_SOURCES:
        raise ValueError(
            "source must be one of " + ", ".join(sorted(SUPPORTED_EVENT_SOURCES))
        )
    events: list[ReplayEvent] = []
    diagnostics = {
        "loaded": 0,
        "ignored_non_signal": 0,
        "ignored_other_source": 0,
        "legacy_unlabelled": 0,
    }
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if path.is_dir():
            event_files = sorted(path.rglob("events.jsonl"))
            if not event_files:
                raise FileNotFoundError(f"no events.jsonl under {path}")
            nested, nested_diagnostics = load_replay_events(
                event_files,
                source=source,
                accept_legacy_unlabelled=accept_legacy_unlabelled,
            )
            events.extend(nested)
            for key, value in nested_diagnostics.items():
                diagnostics[key] += value
            continue
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid event JSON at {path}:{line_number}") from exc
                event_type = str(record.get("event_type", ""))
                if event_type not in SUPPORTED_EVENT_TYPES:
                    diagnostics["ignored_non_signal"] += 1
                    continue
                event_source = record.get("signal_source")
                if event_source is None:
                    diagnostics["legacy_unlabelled"] += 1
                    if not accept_legacy_unlabelled:
                        raise ValueError(
                            f"unlabelled strategy event at {path}:{line_number}; "
                            "primary and shadow signals cannot be distinguished"
                        )
                    event_source = source
                elif event_source not in SUPPORTED_EVENT_SOURCES:
                    raise ValueError(
                        f"invalid signal_source at {path}:{line_number}: "
                        f"{event_source!r}"
                    )
                if event_source != source:
                    diagnostics["ignored_other_source"] += 1
                    continue
                stock_code = str(record.get("stock_code", "")).strip()
                if not stock_code:
                    raise ValueError(f"empty event stock code at {path}:{line_number}")
                target = record.get("target_remaining_volume")
                buy_type = str(record.get("buy_type", ""))
                if event_type == "buy_decision" and buy_type not in {"排板", "扫板"}:
                    raise ValueError(
                        f"unsupported buy_type at {path}:{line_number}: {buy_type!r}"
                    )
                events.append(
                    ReplayEvent(
                        event_type=event_type,
                        timestamp_ms=_parse_timestamp_ms(record),
                        stock_code=stock_code,
                        source=str(event_source),
                        buy_type=buy_type,
                        target_remaining=(None if target is None else int(target)),
                        reason=str(record.get("reason", "")),
                        raw=record,
                    )
                )
                diagnostics["loaded"] += 1
    events.sort(key=lambda item: item.timestamp_ms)
    seen: set[tuple[str, int, str, str]] = set()
    for event in events:
        identity = (
            event.source,
            event.timestamp_ms,
            event.event_type,
            event.stock_code,
        )
        if identity in seen:
            raise ValueError(f"duplicate strategy event: {identity}")
        seen.add(identity)
    return events, diagnostics


class EventLogReplayStrategy:
    """Emit historical decisions only after their logged wall-clock timestamp.

    Sweep orders use the engine's normal next-stock-Tick execution.  Limit-up
    queue orders are held internally until cumulative traded lots cross the
    complete queue; opening the board permanently invalidates them.
    """

    def __init__(
        self,
        events: Iterable[ReplayEvent],
        *,
        buy_target_value: float = 100_000.0,
    ) -> None:
        if buy_target_value <= 0:
            raise ValueError("buy_target_value must be positive")
        self.events = sorted(events, key=lambda item: item.timestamp_ms)
        self.timestamps = [event.timestamp_ms for event in self.events]
        self.cursor = 0
        self.buy_target_value = float(buy_target_value)
        self.pending_queues: dict[str, dict[str, Any]] = {}
        self.expired_queue_count = 0
        self.cancelled_queue_count = 0
        self.duplicate_queue_event_count = 0
        self.last_trade_date = ""
        self.used_received_time_batches = 0
        self.event_time_fallback_batches = 0
        self.stale_event_count = 0

    @staticmethod
    def _signal_id(event: ReplayEvent) -> str:
        return (
            f"event:{event.source}:{event.timestamp_ms}:"
            f"{event.event_type}:{event.stock_code}"
        )

    def _queue_signal(
        self,
        event: ReplayEvent,
        tick: Mapping[str, Any],
        broker: PaperBroker,
        trade_date: str,
    ) -> None:
        bid_volume = float(_first(tick.get("bidVol")) or 0)
        limit_price = _limit_up_price_for_entry(tick)
        if (
            limit_price is None
            or not is_sealed_limit_up_quote(tick, limit_price)
        ):
            self.expired_queue_count += 1
            return
        quantity = (
            int(self.buy_target_value / limit_price / broker.config.lot_size)
            * broker.config.lot_size
        )
        if quantity <= 0:
            self.expired_queue_count += 1
            return
        if event.stock_code in self.pending_queues:
            self.duplicate_queue_event_count += 1
            return
        self.pending_queues[event.stock_code] = {
            "event": event,
            "limit_price": limit_price,
            "quantity": quantity,
            "trade_date": trade_date,
            "下单累计成交手数": float(tick.get("volume", 0) or 0),
            "前方队列手数": bid_volume,
            "本单手数": quantity / broker.config.lot_size,
        }

    def _advance_queues(self, batch: TickBatch) -> list[BacktestSignal]:
        signals: list[BacktestSignal] = []
        for stock_code, pending in list(self.pending_queues.items()):
            if pending["trade_date"] != batch.trade_date:
                self.pending_queues.pop(stock_code, None)
                self.expired_queue_count += 1
                continue
            tick = batch.ticks.get(stock_code)
            if tick is None:
                continue
            if not is_sealed_limit_up_quote(tick, pending["limit_price"]):
                self.pending_queues.pop(stock_code, None)
                self.expired_queue_count += 1
                continue
            progress = queued_buy_fill_progress(
                pending, tick, is_limit_up=True
            )
            if not progress.confirmed:
                continue
            event = pending["event"]
            signals.append(
                BacktestSignal(
                    stock_code=stock_code,
                    side="BUY",
                    quantity=pending["quantity"],
                    limit_price=pending["limit_price"],
                    reason=event.reason,
                    signal_id=self._signal_id(event),
                    limit_up_entry=True,
                    respect_liquidity=False,
                    execute_on_current_tick=True,
                )
            )
            self.pending_queues.pop(stock_code, None)
        return signals

    def _consume_events(
        self,
        events: Iterable[ReplayEvent],
        batch: TickBatch,
        broker: PaperBroker,
        *,
        execute_on_current_tick: bool,
    ) -> list[BacktestSignal]:
        signals: list[BacktestSignal] = []
        for event in events:
            if event.event_type == "cancel_decision":
                if self.pending_queues.pop(event.stock_code, None) is not None:
                    self.cancelled_queue_count += 1
            elif event.event_type == "buy_decision":
                if event.buy_type == "排板":
                    snapshot = dict((event.raw or {}).get("snapshot") or {})
                    event_limit = (event.raw or {}).get("limit_up_price")
                    if (
                        event_limit is not None
                        and "limitUpPrice" not in snapshot
                        and "upperLimitPrice" not in snapshot
                    ):
                        snapshot["limitUpPrice"] = event_limit
                    if not snapshot:
                        self.expired_queue_count += 1
                    else:
                        self._queue_signal(event, snapshot, broker, batch.trade_date)
                else:
                    can_execute_now = (
                        execute_on_current_tick
                        and event.stock_code in batch.ticks
                    )
                    signals.append(
                        BacktestSignal(
                            stock_code=event.stock_code,
                            side="BUY",
                            target_value=self.buy_target_value,
                            reason=event.reason,
                            signal_id=self._signal_id(event),
                            limit_up_entry=True,
                            execute_on_current_tick=can_execute_now,
                        )
                    )
            else:
                can_execute_now = (
                    execute_on_current_tick
                    and event.stock_code in batch.ticks
                )
                signals.append(
                    BacktestSignal(
                        stock_code=event.stock_code,
                        side="SELL",
                        target_remaining=event.target_remaining,
                        reason=event.reason,
                        signal_id=self._signal_id(event),
                        execute_on_current_tick=can_execute_now,
                    )
                )
        return signals

    def on_tick_batch(
        self, batch: TickBatch, broker: PaperBroker
    ) -> Iterable[BacktestSignal]:
        if self.last_trade_date and batch.trade_date != self.last_trade_date:
            self.expired_queue_count += len(self.pending_queues)
            self.pending_queues.clear()
        self.last_trade_date = batch.trade_date
        # Decision logs use local wall-clock time.  Align them to the archive's
        # callback receipt clock, not the exchange quote clock; otherwise clock
        # skew can release a decision against a quote that arrived before the
        # decision actually existed.  Legacy/synthetic archives fall back to the
        # exchange timestamp and expose that weaker mode in diagnostics.
        receipt_cutoff = batch.received_time_ms
        # Synthetic/legacy archives may be written long after the historical
        # quote.  A receipt clock modestly *after* exchange time is plausible
        # callback latency; a much later one is probably synthetic and falls
        # back.  Conversely, an exchange timestamp may be arbitrarily ahead of
        # the local clock: falling back in that direction would release future
        # decisions before the callback was actually received (lookahead).
        receipt_trade_date = (
            datetime.fromtimestamp(receipt_cutoff / 1000, CHINA_TZ).strftime(
                "%Y%m%d"
            )
            if receipt_cutoff > 0
            else ""
        )
        receipt_is_aligned = (
            receipt_cutoff > 0
            and batch.event_time_ms > 0
            and receipt_trade_date == batch.trade_date
            and receipt_cutoff - batch.event_time_ms <= 60_000
        )
        if receipt_is_aligned:
            cutoff = receipt_cutoff
            self.used_received_time_batches += 1
        else:
            cutoff = batch.event_time_ms
            self.event_time_fallback_batches += 1
        # A receipt timestamp truncated to milliseconds cannot prove whether an
        # event with the same millisecond happened before or after the callback.
        # In receipt-clock mode require a strict ordering; fallback mode still
        # queues equality for the engine's normal next-Tick execution.
        upper = (
            bisect_left(self.timestamps, cutoff, lo=self.cursor)
            if receipt_is_aligned
            else bisect_right(self.timestamps, cutoff, lo=self.cursor)
        )
        released = self.events[self.cursor:upper]
        current_events: list[ReplayEvent] = []
        for event in released:
            event_trade_date = datetime.fromtimestamp(
                event.timestamp_ms / 1000, CHINA_TZ
            ).strftime("%Y%m%d")
            if event_trade_date < batch.trade_date:
                # A decision that never saw another same-day quote is expired,
                # not an order that may be filled at tomorrow's opening price.
                self.stale_event_count += 1
            elif event_trade_date == batch.trade_date:
                current_events.append(event)
            else:
                raise ValueError(
                    "event timestamp is later than Tick archive trade_date: "
                    f"{event_trade_date} > {batch.trade_date}"
                )
        if receipt_is_aligned:
            # These decisions were logged before this callback arrived.  Apply
            # cancels/new queue orders first, then let this first subsequent
            # quote provide execution evidence.  Sweep/sell signals may execute
            # on it because it is already the next Tick after the decision.
            signals = self._consume_events(
                current_events, batch, broker, execute_on_current_tick=True
            )
            signals.extend(self._advance_queues(batch))
        else:
            # Exchange-time fallback cannot prove the event preceded this same
            # quote, so retain the conservative next-Tick ordering.
            signals = self._advance_queues(batch)
            signals.extend(self._consume_events(
                current_events, batch, broker, execute_on_current_tick=False
            ))
        self.cursor = upper
        return signals

    def diagnostics(self) -> dict[str, int]:
        return {
            "event_count": len(self.events),
            "unconsumed_event_count": len(self.events) - self.cursor,
            "pending_queue_count": len(self.pending_queues),
            "expired_queue_count": self.expired_queue_count,
            "cancelled_queue_count": self.cancelled_queue_count,
            "duplicate_queue_event_count": self.duplicate_queue_event_count,
            "received_time_alignment_batches": self.used_received_time_batches,
            "event_time_fallback_batches": self.event_time_fallback_batches,
            "stale_event_count": self.stale_event_count,
        }
