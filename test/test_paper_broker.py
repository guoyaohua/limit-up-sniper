from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.paper_broker import BrokerConfig, PaperBroker


def _timestamp(year: int, month: int, day: int, hour: int = 10) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def _tick(
    timestamp_ms: int,
    *,
    last: float = 10.0,
    asks: list[float] | None = None,
    ask_lots: list[int] | None = None,
    bids: list[float] | None = None,
    bid_lots: list[int] | None = None,
) -> dict:
    return {
        "time": timestamp_ms,
        "lastPrice": last,
        "askPrice": asks if asks is not None else [last + 0.01],
        "askVol": ask_lots if ask_lots is not None else [1_000],
        "bidPrice": bids if bids is not None else [last - 0.01],
        "bidVol": bid_lots if bid_lots is not None else [1_000],
    }


def test_buy_uses_visible_book_vwap_and_all_fees():
    broker = PaperBroker(
        BrokerConfig(
            initial_cash=100_000,
            commission_rate=0.0003,
            minimum_commission=5,
            transfer_fee_rate=0.00001,
            slippage_bps=0,
            participation_rate=1,
        )
    )
    tick = _tick(
        _timestamp(2026, 7, 10),
        asks=[10.0, 10.1],
        ask_lots=[1, 2],
    )

    fill = broker.buy("000001.SZ", 300, tick)

    assert fill is not None
    assert fill.quantity == 300
    assert fill.price == pytest.approx(10.067)
    assert fill.commission == 5
    assert fill.transfer_fee == pytest.approx(fill.gross_amount * 0.00001)
    assert broker.cash == pytest.approx(
        100_000 - fill.gross_amount - fill.commission - fill.transfer_fee
    )


def test_participation_never_fabricates_a_minimum_lot():
    broker = PaperBroker(
        BrokerConfig(participation_rate=0.10, slippage_bps=0)
    )
    tick = _tick(
        _timestamp(2026, 7, 10), asks=[10.0], ask_lots=[5]
    )

    assert broker.buy("000001.SZ", 100, tick) is None
    assert broker.positions == {}


def test_t_plus_one_and_sell_fees_are_enforced():
    broker = PaperBroker(
        BrokerConfig(
            initial_cash=100_000,
            slippage_bps=0,
            participation_rate=1,
        )
    )
    buy_tick = _tick(_timestamp(2026, 7, 10), last=10.0)
    buy = broker.buy("000001.SZ", 1_000, buy_tick)
    assert buy is not None

    assert broker.sell("000001.SZ", 1_000, buy_tick) is None

    sell_tick = _tick(_timestamp(2026, 7, 11), last=11.0)
    sell = broker.sell("000001.SZ", 1_000, sell_tick)
    assert sell is not None
    assert sell.stamp_duty == pytest.approx(sell.gross_amount * 0.0005)
    assert sell.transfer_fee == pytest.approx(sell.gross_amount * 0.00001)
    assert broker.positions == {}


def test_restored_position_cost_is_debited_from_cash():
    broker = PaperBroker(BrokerConfig(initial_cash=100_000))

    broker.restore_position("000001.SZ", 1_000, 10.0, mark_price=10.0)

    snapshot = broker.snapshot()
    assert snapshot["cash"] == 90_000
    assert snapshot["equity"] == 100_000
    assert snapshot["total_return"] == 0


def test_sqlite_state_and_signal_id_are_restart_safe(tmp_path: Path):
    database = tmp_path / "paper.sqlite3"
    config = BrokerConfig(
        initial_cash=100_000, slippage_bps=0, participation_rate=1
    )
    tick = _tick(_timestamp(2026, 7, 10))

    with PaperBroker(config, database_path=database, account_id="test") as broker:
        first = broker.buy("000001.SZ", 100, tick, signal_id="buy-1")
        duplicate = broker.buy("000001.SZ", 100, tick, signal_id="buy-1")
        assert first is not None
        assert duplicate is None

    with PaperBroker(config, database_path=database, account_id="test") as broker:
        assert len(broker.fills) == 1
        assert broker.positions["000001.SZ"]["quantity"] == 100
        assert broker.buy("000001.SZ", 100, tick, signal_id="buy-1") is None
