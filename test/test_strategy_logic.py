"""Offline regression tests for deterministic strategy rules."""

import json
import sys
import types
from datetime import datetime as RealDatetime
from multiprocessing import Array, Value

import pandas as pd
import pytest

from analysis import pre_market_analysis
from core import decisions
from core.gene_calculator import (
    STRENGTH_SCORE_WEIGHTS,
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
