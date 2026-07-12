"""Offline regression tests for deterministic strategy rules."""

import json
import sys
import time
import types
from datetime import datetime as RealDatetime
from multiprocessing import Array, Value

import pandas as pd
import pytest

from analysis import pre_market_analysis
from core import decisions
from engine import tick_processor
from core.gene_calculator import (
    STRENGTH_SCORE_WEIGHTS,
    calculate_stock_gene,
    calculate_strength_scores,
)
from core.interpolation import (
    interpolate_seal_threshold,
    interpolate_sector_requirements,
)


def _strength_frame():
    data = {'股票代码': ['LOW', 'MID', 'HIGH']}
    for factor in STRENGTH_SCORE_WEIGHTS:
        data[factor] = [1.0, 2.0, 3.0]
    return pd.DataFrame(data)


def test_strength_score_rewards_better_positive_factors():
    raw = _strength_frame()

    scored = calculate_strength_scores(raw)

    assert sum(STRENGTH_SCORE_WEIGHTS.values()) == pytest.approx(1.0)
    assert list(scored.sort_values('涨停基因打分')['股票代码']) == [
        'LOW', 'MID', 'HIGH'
    ]
    assert scored.loc[2, '涨停基因打分'] > scored.loc[0, '涨停基因打分']
    assert '涨停基因打分' not in raw.columns


def test_next_day_gene_features_only_enter_after_they_are_observable():
    """A t+1 outcome must not alter the feature exposed on event day t."""
    frame = pd.DataFrame({
        '股票代码': ['000001.SZ'] * 4,
        '开盘价': [10.0, 11.0, 11.5, 12.0],
        '最高价': [11.0, 11.5, 12.0, 12.5],
        '最低价': [9.9, 10.8, 11.3, 11.8],
        '收盘价': [11.0, 11.2, 11.8, 12.2],
        '昨收': [10.0, 11.0, 11.2, 11.8],
        '涨停': [True, False, False, False],
        '炸板': [False, False, False, False],
    })

    baseline = calculate_stock_gene(frame, N=250)
    changed = frame.copy()
    # This is the next-day open/close for the event at row 0. It is not known
    # on row 0, but must be available from row 1 onwards.
    changed.loc[1, ['开盘价', '收盘价']] = [13.0, 13.2]
    recalculated = calculate_stock_gene(changed, N=250)

    feature_columns = [
        '涨停次日开盘平均溢价',
        '涨停次日收盘平均溢价',
        '涨停次日开盘溢价超5%比例',
        '涨停次日收盘红盘率',
    ]
    for column in feature_columns:
        assert pd.isna(baseline.loc[0, column])
        assert pd.isna(recalculated.loc[0, column])
    assert baseline.loc[1, '涨停次日开盘平均溢价'] != pytest.approx(
        recalculated.loc[1, '涨停次日开盘平均溢价'])


def test_unsettled_latest_limit_up_is_not_counted_as_failed_premium():
    frame = pd.DataFrame({
        '股票代码': ['000001.SZ'] * 3,
        '开盘价': [10.0, 11.6, 11.5],
        '最高价': [11.0, 11.8, 12.65],
        '最低价': [9.9, 11.4, 11.4],
        '收盘价': [11.0, 11.7, 12.65],
        '昨收': [10.0, 11.0, 11.5],
        '涨停': [True, False, True],
        '炸板': [False, False, False],
    })

    result = calculate_stock_gene(frame, N=250)

    # Row 2 knows the successful outcome of row 0. Its own (still unknown)
    # next-day outcome must not dilute the observed success ratio.
    assert result.loc[2, '涨停次日开盘溢价超5%比例'] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ('sentiment', 'expected'),
    [(1.0, 2e8), (2.5, 1.5e8), (4.0, 1e8), (5.5, 8e7),
     (7.0, 5e7), (8.0, 3e7), (10.0, 2e7)],
)
def test_seal_threshold_preserves_documented_anchors(sentiment, expected):
    assert interpolate_seal_threshold(sentiment) == pytest.approx(expected)


