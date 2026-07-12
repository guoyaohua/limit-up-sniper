from pathlib import Path

import pytest

from data.tick_archive import TickArchiveWriter
from engine.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestSignal,
    wilson_interval,
)
from engine.paper_broker import BrokerConfig


def _tick(
    timestamp_ms: int,
    price: float,
    *,
    sealed: bool = False,
    limit_price: float = 11.0,
) -> dict:
    return {
        "time": timestamp_ms,
        "lastPrice": price,
        "limitUpPrice": limit_price,
        "askPrice": [0.0 if sealed else price],
        "askVol": [0 if sealed else 10_000],
        "bidPrice": [price],
        "bidVol": [10_000],
    }


def _archive(
    tmp_path: Path,
    batches: list[dict],
    *,
    trade_date: str = "20260710",
) -> Path:
    writer = TickArchiveWriter(tmp_path, trade_date, session_id="backtest")
    for batch in batches:
        writer.write_batch(batch)
    return writer.close()


def _broker_config() -> BrokerConfig:
    return BrokerConfig(
        initial_cash=100_000,
        slippage_bps=0,
        participation_rate=1,
        allow_t0=True,
    )


@pytest.mark.parametrize(
    ("successes", "total", "expected"),
    [
        (0, 0, None),
        (0, 1, (0.0, 0.79345069)),
        (1, 1, (0.20654931, 1.0)),
        (0, 10, (0.0, 0.2775328)),
        (10, 10, (0.7224672, 1.0)),
        (50, 100, (0.40383153, 0.59616847)),
    ],
)
def test_wilson_interval_boundaries(successes, total, expected):
    interval = wilson_interval(successes, total)
    if expected is None:
        assert interval is None
        return
    assert interval["lower"] == pytest.approx(expected[0])
    assert interval["upper"] == pytest.approx(expected[1])
    assert interval["successes"] == successes
    assert interval["total"] == total


def test_signal_executes_on_next_stock_tick_without_lookahead(tmp_path: Path):
    archive = _archive(
        tmp_path,
        [
            {"000001.SZ": _tick(1_000, 10.0)},
            {"000002.SZ": _tick(2_000, 20.0, limit_price=22.0)},
            {"000001.SZ": _tick(3_000, 10.5)},
        ],
    )

    class Strategy:
        sent = False

        def on_tick_batch(self, batch, broker):
            if "000001.SZ" in batch.ticks and not self.sent:
                self.sent = True
                return [BacktestSignal("000001.SZ", "BUY", quantity=100)]
            return []

    result = BacktestEngine(Strategy(), broker_config=_broker_config()).run(archive)

    assert result["fills"][0]["timestamp_ms"] == 3_000
    assert result["fills"][0]["price"] == 10.5
    assert result["unexecuted_signal_count"] == 0


def test_maximum_drawdown_includes_initial_equity(tmp_path: Path):
    archive = _archive(
        tmp_path,
        [
            {"000001.SZ": _tick(1_000, 10.0)},
            {"000001.SZ": _tick(2_000, 10.0)},
            {"000001.SZ": _tick(3_000, 5.0)},
        ],
    )

    class Strategy:
        sent = False

        def on_tick_batch(self, batch, broker):
            if not self.sent:
                self.sent = True
                return [BacktestSignal("000001.SZ", "BUY", quantity=10_000)]
            return []

    result = BacktestEngine(
        Strategy(),
        broker_config=_broker_config(),
        config=BacktestConfig(sample_equity_every_batches=1),
    ).run(archive)

    # The broker sizes down one board lot to keep commission and transfer fees
    # within cash, then marks that holding from 10 to 5.
    assert result["metrics"]["maximum_drawdown"] == pytest.approx(0.4953069)


def test_close_at_end_uses_latest_tick_for_each_position(tmp_path: Path):
    archive = _archive(
        tmp_path,
        [
            {
                "000001.SZ": _tick(1_000, 10.0),
                "000002.SZ": _tick(1_000, 20.0, limit_price=22.0),
            },
            {
                "000001.SZ": _tick(2_000, 10.0),
                "000002.SZ": _tick(2_000, 20.0, limit_price=22.0),
            },
            {"000001.SZ": _tick(3_000, 11.0)},
        ],
    )

    class Strategy:
        sent = False

        def on_tick_batch(self, batch, broker):
            if not self.sent:
                self.sent = True
                return [
                    BacktestSignal("000001.SZ", "BUY", quantity=100),
                    BacktestSignal("000002.SZ", "BUY", quantity=100),
                ]
            return []

    result = BacktestEngine(
        Strategy(),
        broker_config=_broker_config(),
        config=BacktestConfig(close_positions_at_end=True),
    ).run(archive)

    assert [fill["side"] for fill in result["fills"]].count("SELL") == 2
    assert result["open_positions"] == {}


