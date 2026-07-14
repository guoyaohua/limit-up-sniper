import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data.tick_archive import TickArchiveWriter
from engine.backtest import BacktestEngine
from engine.event_replay import EventLogReplayStrategy, load_replay_events
from engine.paper_broker import BrokerConfig


CHINA_TZ = timezone(timedelta(hours=8))


def _ms(hour: int, minute: int, second: int) -> int:
    return int(
        datetime(2026, 7, 10, hour, minute, second, tzinfo=CHINA_TZ).timestamp()
        * 1000
    )


def _tick(timestamp_ms: int, price: float, *, sealed=False, volume=1_000):
    return {
        "time": timestamp_ms,
        "lastPrice": price,
        "limitUpPrice": 11.0,
        "askPrice": [0.0 if sealed else price],
        "askVol": [0 if sealed else 10_000],
        "bidPrice": [price],
        "bidVol": [500],
        "volume": volume,
    }


def _events(path: Path, records: list[dict]) -> Path:
    content = "\n".join(
        json.dumps(record, ensure_ascii=False) for record in records
    )
    path.write_text(content + "\n", encoding="utf-8")
    return path


def _archive(
    path: Path,
    ticks: list[dict],
    *,
    trade_date: str = "20260710",
    session_id: str = "events",
) -> Path:
    writer = TickArchiveWriter(path, trade_date, session_id=session_id)
    for tick in ticks:
        writer.write_batch({"000001.SZ": tick})
    return writer.close()


def _broker_config() -> BrokerConfig:
    return BrokerConfig(
        initial_cash=1_000_000,
        slippage_bps=0,
        participation_rate=1,
        allow_t0=True,
    )


def test_loader_filters_signal_source_and_prefers_wall_clock_timestamp(tmp_path):
    path = _events(
        tmp_path / "events.jsonl",
        [
            {
                "event_type": "buy_decision",
                "timestamp": "2026-07-10 09:30:01.000000",
                "stock_code": "000001.SZ",
                "signal_source": "primary",
                "buy_type": "扫板",
                "snapshot": {"time": _ms(9, 29, 59)},
            },
            {
                "event_type": "buy_decision",
                "timestamp": "2026-07-10 09:30:02.000000",
                "stock_code": "000002.SZ",
                "signal_source": "shadow",
                "buy_type": "扫板",
            },
            {"event_type": "limit_up", "stock_code": "000003.SZ"},
        ],
    )

    events, diagnostics = load_replay_events(path, source="primary")

    assert [event.stock_code for event in events] == ["000001.SZ"]
    assert events[0].timestamp_ms == _ms(9, 30, 1)
    assert diagnostics == {
        "loaded": 1,
        "ignored_non_signal": 1,
        "ignored_other_source": 1,
        "legacy_unlabelled": 0,
    }


def test_queue_event_without_known_limit_price_is_rejected(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 09:30:01",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "排板",
            "snapshot": {
                "time": _ms(9, 30, 1), "lastPrice": 10.0,
                "askPrice": 0.0, "bidPrice": 10.0,
                "bidVol": 500, "volume": 1_000,
            },
        }],
    )
    archive_tick = _tick(_ms(9, 30, 2), 10.0, sealed=True, volume=2_000)
    archive_tick.pop("limitUpPrice")
    archive = _archive(tmp_path, [archive_tick])
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events), broker_config=_broker_config()
    ).run(archive)

    assert result["fills"] == []
    assert result["strategy_diagnostics"]["expired_queue_count"] == 1


def test_legacy_unlabelled_events_fail_closed_unless_explicitly_accepted(tmp_path):
    path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 09:30:01",
            "stock_code": "000001.SZ",
            "buy_type": "扫板",
        }],
    )

    with pytest.raises(ValueError, match="cannot be distinguished"):
        load_replay_events(path)

    events, diagnostics = load_replay_events(
        path, accept_legacy_unlabelled=True
    )
    assert events[0].source == "primary"
    assert diagnostics["legacy_unlabelled"] == 1


def test_duplicate_event_identity_is_rejected(tmp_path):
    record = {
        "event_type": "buy_decision",
        "timestamp": "2026-07-10 09:30:01",
        "stock_code": "000001.SZ",
        "signal_source": "primary",
        "buy_type": "扫板",
    }
    path = _events(tmp_path / "events.jsonl", [record, record])

    with pytest.raises(ValueError, match="duplicate strategy event"):
        load_replay_events(path)