def test_interpolated_requirements_relax_as_sentiment_improves():
    scores = [2.5, 4.0, 5.5, 7.0, 8.0, 10.0]
    requirements = [interpolate_sector_requirements(score) for score in scores]

    assert all(
        later[0] <= earlier[0] and later[1] <= earlier[1]
        for earlier, later in zip(requirements, requirements[1:])
    )


def _timestamp_ms(hour, minute, second=0):
    return int(RealDatetime(2026, 7, 10, hour, minute, second).timestamp() * 1000)


@pytest.mark.parametrize(
    ('clock', 'expected'),
    [((9, 30), 1.0), ((10, 30), 60.0), ((11, 30), 120.0),
     ((12, 0), 120.0), ((13, 30), 150.0), ((15, 0), 240.0)],
)
def test_elapsed_trading_minutes_excludes_lunch_break(clock, expected):
    timestamp = _timestamp_ms(*clock)

    assert decisions._elapsed_trading_minutes(timestamp) == pytest.approx(expected)


def test_intraday_volume_ratio_normalises_early_cumulative_volume():
    average_daily_volume = 2_400_000

    morning_ratio = decisions.calculate_intraday_volume_ratio(
        {'time': _timestamp_ms(10, 30), 'volume': 600_000},
        average_daily_volume,
    )
    close_ratio = decisions.calculate_intraday_volume_ratio(
        {'time': _timestamp_ms(15, 0), 'volume': 2_400_000},
        average_daily_volume,
    )

    assert morning_ratio == pytest.approx(1.0)
    assert close_ratio == pytest.approx(1.0)


def test_sweep_capital_uses_ask_depth_not_bid_depth():
    tick = {
        'askPrice': [10.98, 10.99, 11.00, 11.01, 11.02],
        'askVol': [10, 20, 30, 40, 50],
        'bidPrice': [10.97, 10.96, 10.95, 10.94, 10.93],
        'bidVol': [100_000] * 5,
    }

    required = decisions.calculate_limit_up_sweep_capital(tick, 11.00)

    assert required == pytest.approx((10.98 * 10 + 10.99 * 20 + 11.00 * 30) * 100)


def test_realtime_signal_freshness_is_fail_closed():
    now = time.time()

    assert decisions._timestamp_is_fresh({}, '更新时间', 60, now=now) is False
    assert decisions._timestamp_is_fresh(
        {'更新时间': Value('d', now - 30)}, '更新时间', 60, now=now
    ) is True
    assert decisions._timestamp_is_fresh(
        {'更新时间': Value('d', now - 61)}, '更新时间', 60, now=now
    ) is False


def test_tick_dispatch_keeps_each_stock_on_one_fifo_partition():
    class FakeQueue:
        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

    queues = [FakeQueue() for _ in range(4)]
    first = {
        '000001.SZ': {'time': 1},
        '600000.SH': {'time': 1},
    }
    second = {
        '000001.SZ': {'time': 2},
        '600000.SH': {'time': 2},
    }

    tick_processor._dispatch_ticks(first, queues)
    tick_processor._dispatch_ticks(second, queues)

    for stock_code in first:
        partition = tick_processor._queue_partition(stock_code, len(queues))
        stock_times = [
            payload[stock_code]['time']
            for payload in queues[partition].items
            if stock_code in payload
        ]
        assert stock_times == [1, 2]


def test_pre_market_result_is_bounded_and_tolerates_bad_model_output():
    parsed = {
        'market_outlook': '  分歧  ',
        'priority_sectors': [
            {'sector': 'AI', 'weight': 1.5},
            {'sector': '机器人', 'weight': '-0.2'},
            {'sector': '坏权重', 'weight': 'not-a-number'},
            {'sector': 'NaN', 'weight': float('nan')},
            'invalid',
            {'sector': '超过上限', 'weight': 0.8},
        ],
        'avoid_sectors': [
            {'sector': '退潮'}, '高位股', None, {'sector': '超过上限'}
        ],
        'key_stocks': ['000001.SZ', '600000', 'bad-code', 1],
    }

    result = pre_market_analysis._normalise_llm_result(parsed)

    assert result == {
        'market_outlook': '分歧',
        'priority_sectors': {'AI': 1.0, '机器人': 0.0},
        'avoid_sectors': ['退潮', '高位股'],
        'key_stocks': ['000001', '600000'],
    }


