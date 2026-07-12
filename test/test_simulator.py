import json
from multiprocessing import Value

import pytest

from engine.paper_broker import BrokerConfig, PaperBroker
from engine.queue_fill import queued_buy_fill_progress
from engine.simulator import execute_paper_order, update_paper_market
from engine.tick_processor import check_order_successed
from infra.xtconstant_compat import xtconstant
from infra.common_enums import OrderType, StockOrderStatusInt


def _shared_data() -> dict:
    return {
        "持仓状态": {},
        "委托状态": {},
        "股票信息": {"000001.SZ": {"近20日平均振幅": 0.05}},
        "观察名单": {},
        "股票状态信号": {
            "000001.SZ": {
                "下单状态": Value("i", StockOrderStatusInt.NOT_ORDERED),
                "下单时成交量": Value("i", 0),
                "下单时封单量": Value("i", 0),
            }
        },
        "模拟账户": {},
        "撤单次数": Value("i", 0),
    }


def _tick(price: float, timestamp_ms: int = 1_000) -> dict:
    return {
        "time": timestamp_ms,
        "lastPrice": price,
        "askPrice": [price],
        "askVol": [10_000],
        "bidPrice": [price],
        "bidVol": [10_000],
        "volume": 1_000,
    }


def _sealed_tick(price: float, timestamp_ms: int = 1_000) -> dict:
    tick = _tick(price, timestamp_ms)
    tick["askPrice"] = []
    tick["askVol"] = []
    return tick


def test_live_marks_update_shadow_equity_and_position_view():
    shared = _shared_data()
    broker = PaperBroker(
        BrokerConfig(
            initial_cash=100_000,
            slippage_bps=0,
            participation_rate=1,
            allow_t0=True,
        )
    )
    order = {
        "委托类型": OrderType.BUY,
        "买入类型": "扫板",
        "股票代码": "000001.SZ",
        "委托价格": 10.0,
        "委托数量": 1_000,
        # A sweep is submitted while visible ask liquidity still exists.
        "快照": _tick(10.0),
    }

    fill = execute_paper_order(broker, order, shared)
    assert fill is not None

    update_paper_market(
        broker, shared, {"000001.SZ": _tick(11.0, timestamp_ms=2_000)}
    )

    position = json.loads(shared["持仓状态"]["000001.SZ"])
    assert position["市值"] == 11_000
    assert shared["模拟账户"]["equity"] == pytest.approx(100_994.9)
    assert shared["模拟账户"]["unrealized_pnl"] == pytest.approx(994.9)


def _queued_order() -> dict:
    return {
        "下单累计成交手数": 1_000,
        "前方队列手数": 500,
        "本单手数": 10,
    }


def test_queue_fill_requires_trades_through_ahead_queue_and_complete_order():
    pending = _queued_order()

    not_filled = queued_buy_fill_progress(
        pending, {"volume": 1_509}, is_limit_up=True
    )
    filled = queued_buy_fill_progress(
        pending, {"volume": 1_510}, is_limit_up=True
    )

    assert not not_filled.confirmed
    assert filled.confirmed
    assert filled.required_lots == 510


def test_open_limit_never_proves_a_queued_fill():
    progress = queued_buy_fill_progress(
        _queued_order(), {"volume": 2_000}, is_limit_up=False
    )

    assert not progress.confirmed
    assert progress.reason == "limit-up opened"


def test_bid_queue_cancellation_does_not_count_as_traded_volume():
    # bidVol may collapse from 500 lots to zero because other buyers cancelled.
    # It is intentionally absent from the formula; exchange volume only rose 9.
    tick = {"volume": 1_009, "bidVol": [0]}

    progress = queued_buy_fill_progress(_queued_order(), tick, is_limit_up=True)

    assert not progress.confirmed
    assert progress.traded_lots == 9


