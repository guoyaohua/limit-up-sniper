import json
from multiprocessing import Value

import pytest

from engine.paper_broker import BrokerConfig, PaperBroker
from engine.simulator import execute_paper_order, update_paper_market
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
