"""
盘前 LLM 板块预判模块（U7升级）

在 9:15-9:25 运行，利用 AI 热股策略的数据源和 LLM 能力，
输出今日优先打板的板块列表和权重。

数据输入：
- 同花顺 1H 热股榜（捕捉新发酵题材）
- 同花顺 24H 热股榜（盘前仍在更新）
- 同花顺概念板块热度排名
- 同花顺行业板块热度排名
- 昨日涨停列表（从涨停基因数据中获取）
- 涨停基因强势池（扩大首板候选覆盖）

输出：
- 优先板块字典 {板块名: 权重0-1}
- 风险板块列表
- 市场环境简评
- 分层首板候选（核心/观察）
"""

import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 添加 deps 目录到 sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPS_DIR = os.path.join(ROOT_DIR, 'deps', 'ai_hotspot_trader')
if DEPS_DIR not in sys.path:
    sys.path.insert(0, DEPS_DIR)

# Prompt 模板路径
PROMPT_TEMPLATE_PATH = os.path.join(ROOT_DIR, 'prompts', 'pre_market_sector_v2.md')

# LLM 超时（秒）
LLM_TIMEOUT = 60
LLM_MAX_TOKENS = 8192

# 盘前只做候选发现，盘中盘口、资金流和情绪风控仍是最终买入门槛。扩大
# 数据覆盖时按核心/观察两层输出，避免把更多输入直接等价成更多交易。
HOT_STOCK_LIMIT = 100
HOT_SECTOR_LIMIT = 50
FIRST_BOARD_POOL_LIMIT = 500
YESTERDAY_LIMIT_UP_LIMIT = 100
CANDIDATE_EVIDENCE_LIMIT = 260
MAX_PRIORITY_SECTORS = 8
MAX_WATCH_SECTORS = 12
MAX_AVOID_SECTORS = 6
MAX_CORE_CANDIDATES = 12
MAX_FIRST_BOARD_CANDIDATES = 30
CORE_CANDIDATE_MIN_CONFIDENCE = 0.78

_SOURCE_LABELS = {
    'hot_1h': '1小时热榜',
    'hot_24h': '24小时热榜',
    'gene_pool': '涨停基因池',
}


