"""
盘前 LLM 板块预判模块（U7升级）

在 9:15-9:25 运行，利用 AI 热股策略的数据源和 LLM 能力，
输出今日优先打板的板块列表和权重。

数据输入：
- 同花顺 24H 热股榜（盘前仍在更新）
- 同花顺概念板块热度排名
- 同花顺行业板块热度排名
- 昨日涨停列表（从涨停基因数据中获取）

输出：
- 优先板块字典 {板块名: 权重0-1}
- 风险板块列表
- 市场环境简评
"""

import json
import logging
import math
import os
import sys
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# 添加 deps 目录到 sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPS_DIR = os.path.join(ROOT_DIR, 'deps', 'ai_hotspot_trader')
if DEPS_DIR not in sys.path:
    sys.path.insert(0, DEPS_DIR)

# Prompt 模板路径
PROMPT_TEMPLATE_PATH = os.path.join(ROOT_DIR, 'prompts', 'pre_market_sector_v1.md')

# LLM 超时（秒）
LLM_TIMEOUT = 60


def _load_prompt_template() -> str:
    """加载 Prompt 模板"""
    with open(PROMPT_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def _fetch_ths_data() -> dict:
    """获取同花顺热股榜和板块热度数据"""
    try:
        from ths_scraper.scraper import THSHotSpotScraper
        scraper = THSHotSpotScraper()

        hot_stocks_24h = scraper.get_hot_stocks_24h(limit=30)
        hot_concept_sectors = scraper.get_hot_concept_sectors(limit=20)
        hot_industry_sectors = scraper.get_hot_industry_sectors(limit=20)

        # 格式化为文本
        stocks_text = '\n'.join([
            f"{i+1}. {s.name}({s.code}) - 概念:{','.join(s.concepts[:3]) if s.concepts else '无'}"
            for i, s in enumerate(hot_stocks_24h)
        ]) if hot_stocks_24h else '数据不可用'

        concept_text = '\n'.join([
            f"{i+1}. {s.name} - 涨跌幅:{s.change_pct:.2f}%"
            for i, s in enumerate(hot_concept_sectors)
        ]) if hot_concept_sectors else '数据不可用'

        industry_text = '\n'.join([
            f"{i+1}. {s.name} - 涨跌幅:{s.change_pct:.2f}%"
            for i, s in enumerate(hot_industry_sectors)
        ]) if hot_industry_sectors else '数据不可用'

        return {
            'hot_stocks_24h': stocks_text,
            'hot_concept_sectors': concept_text,
            'hot_industry_sectors': industry_text,
        }
    except Exception as e:
        logger.error(f'获取同花顺数据失败: {e}')
        return {
            'hot_stocks_24h': '数据获取失败',
            'hot_concept_sectors': '数据获取失败',
            'hot_industry_sectors': '数据获取失败',
        }


def _get_yesterday_limit_up_stocks() -> str:
    """从涨停基因CSV中获取昨日涨停列表"""
    try:
        import pandas as pd
        # 尝试读取最近的涨停基因文件
        gene_dir = os.path.join(ROOT_DIR, 'output', '涨停基因')
        if not os.path.exists(gene_dir):
            return '数据不可用'

        # 找最新的文件
        files = sorted([f for f in os.listdir(gene_dir) if f.endswith('.csv')], reverse=True)
        if not files:
            return '数据不可用'

        df = pd.read_csv(
            os.path.join(gene_dir, files[0]),
            dtype={'股票代码': str},
        )
        limit_up = df[df['涨停'] == True]
        if limit_up.empty:
            return '昨日无涨停股票'

        result = []
        for _, row in limit_up.head(30).iterrows():
            code = row.get('股票代码', '')
            name = row.get('股票名称', '')
            result.append(f"{code} {name}")
        return '\n'.join(result)
    except Exception as e:
        logger.error(f'获取昨日涨停列表失败: {e}')
        return '数据获取失败'


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
            max_tokens=2048,
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
                max_tokens=2048,
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
        if 'avoid_sectors' not in result:
            result['avoid_sectors'] = []
        if 'market_outlook' not in result:
            result['market_outlook'] = '未知'
        if 'key_stocks' not in result:
            result['key_stocks'] = []

        return result
    except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError) as e:
        logger.error(f'解析 LLM 响应失败: {e}, 响应内容: {str(response)[:200]}...')
        return {
            'market_outlook': '未知',
            'priority_sectors': [],
            'avoid_sectors': [],
            'key_stocks': [],
        }


def _normalise_llm_result(parsed: dict) -> dict:
    """Validate untrusted model output before it can affect the strategy."""
    result = {
        'priority_sectors': {},
        'avoid_sectors': [],
        'market_outlook': '未知',
        'key_stocks': [],
    }
    if not isinstance(parsed, dict):
        return result

    outlook = parsed.get('market_outlook')
    if isinstance(outlook, str) and outlook.strip():
        result['market_outlook'] = outlook.strip()[:20]

    priority_items = parsed.get('priority_sectors', [])
    if isinstance(priority_items, list):
        for item in priority_items[:5]:
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

    avoid_items = parsed.get('avoid_sectors', [])
    if isinstance(avoid_items, list):
        for item in avoid_items[:3]:
            sector = item.get('sector') if isinstance(item, dict) else item
            if isinstance(sector, str) and sector.strip():
                result['avoid_sectors'].append(sector.strip())

    key_stocks = parsed.get('key_stocks', [])
    if isinstance(key_stocks, list):
        for stock_code in key_stocks[:5]:
            code = str(stock_code).strip().split('.')[0]
            if len(code) == 6 and code.isdigit():
                result['key_stocks'].append(code)

    return result


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
        'avoid_sectors': [],
        'market_outlook': '未知',
        'key_stocks': [],
    }

    try:
        # 1. 加载 Prompt 模板
        template = _load_prompt_template()

        # 2. 获取数据
        ths_data = _fetch_ths_data()
        yesterday_limit_up = _get_yesterday_limit_up_stocks()

        # 3. 填充 Prompt
        prompt = template.format(
            current_time=datetime.now().strftime('%Y-%m-%d %H:%M'),
            hot_stocks_24h=ths_data['hot_stocks_24h'],
            hot_concept_sectors=ths_data['hot_concept_sectors'],
            hot_industry_sectors=ths_data['hot_industry_sectors'],
            yesterday_limit_up_stocks=yesterday_limit_up,
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

        elapsed = time.time() - start_time
        logger.info(
            f'[盘前分析] 完成，耗时 {elapsed:.1f}s，市场展望: {result["market_outlook"]}，'
            f'优先板块: {list(result["priority_sectors"].keys())}'
        )

    except Exception as e:
        logger.error(f'[盘前分析] 异常: {e}', exc_info=True)

    return result


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    result = run_pre_market_analysis()
    print(json.dumps(result, ensure_ascii=False, indent=2))