def test_sweep_event_executes_on_next_stock_tick(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 09:30:01",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "扫板",
        }],
    )
    archive = _archive(
        tmp_path,
        [
            _tick(_ms(9, 30, 0), 10.0),
            _tick(_ms(9, 30, 1), 10.1),
            _tick(_ms(9, 30, 2), 10.2),
        ],
    )
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=10_000),
        broker_config=_broker_config(),
    ).run(archive)

    assert result["fills"][0]["timestamp_ms"] == _ms(9, 30, 2)
    assert result["fills"][0]["price"] == 10.2


def test_event_replay_uses_live_callback_receipt_clock(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 09:30:01.500000",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "扫板",
        }],
    )
    writer = TickArchiveWriter(
        tmp_path / "received-clock", "20260710", session_id="received-clock"
    )
    writer.write_batch(
        {"000001.SZ": _tick(_ms(9, 30, 2), 10.0)},
        received_at_ns=_ms(9, 30, 1) * 1_000_000,
    )
    writer.write_batch(
        {"000001.SZ": _tick(_ms(9, 30, 3), 10.1)},
        received_at_ns=_ms(9, 30, 2) * 1_000_000,
    )
    writer.write_batch(
        {"000001.SZ": _tick(_ms(9, 30, 4), 10.2)},
        received_at_ns=_ms(9, 30, 3) * 1_000_000,
    )
    archive = writer.close()
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=10_000),
        broker_config=_broker_config(),
    ).run(archive)

    # The exchange timestamp of the first quote is already after the decision,
    # but that callback was received before it.  Signal release therefore waits
    # for the second callback, which is the first quote actually received after
    # the decision and is eligible for execution.
    assert result["fills"][0]["timestamp_ms"] == _ms(9, 30, 3)
    diagnostics = result["strategy_diagnostics"]
    assert diagnostics["received_time_alignment_batches"] == 3
    assert diagnostics["event_time_fallback_batches"] == 0
    assert diagnostics["stale_event_count"] == 0


def test_receipt_clock_requires_strictly_later_callback(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 09:30:01.000500",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "扫板",
        }],
    )
    writer = TickArchiveWriter(
        tmp_path / "same-ms", "20260710", session_id="same-ms"
    )
    writer.write_batch(
        {"000001.SZ": _tick(_ms(9, 30, 1), 10.0)},
        received_at_ns=_ms(9, 30, 1) * 1_000_000,
    )
    writer.write_batch(
        {"000001.SZ": _tick(_ms(9, 30, 2), 10.1)},
        received_at_ns=_ms(9, 30, 2) * 1_000_000,
    )
    archive = writer.close()
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=10_000),
        broker_config=_broker_config(),
    ).run(archive)

    assert result["fills"][0]["timestamp_ms"] == _ms(9, 30, 2)


def test_exchange_clock_far_ahead_cannot_release_a_future_decision(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 09:31:00",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "扫板",
        }],
    )
    writer = TickArchiveWriter(
        tmp_path / "ahead-clock", "20260710", session_id="ahead-clock"
    )
    writer.write_batch(
        {"000001.SZ": _tick(_ms(9, 32, 0), 10.0)},
        received_at_ns=_ms(9, 30, 0) * 1_000_000,
    )
    writer.write_batch(
        {"000001.SZ": _tick(_ms(9, 32, 1), 10.1)},
        received_at_ns=_ms(9, 30, 1) * 1_000_000,
    )
    writer.write_batch(
        {"000001.SZ": _tick(_ms(9, 33, 1), 10.2)},
        received_at_ns=_ms(9, 31, 1) * 1_000_000,
    )
    archive = writer.close()
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=10_000),
        broker_config=_broker_config(),
    ).run(archive)

    assert result["fills"][0]["timestamp_ms"] == _ms(9, 33, 1)
    assert result["strategy_diagnostics"]["received_time_alignment_batches"] == 3


def test_receipt_clock_waits_for_next_tick_of_the_target_stock(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 09:30:01.500000",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "扫板",
        }],
    )
    writer = TickArchiveWriter(
        tmp_path / "other-stock", "20260710", session_id="other-stock"
    )
    writer.write_batch(
        {"000002.SZ": _tick(_ms(9, 30, 2), 20.0)},
        received_at_ns=_ms(9, 30, 2) * 1_000_000,
    )
    writer.write_batch(
        {"000001.SZ": _tick(_ms(9, 30, 3), 10.2)},
        received_at_ns=_ms(9, 30, 3) * 1_000_000,
    )
    archive = writer.close()
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=10_000),
        broker_config=_broker_config(),
    ).run(archive)

    assert result["fills"][0]["stock_code"] == "000001.SZ"
    assert result["fills"][0]["timestamp_ms"] == _ms(9, 30, 3)


