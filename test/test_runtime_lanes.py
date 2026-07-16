import json
from multiprocessing import Value
from pathlib import Path
from queue import Queue

import pytest

from core.runtime_lanes import (
    freeze_challenger_profile, load_challenger_profile, validate_experiment_id,
)
from data.shared_data import normalize_runtime_namespace
from engine.mirror import LiveMirrorAccount
from engine.paper_broker import BrokerConfig, PaperBroker
from engine.simulator import execute_paper_order, paper_database_path
from engine.tick_processor import (
    _dispatch_ticks_nonblocking, _mark_research_lane_incomplete,
)
from infra.common_enums import OrderType, StockOrderStatusInt


def _shared_data():
    return {
        '持仓状态': {
            '600000.SH': json.dumps({
                '持仓数量': 100, '成本价': 10.0, '市值': 1_000,
            })
        },
        '委托状态': {},
        '股票信息': {'000001.SZ': {'近20日平均振幅': 0.05}},
        '观察名单': {},
        '股票状态信号': {
            '000001.SZ': {
                '下单状态': Value('i', StockOrderStatusInt.NOT_ORDERED),
                '下单时成交量': Value('i', 0),
                '下单时封单量': Value('i', 0),
            }
        },
        '模拟账户': {},
        '撤单次数': Value('i', 0),
    }


def _tick(price=10.0, volume=1_000):
    return {
        'time': 1_000, 'lastPrice': price, 'askPrice': [price],
        'askVol': [10_000], 'bidPrice': [price], 'bidVol': [10_000],
        'volume': volume,
    }


def test_coverage_executor_ignores_portfolio_slot_limit(monkeypatch):
    import engine.simulator as simulator
    monkeypatch.setattr(simulator, 'MAX_HOLDING_COUNT', 1)
    shared = _shared_data()
    broker = PaperBroker(BrokerConfig(
        initial_cash=1_000_000, slippage_bps=0, participation_rate=1, allow_t0=True,
    ))
    order = {
        '委托类型': OrderType.BUY, '买入类型': '扫板',
        '股票代码': '000001.SZ', '委托价格': 10.0,
        '委托数量': 100, '快照': _tick(),
    }

    fill = execute_paper_order(broker, order, shared, lane='coverage')

    assert fill is not None
    assert fill.quantity == 100


def test_mirror_books_only_explicitly_published_orders():
    broker = PaperBroker(BrokerConfig(
        initial_cash=100_000, slippage_bps=0, participation_rate=1, allow_t0=True,
    ))
    mirror = LiveMirrorAccount(broker)
    assert broker.fills == ()

    mirror.submit({
        '委托类型': OrderType.BUY, '买入类型': '扫板',
        '股票代码': '000001.SZ', '委托价格': 10.0,
        '委托数量': 100, '快照': _tick(), 'signal_id': 'accepted-1',
    })

    assert len(broker.fills) == 1
    assert broker.fills[0].signal_id == 'accepted-1'


def test_research_dispatch_never_blocks_when_queue_is_full():
    queue = Queue(maxsize=1)
    queue.put_nowait({'occupied': {}})
    assert _dispatch_ticks_nonblocking({'000001.SZ': _tick()}, queue) is False


def test_research_integrity_alerts_only_on_first_queue_overflow():
    integrity = Value('b', True)

    assert _mark_research_lane_incomplete(integrity) is True
    assert not integrity.value
    assert _mark_research_lane_incomplete(integrity) is False


def test_paper_database_paths_are_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('LIMIT_UP_PAPER_DB', str(tmp_path))
    assert paper_database_path('primary_simulation').name == 'primary_simulation.sqlite3'
    assert paper_database_path('live_mirror').name == 'live_mirror.sqlite3'
    assert paper_database_path('coverage').name == 'coverage.sqlite3'
    assert paper_database_path('baseline', 'exp-1') == (
        tmp_path / 'experiments' / 'exp-1' / 'baseline.sqlite3'
    ).resolve()


def test_challenger_profile_is_frozen_and_drift_is_rejected(tmp_path):
    source = tmp_path / 'profile.json'
    source.write_text(
        json.dumps({'include_exploration_candidates': True}), encoding='utf-8'
    )
    profile = load_challenger_profile(source, 'expanded-v1')
    frozen = freeze_challenger_profile(
        profile, tmp_path / 'paper', {'initial_cash': 1_000_000})
    assert frozen.is_file()

    source.write_text(
        json.dumps({'include_exploration_candidates': False}), encoding='utf-8'
    )
    changed = load_challenger_profile(source, 'expanded-v1')
    with pytest.raises(RuntimeError, match='ExperimentId'):
        freeze_challenger_profile(
            changed, tmp_path / 'paper', {'initial_cash': 1_000_000})


def test_challenger_frozen_execution_assumptions_cannot_drift(tmp_path):
    source = tmp_path / 'profile.json'
    source.write_text('{}', encoding='utf-8')
    profile = load_challenger_profile(source, 'cost-v1')
    freeze_challenger_profile(profile, tmp_path / 'paper', {'slippage_bps': 2.0})

    with pytest.raises(RuntimeError, match='撮合假设'):
        freeze_challenger_profile(
            profile, tmp_path / 'paper', {'slippage_bps': 5.0})


@pytest.mark.parametrize('value', ['', '../escape', 'has space', 'x' * 65])
def test_invalid_experiment_ids_are_rejected(value):
    with pytest.raises(ValueError):
        validate_experiment_id(value)


def test_dotted_experiment_id_is_safe_for_backup_namespace():
    experiment_id = validate_experiment_id('expanded.pool-v1')
    assert normalize_runtime_namespace(
        f'baseline_{experiment_id}', '', '研究备份命名空间'
    ) == 'baseline_expanded.pool-v1'
