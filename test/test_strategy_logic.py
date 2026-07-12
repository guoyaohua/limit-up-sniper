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


class _HotStock:
    def __init__(self, code, rank, concepts=None):
        self.code = code
        self.rank = rank
        self.name = f'股票{code}'
        self.change_percent = 1.23
        self.concept_tags = concepts or []
        self.popularity_tag = '持续上榜'


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
        'watch_sectors': ['低空经济'],
        'avoid_sectors': [
            {'sector': '退潮'}, '高位股', None, {'sector': '超过上限'}
        ],
        'key_stocks': ['000001.SZ', '600000', 'bad-code', 1],
        'first_board_candidates': [{
            'code': '000001.SZ',
            'confidence': 0.90,
            'sector': 'AI',
            'evidence_sources': ['1小时热榜', '涨停基因池'],
            'reason': '多源共振',
            'risks': '板块转弱',
        }, {
            'code': '600000',
            'confidence': 0.70,
            'evidence_sources': ['24小时热榜'],
        }],
    }

    result = pre_market_analysis._normalise_llm_result(parsed)

    assert result == {
        'market_outlook': '分歧',
        'priority_sectors': {
            'AI': 1.0, '机器人': 0.0, '超过上限': 0.8
        },
        'watch_sectors': ['低空经济'],
        'avoid_sectors': ['退潮', '高位股', '超过上限'],
        'key_stocks': ['000001', '600000'],
        'first_board_candidates': [{
            'code': '000001',
            'confidence': 0.9,
            'tier': 'core',
            'sector': 'AI',
            'evidence_sources': ['1小时热榜', '涨停基因池'],
            'reason': '多源共振',
            'risks': '板块转弱',
        }, {
            'code': '600000',
            'confidence': 0.7,
            'tier': 'watch',
            'sector': '',
            'evidence_sources': ['24小时热榜'],
            'reason': '',
            'risks': '',
        }],
    }


def test_pre_market_parser_rejects_non_object_json():
    result = pre_market_analysis._parse_llm_response('[1, 2, 3]')
    assert result['priority_sectors'] == []
    assert result['market_outlook'] == '未知'


def test_pre_market_ths_fetch_uses_real_hot_stock_fields(monkeypatch):
    class FakeScraper:
        def get_hot_stocks_1h(self, limit):
            assert limit == pre_market_analysis.HOT_STOCK_LIMIT
            return [_HotStock('000001', 1, ['银行'])]

        def get_hot_stocks_24h(self, limit):
            return [_HotStock('600000', 2, ['银行'])]

        def get_hot_concept_sectors(self, limit):
            return []

        def get_hot_industry_sectors(self, limit):
            return []

    scraper_module = types.ModuleType('ths_scraper.scraper')
    scraper_module.THSHotSpotScraper = FakeScraper
    monkeypatch.setitem(sys.modules, 'ths_scraper.scraper', scraper_module)

    result = pre_market_analysis._fetch_ths_data()

    assert '000001' in result['hot_stocks_1h']
    assert '概念:银行' in result['hot_stocks_1h']
    assert len(result['hot_stocks_1h_raw']) == 1


def test_pre_market_candidate_tier_uses_verified_local_sources():
    result = pre_market_analysis._normalise_llm_result({
        'priority_sectors': [{'sector': 'AI', 'weight': 0.9}],
        'watch_sectors': ['机器人', '幻觉板块'],
        'first_board_candidates': [{
            'code': '000001',
            'confidence': 0.95,
            # Model tries to duplicate a single local source.
            'evidence_sources': ['1小时热榜', '1小时热榜'],
        }, {
            'code': '600000',
            'confidence': 0.82,
            'evidence_sources': ['伪造来源'],
        }, {
            'code': '999999',
            'confidence': 0.99,
            'evidence_sources': ['1小时热榜', '涨停基因池'],
        }],
    })

    filtered = pre_market_analysis._filter_candidates_to_evidence(
        result,
        {
            '000001': {'1小时热榜'},
            '600000': {'24小时热榜', '涨停基因池'},
        },
        {'AI', '机器人'},
    )

    assert filtered['watch_sectors'] == ['机器人']
    assert filtered['first_board_candidates'][0]['tier'] == 'watch'
    assert filtered['first_board_candidates'][1]['tier'] == 'core'
    assert filtered['first_board_candidates'][1]['evidence_sources'] == [
        '24小时热榜', '涨停基因池'
    ]
    assert filtered['key_stocks'] == ['600000']


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


def test_pre_market_candidate_pool_is_ranked_and_broad(monkeypatch, tmp_path):
    strong_dir = tmp_path / 'output' / '强势股票'
    strong_dir.mkdir(parents=True)
    pd.DataFrame({
        '股票代码': ['000001', '600000', '300001'],
        '股票名称': ['甲', '乙', '丙'],
        '涨停基因打分': [70.0, 95.0, 80.0],
        '首板封板率': [0.8, 0.9, 0.85],
        '首板次日收盘红盘率': [0.6, 0.7, 0.65],
    }).to_csv(strong_dir / '强势股票_20260710.csv', index=False)
    monkeypatch.setattr(pre_market_analysis, 'ROOT_DIR', str(tmp_path))

    pool = pre_market_analysis._get_first_board_candidate_pool()

    assert [item['code'] for item in pool] == [
        '600000', '300001', '000001'
    ]
    assert pool[0]['rank'] == 1
    assert pool[0]['seal_rate'] == pytest.approx(0.9)


def test_candidate_evidence_merges_hot_lists_and_gene_pool():
    text, sources, sectors = pre_market_analysis._build_candidate_evidence(
        {
            'hot_stocks_1h_raw': [_HotStock('000001', 1, ['AI'])],
            'hot_stocks_24h_raw': [
                _HotStock('000001', 3, ['机器人']),
                _HotStock('600000', 2, ['银行']),
            ],
            'hot_concept_sectors_raw': [],
            'hot_industry_sectors_raw': [],
        },
        [{
            'code': '000001', 'name': '甲', 'rank': 2,
            'gene_score': 88.0, 'seal_rate': 0.85,
            'next_day_red_rate': 0.7,
        }],
    )

    assert sources['000001'] == {
        '1小时热榜', '24小时热榜', '涨停基因池'
    }
    assert sources['600000'] == {'24小时热榜'}
    assert {'AI', '机器人', '银行'} <= sectors
    assert '历史首板封板率=85.0%' in text


def test_exploration_candidates_are_local_market_codes_only():
    result = {
        'first_board_candidates': [
            {'code': '000001', 'tier': 'core'},
            {'code': '600000', 'tier': 'watch'},
            {'code': '999999', 'tier': 'watch'},
            {'code': 'bad', 'tier': 'core'},
        ]
    }

    codes = pre_market_analysis.get_exploration_candidate_codes(
        result, ['000001.SZ', '600000.SH'])

    assert codes == ['000001.SZ', '600000.SH']


def test_pre_market_v2_prompt_has_complete_placeholders():
    template = pre_market_analysis._load_prompt_template()

    rendered = template.format(
        current_time='2026-07-12 09:15',
        hot_stocks_1h='one-hour',
        hot_stocks_24h='day',
        hot_concept_sectors='concepts',
        hot_industry_sectors='industries',
        yesterday_limit_up_stocks='yesterday',
        first_board_candidate_evidence='candidates',
    )

    assert 'one-hour' in rendered
    assert 'candidates' in rendered
    assert 'first_board_candidates' in rendered


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