def _load_prompt_template() -> str:
    """加载 Prompt 模板"""
    with open(PROMPT_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def _fetch_ths_data() -> dict:
    """获取同花顺热股榜和板块热度数据"""
    try:
        from ths_scraper.scraper import THSHotSpotScraper
        scraper = THSHotSpotScraper()

        hot_stocks_1h = scraper.get_hot_stocks_1h(limit=HOT_STOCK_LIMIT)
        hot_stocks_24h = scraper.get_hot_stocks_24h(limit=HOT_STOCK_LIMIT)
        hot_concept_sectors = scraper.get_hot_concept_sectors(
            limit=HOT_SECTOR_LIMIT)
        hot_industry_sectors = scraper.get_hot_industry_sectors(
            limit=HOT_SECTOR_LIMIT)

        # 格式化为文本
        def format_stocks(stocks):
            if not stocks:
                return '数据不可用'
            return '\n'.join(
                f"{i + 1}. {s.name}({s.code}) - 涨跌幅:{s.change_percent:.2f}% "
                f"- 概念:{','.join(s.concept_tags[:5]) if s.concept_tags else '无'} "
                f"- 标签:{s.popularity_tag or '无'}"
                for i, s in enumerate(stocks)
            )

        stocks_1h_text = format_stocks(hot_stocks_1h)
        stocks_24h_text = format_stocks(hot_stocks_24h)

        concept_text = '\n'.join([
            f"{i+1}. {s.name} - 涨跌幅:{s.change_percent:.2f}%"
            for i, s in enumerate(hot_concept_sectors)
        ]) if hot_concept_sectors else '数据不可用'

        industry_text = '\n'.join([
            f"{i+1}. {s.name} - 涨跌幅:{s.change_percent:.2f}%"
            for i, s in enumerate(hot_industry_sectors)
        ]) if hot_industry_sectors else '数据不可用'

        return {
            'hot_stocks_1h': stocks_1h_text,
            'hot_stocks_24h': stocks_24h_text,
            'hot_concept_sectors': concept_text,
            'hot_industry_sectors': industry_text,
            'hot_stocks_1h_raw': hot_stocks_1h,
            'hot_stocks_24h_raw': hot_stocks_24h,
            'hot_concept_sectors_raw': hot_concept_sectors,
            'hot_industry_sectors_raw': hot_industry_sectors,
        }
    except Exception as e:
        logger.error(f'获取同花顺数据失败: {e}')
        return {
            'hot_stocks_1h': '数据获取失败',
            'hot_stocks_24h': '数据获取失败',
            'hot_concept_sectors': '数据获取失败',
            'hot_industry_sectors': '数据获取失败',
            'hot_stocks_1h_raw': [],
            'hot_stocks_24h_raw': [],
            'hot_concept_sectors_raw': [],
            'hot_industry_sectors_raw': [],
        }


def _latest_csv(directory: Path) -> Path | None:
    """Return the newest dated CSV without depending on the process cwd."""
    if not directory.exists():
        return None
    files = sorted(directory.glob('*.csv'), reverse=True)
    return files[0] if files else None


def _normalise_stock_code(value) -> str:
    """Normalise model/data-source codes to a six digit A-share code."""
    if not isinstance(value, str):
        return ''
    code = value.strip().split('.')[0]
    if code.endswith('.0'):
        code = code[:-2]
    return code.zfill(6) if code.isdigit() and len(code) <= 6 else ''


def _get_yesterday_limit_up_stocks() -> str:
    """从涨停基因CSV中获取完整的昨日涨停列表。"""
    try:
        import pandas as pd
        # 收盘时保存的涨停清单比基因 CSV 更直接；存在时优先使用，
        # 并保留全量（上限仅用于控制 prompt 大小）。
        limit_dir = Path(ROOT_DIR) / 'output' / '涨停列表'
        text_files = sorted(limit_dir.glob('涨停_*.txt'), reverse=True)
        if text_files:
            codes = []
            for raw_line in text_files[0].read_text(encoding='utf-8').splitlines():
                code = _normalise_stock_code(raw_line)
                if code and code not in codes:
                    codes.append(code)
            if codes:
                return '\n'.join(codes[:YESTERDAY_LIMIT_UP_LIMIT])

        # 尝试读取最近的涨停基因文件
        latest = _latest_csv(Path(ROOT_DIR) / 'output' / '涨停基因')
        if latest is None:
            return '数据不可用'

        df = pd.read_csv(
            latest,
            dtype={'股票代码': str},
        )
        if '涨停' not in df.columns:
            return '数据不可用'
        limit_up = df[df['涨停'].fillna(False).astype(bool)]
        if limit_up.empty:
            return '昨日无涨停股票'

        result = []
        for _, row in limit_up.head(YESTERDAY_LIMIT_UP_LIMIT).iterrows():
            code = _normalise_stock_code(row.get('股票代码', ''))
            name = row.get('股票名称', '')
            sectors = row.get('所属概念', row.get('概念板块', ''))
            extra = f" - 板块:{sectors}" if isinstance(
                sectors, str) and sectors.strip() else ''
            if code:
                result.append(f"{code} {name}{extra}")
        return '\n'.join(result)
    except Exception as e:
        logger.error(f'获取昨日涨停列表失败: {e}')
        return '数据获取失败'


def _get_first_board_candidate_pool() -> list[dict]:
    """Load a broad, ranked pool for first-board discovery.

    The strong-stock file has already applied liquidity and historical-gene
    filters.  Keeping more than the final LLM output here improves recall while
    leaving intraday execution filters unchanged.
    """
    try:
        import pandas as pd

        latest = _latest_csv(Path(ROOT_DIR) / 'output' / '强势股票')
        if latest is None:
            return []
        df = pd.read_csv(latest, dtype={'股票代码': str})
        if '股票代码' not in df.columns:
            return []
        if '涨停基因打分' in df.columns:
            df = df.sort_values('涨停基因打分', ascending=False)

        candidates = []
        for rank, (_, row) in enumerate(
                df.head(FIRST_BOARD_POOL_LIMIT).iterrows(), 1):
            code = _normalise_stock_code(row.get('股票代码'))
            if not code:
                continue
            candidates.append({
                'code': code,
                'name': str(row.get('股票名称', '') or ''),
                'rank': rank,
                'gene_score': _finite_float(row.get('涨停基因打分')),
                'seal_rate': _finite_float(row.get('首板封板率')),
                'next_day_red_rate': _finite_float(
                    row.get('首板次日收盘红盘率')),
            })
        return candidates
    except Exception as exc:
        logger.error(f'获取盘前首板候选池失败: {exc}')
        return []


def _finite_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _build_candidate_evidence(ths_data: dict,
                              gene_candidates: list[dict]) -> tuple[str, dict[str, set[str]], set[str]]:
    """Merge independent discovery sources and describe their evidence."""
    evidence = defaultdict(lambda: {
        'name': '', 'sources': set(), 'concepts': set(), 'ranks': {},
        'gene_score': None, 'seal_rate': None, 'next_day_red_rate': None,
    })

    for source_key, data_key in (('hot_1h', 'hot_stocks_1h_raw'),
                                 ('hot_24h', 'hot_stocks_24h_raw')):
        for fallback_rank, stock in enumerate(ths_data.get(data_key, []), 1):
            code = _normalise_stock_code(getattr(stock, 'code', ''))
            if not code:
                continue
            item = evidence[code]
            item['name'] = getattr(stock, 'name', '') or item['name']
            item['sources'].add(source_key)
            item['concepts'].update(getattr(stock, 'concept_tags', []) or [])
            try:
                rank = int(getattr(stock, 'rank', fallback_rank))
            except (TypeError, ValueError):
                rank = fallback_rank
            item['ranks'][source_key] = rank

    for candidate in gene_candidates:
        code = candidate['code']
        item = evidence[code]
        item['name'] = candidate.get('name') or item['name']
        item['sources'].add('gene_pool')
        item['ranks']['gene_pool'] = candidate['rank']
        for key in ('gene_score', 'seal_rate', 'next_day_red_rate'):
            item[key] = candidate.get(key)

    def evidence_score(item):
        source_bonus = len(item['sources']) * 1000
        rank_bonus = sum(max(0, 200 - rank) for rank in item['ranks'].values())
        return source_bonus + rank_bonus

    ranked = sorted(evidence.items(), key=lambda pair: evidence_score(pair[1]),
                    reverse=True)[:CANDIDATE_EVIDENCE_LIMIT]
    lines = []
    evidence_sources = {}
    sector_names = set()
    for code, item in ranked:
        source_labels = {_SOURCE_LABELS[s] for s in item['sources']}
        evidence_sources[code] = source_labels
        sources = ','.join(sorted(source_labels))
        concepts = ','.join(sorted(item['concepts'])[:6]) or '无'
        sector_names.update(item['concepts'])
        metrics = []
        if item['gene_score'] is not None:
            metrics.append(f"基因分={item['gene_score']:.1f}")
        if item['seal_rate'] is not None:
            metrics.append(f"历史首板封板率={item['seal_rate']:.1%}")
        if item['next_day_red_rate'] is not None:
            metrics.append(f"首板次日红盘率={item['next_day_red_rate']:.1%}")
        lines.append(
            f"{code} {item['name']} | 来源:{sources} | 概念:{concepts}" +
            (f" | {';'.join(metrics)}" if metrics else '')
        )
    for data_key in ('hot_concept_sectors_raw', 'hot_industry_sectors_raw'):
        sector_names.update(
            getattr(sector, 'name', '')
            for sector in ths_data.get(data_key, [])
            if getattr(sector, 'name', '')
        )
    return (
        '\n'.join(lines) if lines else '数据不可用',
        evidence_sources,
        sector_names,
    )


def _call_llm(prompt: str) -> str:
    """调用 LLM 获取板块预判"""
    started_at = time.monotonic()
    try:
        from llm_client import DashScopeOpenAIClient
        from llm_client.config import DASHSCOPE_TEXT_MODELS

        # 选择第一个可用的文本模型
        model = DASHSCOPE_TEXT_MODELS[0] if DASHSCOPE_TEXT_MODELS else 'qwen3.5-plus'

        client = DashScopeOpenAIClient(
            model=model,
            timeout=LLM_TIMEOUT,
            max_retries=0,
        )
        return client.chat(
            prompt,
            stream=False,
            max_tokens=LLM_MAX_TOKENS,
            temperature=0.1,
        )
    except Exception as e:
        logger.error(f'LLM 调用失败: {e}')
        # 退回到 Azure
        try:
            remaining_timeout = LLM_TIMEOUT - (time.monotonic() - started_at)
            if remaining_timeout <= 0:
                logger.error('LLM 调用已耗尽总超时预算，跳过 Azure 备用模型')
                return ''
            from llm_client import AzureOpenAIClient
            from llm_client.config import AZURE_TEXT_MODELS
            model = AZURE_TEXT_MODELS[0] if AZURE_TEXT_MODELS else 'DeepSeek-V3.2-Speciale'
            client = AzureOpenAIClient(
                model=model,
                timeout=remaining_timeout,
                max_retries=0,
            )
            return client.chat(
                prompt,
                stream=False,
                max_tokens=LLM_MAX_TOKENS,
                temperature=0.1,
            )
        except Exception as e2:
            logger.error(f'Azure LLM 也调用失败: {e2}')
            return ''


def _parse_llm_response(response: str) -> dict:
    """解析 LLM 返回的 JSON"""
    try:
        # 尝试从 markdown code block 中提取 JSON
        if '```json' in response:
            json_str = response.split('```json')[1].split('```')[0].strip()
        elif '```' in response:
            json_str = response.split('```')[1].split('```')[0].strip()
        else:
            json_str = response.strip()

        result = json.loads(json_str)
        if not isinstance(result, dict):
            raise ValueError('LLM 响应顶层必须是 JSON 对象')

        # 验证结构
        if 'priority_sectors' not in result:
            result['priority_sectors'] = []
        if 'watch_sectors' not in result:
            result['watch_sectors'] = []
        if 'avoid_sectors' not in result:
            result['avoid_sectors'] = []
        if 'market_outlook' not in result:
            result['market_outlook'] = '未知'
        if 'key_stocks' not in result:
            result['key_stocks'] = []
        if 'first_board_candidates' not in result:
            result['first_board_candidates'] = []

        return result
    except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError) as e:
        logger.error(f'解析 LLM 响应失败: {e}, 响应内容: {str(response)[:200]}...')
        return {
            'market_outlook': '未知',
            'priority_sectors': [],
            'watch_sectors': [],
            'avoid_sectors': [],
            'key_stocks': [],
            'first_board_candidates': [],
        }