def test_prior_day_event_cannot_execute_at_next_day_open(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 14:59:59",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "扫板",
        }],
    )
    next_open = int(
        datetime(2026, 7, 11, 9, 30, tzinfo=CHINA_TZ).timestamp() * 1000
    )
    archive = _archive(
        tmp_path,
        [
            _tick(next_open, 10.5),
            _tick(next_open + 1_000, 10.6),
        ],
        trade_date="20260711",
        session_id="next-day-only",
    )
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=10_000),
        broker_config=_broker_config(),
    ).run(archive)

    assert result["fills"] == []
    assert result["strategy_diagnostics"]["stale_event_count"] == 1


def test_queue_event_requires_complete_trade_through(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 09:30:01",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "排板",
            "snapshot": _tick(
                _ms(9, 30, 1), 11.0, sealed=True, volume=1_000
            ),
        }],
    )
    archive = _archive(
        tmp_path,
        [
            _tick(_ms(9, 30, 1), 11.0, sealed=True, volume=1_000),
            _tick(_ms(9, 30, 2), 11.0, sealed=True, volume=1_509),
            _tick(_ms(9, 30, 3), 11.0, sealed=True, volume=1_510),
        ],
    )
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=11_000),
        broker_config=_broker_config(),
    ).run(archive)

    assert result["fills"][0]["timestamp_ms"] == _ms(9, 30, 3)
    assert result["fills"][0]["price"] == 11.0
    assert result["metrics"]["limit_up_entry_count"] == 1


def test_queue_event_is_cancelled_when_board_opens(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 09:30:01",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "排板",
            "snapshot": _tick(
                _ms(9, 30, 1), 11.0, sealed=True, volume=1_000
            ),
        }],
    )
    archive = _archive(
        tmp_path,
        [
            _tick(_ms(9, 30, 1), 11.0, sealed=True, volume=1_000),
            _tick(_ms(9, 30, 2), 10.9, sealed=False, volume=1_100),
            _tick(_ms(9, 30, 3), 11.0, sealed=True, volume=9_999),
        ],
    )
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=11_000),
        broker_config=_broker_config(),
    ).run(archive)

    assert result["fills"] == []
    assert result["strategy_diagnostics"]["expired_queue_count"] == 1


def test_cancel_event_removes_a_pending_queue(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [
            {
                "event_type": "buy_decision",
                "timestamp": "2026-07-10 09:30:01",
                "stock_code": "000001.SZ",
                "signal_source": "primary",
                "buy_type": "排板",
                "snapshot": _tick(
                    _ms(9, 30, 1), 11.0, sealed=True, volume=1_000
                ),
            },
            {
                "event_type": "cancel_decision",
                "timestamp": "2026-07-10 09:30:02",
                "stock_code": "000001.SZ",
                "signal_source": "primary",
            },
        ],
    )
    archive = _archive(
        tmp_path,
        [
            _tick(_ms(9, 30, 1), 11.0, sealed=True, volume=1_000),
            _tick(_ms(9, 30, 2), 11.0, sealed=True, volume=1_100),
            _tick(_ms(9, 30, 3), 11.0, sealed=True, volume=9_999),
        ],
    )
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=11_000),
        broker_config=_broker_config(),
    ).run(archive)

    assert result["fills"] == []
    assert result["strategy_diagnostics"]["cancelled_queue_count"] == 1


def test_queue_order_expires_before_next_trading_day(tmp_path):
    event_path = _events(
        tmp_path / "events.jsonl",
        [{
            "event_type": "buy_decision",
            "timestamp": "2026-07-10 14:59:59",
            "stock_code": "000001.SZ",
            "signal_source": "primary",
            "buy_type": "排板",
            "snapshot": _tick(
                _ms(14, 59, 59), 11.0, sealed=True, volume=1_000
            ),
        }],
    )
    first = _archive(
        tmp_path,
        [_tick(_ms(14, 59, 59), 11.0, sealed=True, volume=1_000)],
        session_id="day-one",
    )
    next_ms = int(
        datetime(2026, 7, 11, 9, 30, tzinfo=CHINA_TZ).timestamp() * 1000
    )
    second = _archive(
        tmp_path,
        [_tick(next_ms, 11.0, sealed=True, volume=9_999)],
        trade_date="20260711",
        session_id="day-two",
    )
    events, _ = load_replay_events(event_path)

    result = BacktestEngine(
        EventLogReplayStrategy(events, buy_target_value=11_000),
        broker_config=_broker_config(),
    ).run([first, second])

    assert result["fills"] == []
    assert result["strategy_diagnostics"]["expired_queue_count"] == 1