def test_limit_up_entries_report_close_seal_rate(tmp_path: Path):
    archive = _archive(
        tmp_path,
        [
            {"000001.SZ": _tick(1_000, 10.9)},
            {"000001.SZ": _tick(2_000, 10.95)},
            {"000001.SZ": _tick(3_000, 11.0, sealed=True)},
            {"000001.SZ": _tick(4_000, 10.8)},
            {"000001.SZ": _tick(5_000, 11.0, sealed=True)},
        ],
    )

    class Strategy:
        sent = False

        def on_tick_batch(self, batch, broker):
            if not self.sent:
                self.sent = True
                return [
                    BacktestSignal(
                        "000001.SZ",
                        "BUY",
                        quantity=100,
                        limit_up_entry=True,
                    )
                ]
            return []

    result = BacktestEngine(Strategy(), broker_config=_broker_config()).run(archive)
    metrics = result["metrics"]

    assert metrics["limit_up_entry_count"] == 1
    assert metrics["sealed_at_least_once_count"] == 1
    assert metrics["broken_after_seal_count"] == 1
    assert metrics["sealed_through_close_count"] == 1
    assert metrics["seal_success_rate"] == 1.0


def test_invalid_archive_is_rejected_by_default(tmp_path: Path):
    archive = _archive(tmp_path, [{"000001.SZ": _tick(1_000, 10.0)}])
    manifest = archive.with_name(archive.name[:-9] + "manifest.json")
    manifest.unlink()

    with pytest.raises(ValueError, match="integrity checks"):
        BacktestEngine(lambda batch, broker: []).run(archive)


def test_pending_signal_expires_at_trading_day_boundary(tmp_path: Path):
    first = _archive(
        tmp_path,
        [{"000001.SZ": _tick(1_000, 10.0)}],
        trade_date="20260710",
    )
    second = _archive(
        tmp_path,
        [{"000001.SZ": _tick(2_000, 11.0)}],
        trade_date="20260711",
    )

    class Strategy:
        sent = False

        def on_tick_batch(self, batch, broker):
            if not self.sent:
                self.sent = True
                return [BacktestSignal("000001.SZ", "BUY", quantity=100)]
            return []

    result = BacktestEngine(Strategy(), broker_config=_broker_config()).run(
        [first, second]
    )

    assert result["fills"] == []
    assert result["expired_signal_count"] == 1
    assert result["unexecuted_signal_count"] == 0


def test_seal_rate_counts_each_entry_and_uses_its_day_close(tmp_path: Path):
    first = _archive(
        tmp_path,
        [
            {"000001.SZ": _tick(1_000, 10.9)},
            {"000001.SZ": _tick(2_000, 10.95)},
            {"000001.SZ": _tick(3_000, 11.0, sealed=True)},
        ],
        trade_date="20260710",
    )
    second = _archive(
        tmp_path,
        [
            {"000001.SZ": _tick(4_000, 10.9)},
            {"000001.SZ": _tick(5_000, 10.95)},
            {"000001.SZ": _tick(6_000, 10.8)},
        ],
        trade_date="20260711",
    )

    class Strategy:
        sent_dates = set()

        def on_tick_batch(self, batch, broker):
            if batch.trade_date not in self.sent_dates:
                self.sent_dates.add(batch.trade_date)
                return [BacktestSignal(
                    "000001.SZ",
                    "BUY",
                    quantity=100,
                    signal_id=f"entry-{batch.trade_date}",
                    limit_up_entry=True,
                )]
            return []

    result = BacktestEngine(Strategy(), broker_config=_broker_config()).run(
        [first, second]
    )
    metrics = result["metrics"]

    assert metrics["limit_up_entry_count"] == 2
    assert metrics["sealed_through_close_count"] == 1
    assert metrics["seal_success_rate"] == 0.5
    assert [entry["sealed_at_close"] for entry in result["limit_up_entries"]] == [
        True,
        False,
    ]