def _normalise_llm_result(parsed: dict) -> dict:
    """Validate untrusted model output before it can affect the strategy."""
    result = {
        'priority_sectors': {},
        'watch_sectors': [],
        'avoid_sectors': [],
        'market_outlook': '未知',
        'key_stocks': [],
        'first_board_candidates': [],
    }
    if not isinstance(parsed, dict):
        return result

    outlook = parsed.get('market_outlook')
    if isinstance(outlook, str) and outlook.strip():
        result['market_outlook'] = outlook.strip()[:20]

    priority_items = parsed.get('priority_sectors', [])
    if isinstance(priority_items, list):
        for item in priority_items[:MAX_PRIORITY_SECTORS]:
            if not isinstance(item, dict):
                continue
            sector = item.get('sector')
            if not isinstance(sector, str) or not sector.strip():
                continue
            try:
                weight = float(item.get('weight', 0.5))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(weight):
                continue
            result['priority_sectors'][sector.strip()] = max(0.0, min(1.0, weight))

    watch_items = parsed.get('watch_sectors', [])
    if isinstance(watch_items, list):
        for item in watch_items[:MAX_WATCH_SECTORS]:
            sector = item.get('sector') if isinstance(item, dict) else item
            if isinstance(sector, str) and sector.strip():
                result['watch_sectors'].append(sector.strip())

    avoid_items = parsed.get('avoid_sectors', [])
    if isinstance(avoid_items, list):
        for item in avoid_items[:MAX_AVOID_SECTORS]:
            sector = item.get('sector') if isinstance(item, dict) else item
            if isinstance(sector, str) and sector.strip():
                result['avoid_sectors'].append(sector.strip())

    key_stocks = parsed.get('key_stocks', [])
    if isinstance(key_stocks, list):
        for stock_code in key_stocks[:MAX_CORE_CANDIDATES]:
            raw_code = stock_code.get('code') if isinstance(
                stock_code, dict) else stock_code
            code = _normalise_stock_code(raw_code)
            if code:
                result['key_stocks'].append(code)

    candidate_items = parsed.get('first_board_candidates', [])
    if isinstance(candidate_items, list):
        seen = set()
        for item in candidate_items[:MAX_FIRST_BOARD_CANDIDATES]:
            if not isinstance(item, dict):
                continue
            code = _normalise_stock_code(item.get('code'))
            if not code or code in seen:
                continue
            confidence = _finite_float(item.get('confidence'))
            if confidence is None:
                continue
            confidence = max(0.0, min(1.0, confidence))
            raw_sources = item.get('evidence_sources', [])
            sources = []
            if isinstance(raw_sources, list):
                sources = [
                    str(source).strip()[:30]
                    for source in raw_sources[:5]
                    if str(source).strip()
                ]
            result['first_board_candidates'].append({
                'code': code,
                'confidence': confidence,
                'tier': 'core' if (confidence >= CORE_CANDIDATE_MIN_CONFIDENCE
                                   and len(set(sources)) >= 2) else 'watch',
                'sector': str(item.get('sector', '') or '').strip()[:30],
                'evidence_sources': sources,
                'reason': str(item.get('reason', '') or '').strip()[:160],
                'risks': str(item.get('risks', '') or '').strip()[:120],
            })
            seen.add(code)

    return result