def test_legacy_queue_order_without_evidence_fails_closed():
    progress = queued_buy_fill_progress(
        {"委托数量": 1_000}, {"volume": 2_000}, is_limit_up=True
    )

    assert not progress.confirmed
    assert progress.reason == "incomplete queue snapshot"


def test_queue_order_is_cancellable_and_fill_is_revalidated():
    shared = _shared_data()
    broker = PaperBroker(
        BrokerConfig(
            initial_cash=100_000,
            slippage_bps=0,
            participation_rate=1,
            allow_t0=True,
        )
    )
    queued = {
        "委托类型": OrderType.BUY,
        "买入类型": "排板",
        "股票代码": "000001.SZ",
        "委托价格": 10.0,
        "委托数量": 1_000,
        "快照": _tick(10.0),
    }

    assert execute_paper_order(broker, queued, shared) is None
    pending = json.loads(shared["委托状态"]["000001.SZ"])[0]
    assert pending["委托类型"] == xtconstant.STOCK_BUY
    assert pending["前方队列手数"] == 10_000
    assert pending["本单手数"] == 10

    premature = dict(queued)
    premature["买入类型"] = "模拟成交"
    premature["快照"] = _sealed_tick(10.0, timestamp_ms=2_000) | {
        "volume": 11_009
    }
    assert execute_paper_order(broker, premature, shared) is None
    assert "000001.SZ" in shared["委托状态"]

    # Even enough cumulative volume cannot fill while an ask is visible:
    # trading at the limit price is not the same as a sealed limit-up book.
    opened = dict(premature)
    opened["快照"] = _tick(10.0, timestamp_ms=2_100) | {
        "volume": 11_010
    }
    assert execute_paper_order(broker, opened, shared) is None

    cancel = {
        "委托类型": OrderType.CANCEL,
        "股票代码": "000001.SZ",
    }
    assert execute_paper_order(broker, cancel, shared) is None
    assert "000001.SZ" not in shared["委托状态"]
    assert shared["撤单次数"].value == 1
    assert broker.positions == {}


def test_open_then_reseal_cannot_resurrect_the_old_queue_position():
    shared = _shared_data()
    shared["强势股票"] = {"000001.SZ": {}}
    pending = _queued_order() | {
        "委托类型": xtconstant.STOCK_BUY,
        "委托数量": 1_000,
    }
    shared["委托状态"]["000001.SZ"] = json.dumps(
        [pending], ensure_ascii=False
    )

    assert not check_order_successed(
        shared, "000001.SZ", {"volume": 2_000}, is_limit_up=False
    )
    invalidated = json.loads(shared["委托状态"]["000001.SZ"])[0]
    assert invalidated["排队已失效"] is True

    # The later reseal has ample volume but the original queue has ceased to
    # exist and must remain unfilled.
    assert not check_order_successed(
        shared, "000001.SZ", {"volume": 9_999}, is_limit_up=True
    )


def test_confirmed_queue_fill_books_original_limit_price():
    shared = _shared_data()
    broker = PaperBroker(
        BrokerConfig(
            initial_cash=100_000,
            slippage_bps=20,
            participation_rate=1,
            allow_t0=True,
        )
    )
    queued = {
        "委托类型": OrderType.BUY,
        "买入类型": "排板",
        "股票代码": "000001.SZ",
        "委托价格": 10.0,
        "委托数量": 1_000,
        "快照": _sealed_tick(10.0),
    }
    execute_paper_order(broker, queued, shared)

    confirmed = dict(queued)
    confirmed["买入类型"] = "模拟成交"
    confirmed["快照"] = _sealed_tick(10.0, timestamp_ms=2_000) | {
        "volume": 11_010,
        "askPrice": [],
        "askVol": [],
    }
    fill = execute_paper_order(broker, confirmed, shared)

    assert fill is not None
    assert fill.quantity == 1_000
    assert fill.price == 10.0
    assert "000001.SZ" not in shared["委托状态"]
