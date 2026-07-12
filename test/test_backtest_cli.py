import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data.tick_archive import TickArchiveWriter
from scripts import run_backtest


CHINA_TZ = timezone(timedelta(hours=8))


def _ms(second: int) -> int:
    return int(
        datetime(2026, 7, 10, 9, 30, second, tzinfo=CHINA_TZ).timestamp()
        * 1000
    )


def _tick(timestamp_ms: int, price: float) -> dict:
    return {
        "time": timestamp_ms,
        "lastPrice": price,
        "askPrice": [price],
        "askVol": [1_000],
        "bidPrice": [price - 0.01],
        "bidVol": [1_000],
        "volume": 1_000,
    }


def test_cli_replays_recorded_events_to_a_report(monkeypatch, tmp_path: Path):
    writer = TickArchiveWriter(tmp_path / "ticks", "20260710", session_id="cli")
    writer.write_batch({"000001.SZ": _tick(_ms(0), 10.0)})
    writer.write_batch({"000001.SZ": _tick(_ms(1), 10.1)})
    writer.write_batch({"000001.SZ": _tick(_ms(2), 10.2)})
    archive = writer.close()

    event_file = tmp_path / "events.jsonl"
    event_record = {
        "event_type": "buy_decision",
        "timestamp": "2026-07-10 09:30:01",
        "stock_code": "000001.SZ",
        "signal_source": "primary",
        "buy_type": "扫板",
        "reason": "synthetic end-to-end test",
    }
    event_file.write_text(
        json.dumps(event_record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_backtest.py",
            "--ticks",
            str(archive),
            "--events",
            str(event_file),
            "--event-source",
            "primary",
            "--event-buy-target-value",
            "10000",
            "--initial-cash",
            "100000",
            "--slippage-bps",
            "0",
            "--participation-rate",
            "1",
            "--output",
            str(output),
        ],
    )

    assert run_backtest.main() == 0

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["fills"][0]["timestamp_ms"] == _ms(2)
    assert result["fills"][0]["price"] == 10.2
    assert result["event_log_diagnostics"]["loaded"] == 1
    assert result["metrics"]["fill_count"] == 1