def _filter_candidates_to_evidence(
        result: dict,
        evidence_sources: dict[str, set[str]],
        sector_names: set[str] | None = None) -> dict:
    """Reject hallucinated codes that were absent from every input source."""
    evidence_codes = set(evidence_sources)
    if sector_names is not None:
        result['priority_sectors'] = {
            sector: weight
            for sector, weight in result.get('priority_sectors', {}).items()
            if sector in sector_names
        }
        result['watch_sectors'] = [
            sector for sector in result.get('watch_sectors', [])
            if sector in sector_names
        ]
        result['avoid_sectors'] = [
            sector for sector in result.get('avoid_sectors', [])
            if sector in sector_names
        ]
    if not evidence_codes:
        result['key_stocks'] = []
        result['first_board_candidates'] = []
        return result

    validated = []
    for item in result.get('first_board_candidates', []):
        code = item['code']
        if code not in evidence_codes:
            continue
        # Trust source membership from local inputs, never the model's claimed
        # list.  This prevents duplicating a single signal to manufacture core.
        sources = sorted(evidence_sources[code])
        item['evidence_sources'] = sources
        item['tier'] = 'core' if (
            item['confidence'] >= CORE_CANDIDATE_MIN_CONFIDENCE
            and len(sources) >= 2
        ) else 'watch'
        validated.append(item)
    result['first_board_candidates'] = validated
    core_codes = [
        item['code'] for item in result['first_board_candidates']
        if item['tier'] == 'core'
    ]
    # key_stocks is retained as the compatibility projection of validated core
    # candidates.  A separately claimed legacy key_stock cannot bypass tiering.
    result['key_stocks'] = list(
        dict.fromkeys(core_codes))[:MAX_CORE_CANDIDATES]
    return result