def test_pre_market_parser_rejects_non_object_json():
    result = pre_market_analysis._parse_llm_response('[1, 2, 3]')
    assert result['priority_sectors'] == []
    assert result['market_outlook'] == '未知'


def test_pre_market_gene_path_uses_project_root_and_preserves_leading_zero(
        monkeypatch, tmp_path):
    gene_dir = tmp_path / 'output' / '涨停基因'
    gene_dir.mkdir(parents=True)
    pd.DataFrame({
        '股票代码': ['000001', '600000'],
        '股票名称': ['平安银行', '浦发银行'],
        '涨停': [True, False],
    }).to_csv(gene_dir / '涨停基因_20260710.csv', index=False)
    monkeypatch.setattr(pre_market_analysis, 'ROOT_DIR', str(tmp_path))

    result = pre_market_analysis._get_yesterday_limit_up_stocks()

    assert result == '000001 平安银行'


def test_pre_market_llm_client_receives_strategy_timeout(monkeypatch):
    calls = {}

    class FakeDashScopeClient:
        def __init__(self, **kwargs):
            calls['init'] = kwargs

        def chat(self, prompt, **kwargs):
            calls['chat'] = (prompt, kwargs)
            return '{"market_outlook": "分歧"}'

    package = types.ModuleType('llm_client')
    package.__path__ = []
    package.DashScopeOpenAIClient = FakeDashScopeClient
    config_module = types.ModuleType('llm_client.config')
    config_module.DASHSCOPE_TEXT_MODELS = ['test-model']
    monkeypatch.setitem(sys.modules, 'llm_client', package)
    monkeypatch.setitem(sys.modules, 'llm_client.config', config_module)

    response = pre_market_analysis._call_llm('prompt')

    assert response.startswith('{')
    assert calls['init'] == {
        'model': 'test-model',
        'timeout': pre_market_analysis.LLM_TIMEOUT,
        'max_retries': 0,
    }
    assert calls['chat'][1]['stream'] is False


def test_take_profit_catches_up_all_crossed_tiers(monkeypatch):
    class FixedDatetime:
        @classmethod
        def now(cls):
            return RealDatetime(2026, 7, 10, 10, 0, 0)

    monkeypatch.setattr(decisions, 'datetime', FixedDatetime)

    stock_code = '000001.SZ'
    stock_status = {
        '前一价格': Value('d', 112.0),
        '止盈止损价格列表': Array('d', [0.0] * 10),
        '目标剩余仓位': Array('i', [0] * 10),
        '止盈_5pct': Value('i', 0),
        '止盈_8pct': Value('i', 0),
        '止盈_10pct': Value('i', 0),
    }
    holding_status = {
        stock_code: json.dumps({
            '成本价': 100.0,
            '持仓数量': 1000,
            '可用数量': 1000,
        })
    }
    order = {}

    should_sell = decisions.should_sell(
        shared_data={},
        stock_code=stock_code,
        tick_data={
            'bidPrice': [111.0],
            'bidVol': [100],
            'lastPrice': 111.0,
        },
        is_down_limit=False,
        is_near_limit_up=False,
        is_limit_up=False,
        down_limit_price=90.0,
        stock_status=stock_status,
        stock_info={'股票名称': '测试股票'},
        holding_status=holding_status,
        pre_market_holdings=[stock_code],
        order=order,
    )

    assert should_sell is True
    # A-share sell orders are rounded down to a 100-share board lot.
    assert order['剩余仓位'] == 300
    assert order['止盈收益率'] == pytest.approx(0.11)
    assert stock_status['止盈_5pct'].value == 1
    assert stock_status['止盈_8pct'].value == 1
    assert stock_status['止盈_10pct'].value == 1