def get_exploration_candidate_codes(result: dict, stock_pool) -> list[str]:
    """Project verified core/watch discoveries into valid exchange codes.

    This list is intended for simulation or shadow qualification only. It does
    not bypass the intraday decision engine and must not mutate the live core
    pool without an explicit, separately validated promotion.
    """
    available = set(stock_pool or ())
    codes = []
    for item in result.get('first_board_candidates', []):
        if not isinstance(item, dict):
            continue
        code = _normalise_stock_code(item.get('code'))
        if not code:
            continue
        suffixed = f'{code}.SH' if code.startswith('6') else f'{code}.SZ'
        if suffixed in available and suffixed not in codes:
            codes.append(suffixed)
    return codes


def run_pre_market_analysis() -> dict:
    """
    运行盘前 LLM 板块预判分析

    返回:
        dict: {
            'priority_sectors': {板块名: 权重},  # 优先板块
            'avoid_sectors': [板块名列表],        # 回避板块
            'market_outlook': str,                # 市场展望
            'key_stocks': [代码列表],             # 关键标的
        }
    """
    logger.info('[盘前分析] 开始 LLM 板块预判...')
    start_time = time.time()

    result = {
        'priority_sectors': {},
        'watch_sectors': [],
        'avoid_sectors': [],
        'market_outlook': '未知',
        'key_stocks': [],
        'first_board_candidates': [],
    }

    try:
        # 1. 加载 Prompt 模板
        template = _load_prompt_template()

        # 2. 获取数据
        ths_data = _fetch_ths_data()
        yesterday_limit_up = _get_yesterday_limit_up_stocks()
        gene_candidates = _get_first_board_candidate_pool()
        candidate_evidence, evidence_sources, sector_names = _build_candidate_evidence(
            ths_data, gene_candidates)

        # 3. 填充 Prompt
        prompt = template.format(
            current_time=datetime.now().strftime('%Y-%m-%d %H:%M'),
            hot_stocks_1h=ths_data['hot_stocks_1h'],
            hot_stocks_24h=ths_data['hot_stocks_24h'],
            hot_concept_sectors=ths_data['hot_concept_sectors'],
            hot_industry_sectors=ths_data['hot_industry_sectors'],
            yesterday_limit_up_stocks=yesterday_limit_up,
            first_board_candidate_evidence=candidate_evidence,
        )

        # 4. 调用 LLM
        response = _call_llm(prompt)
        if not response:
            logger.warning('[盘前分析] LLM 无响应，跳过盘前分析')
            return result

        # 5. 解析结果
        parsed = _parse_llm_response(response)

        # 转换为内部格式，并在影响策略前校验不可信的模型输出
        result = _normalise_llm_result(parsed)
        result = _filter_candidates_to_evidence(
            result, evidence_sources, sector_names)

        elapsed = time.time() - start_time
        logger.info(
            f'[盘前分析] 完成，耗时 {elapsed:.1f}s，市场展望: {result["market_outlook"]}，'
            f'优先板块: {list(result["priority_sectors"].keys())}'
            f'，首板候选: {len(result["first_board_candidates"])}只'
        )

    except Exception as e:
        logger.error(f'[盘前分析] 异常: {e}', exc_info=True)

    return result


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    result = run_pre_market_analysis()
    print(json.dumps(result, ensure_ascii=False, indent=2))
