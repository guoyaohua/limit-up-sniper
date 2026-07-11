"""
Enhanced Post-Market Review Module for A-Share Limit-Up Trading Strategy
打板策略 v2.1 增强复盘分析模块

This module provides comprehensive post-market analysis with:
- Precise stock categorization with mutual exclusivity validation
- Filter effectiveness metrics (precision, recall, F1-score)
- Missed opportunity deep-dive analysis
- Actionable optimization recommendations
- Interactive HTML reports with visualizations
"""

import os
import sys
import json
import html as html_lib
import pickle
import re
import traceback
from datetime import datetime, timedelta, time as dt_time
from typing import Dict, List, Optional, Tuple, Any, Set
from dataclasses import dataclass, field, asdict
from pathlib import Path
from unittest import result
import pandas as pd
import numpy as np
from loguru import logger
from collections import defaultdict, Counter
import time

# 确保项目根目录在 sys.path 中
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Import the shared data parser
from standalone.shared_data_parser import SharedDataParser

# Import email utilities
from infra.utils import send_email, send_html_email
from config import TRADE_LOG_DIR as CONFIG_TRADE_LOG_DIR

# Configure logger
logger.add("logs/post_market_review_enhanced_{time:YYYY-MM-DD}.log",
           rotation="1 day",
           retention="30 days",
           level="INFO",
           format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:>8} | {message}")

# ============================================================================
# Configuration
# ============================================================================


class ReviewConfig:
    """Enhanced Review Configuration"""

    # Data paths
    DATA_BACKUP_DIR = Path("./data_backup")
    LOG_DIR = Path("G:/Logs")
    REPORT_DIR = Path("./reports/review")
    TRADE_LOG_DIR = Path(CONFIG_TRADE_LOG_DIR)

    # Analysis parameters
    MIN_VOLUME_RATIO = 0.7
    MAX_TURNOVER_RATE = 15.0
    MIN_SEAL_AMOUNT = 5000000  # 500万封单额
    PROFIT_THRESHOLD = 0.05
    FILTER_EFFECTIVENESS_THRESHOLD = 0.7

    # Report settings
    AUTO_EMAIL = False
    EMAIL_RECIPIENTS = []
    KEEP_DAYS = 30

    # Time settings
    AUTO_RUN_TIME = "15:05"
    MARKET_CLOSE_TIME = "15:00"

    # Strategy version
    STRATEGY_VERSION = "v1.0"
    STRATEGY_NAME = "FirstLimitUp"
    
    # Trading mode: 'shadow' for shadow signal mode, 'live' for live trading mode
    TRADING_MODE = "live"


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class StockOutcome:
    """Stock market outcome classification"""
    stock_code: str
    outcome_type: str  # 'limit_up', 'broken_board', 'normal'
    limit_time: Optional[List[str]] = None
    break_time: Optional[List[str]] = None
    seal_success_rate: float = 0.0


@dataclass
class StrategyDecision:
    """Strategy decision for a stock"""
    stock_code: str
    decision_type: str  # 'approved', 'rejected'
    decision_reason: str
    filter_tags: List[str] = field(default_factory=list)
    timestamp: Optional[datetime] = None
    buy_type: str = "未知"  # '扫板', '排板', '未知'
    buy_reason: str = ""
    gene_data: Dict[str, Any] = field(default_factory=dict)
    event_context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FilterMetrics:
    """Filter performance metrics"""
    filter_name: str
    true_positive: int = 0  # Correctly filtered broken boards or not limit up stocks
    false_positive: int = 0  # Incorrectly filtered limit-ups
    true_negative: int = 0  # Correctly passed limit-ups
    false_negative: int = 0  # Incorrectly passed broken boards
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    # 新增：被过滤的股票列表
    filtered_limit_up_stocks: List[str] = field(default_factory=list)  # 误伤的涨停股
    filtered_broken_stocks: List[str] = field(default_factory=list)  # 正确过滤的炸板股
    filtered_other_stocks: List[str] = field(default_factory=list)  # 其他被过滤的股票

    def calculate_metrics(self):
        """Calculate precision, recall, and F1 score"""
        if self.true_positive + self.false_positive > 0:
            self.precision = self.true_positive / (self.true_positive +
                                                   self.false_positive)
        else:
            self.precision = 0.0

        if self.true_positive + self.false_negative > 0:
            self.recall = self.true_positive / (self.true_positive +
                                                self.false_negative)
        else:
            self.recall = 0.0

        if self.precision + self.recall > 0:
            self.f1_score = 2 * (self.precision *
                                 self.recall) / (self.precision + self.recall)
        else:
            self.f1_score = 0.0


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_event_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
    return None


def _json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_reason_tags(reason: Any) -> List[str]:
    if reason is None:
        return []
    if isinstance(reason, list):
        tags: List[str] = []
        for item in reason:
            tags.extend(_normalize_reason_tags(item))
        return [tag for tag in tags if tag]
    if isinstance(reason, str):
        text = reason.strip()
        if not text:
            return []
        if text.startswith('[') and text.endswith(']'):
            try:
                parsed = json.loads(text.replace("'", '"'))
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except Exception:
                pass
        return [part.strip() for part in re.split(r'[;；、,，\n]+', text) if part.strip()]
    return [str(reason)]


def _unique_keep_order(items: List[Any]) -> List[Any]:
    result = []
    seen = set()
    for item in items:
        marker = json.dumps(item,
                            ensure_ascii=False,
                            sort_keys=True,
                            default=_json_default) if isinstance(item, (dict, list)) else item
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


# ============================================================================
# Enhanced Data Collector
# ============================================================================


class EnhancedDataCollector:
    """Enhanced data collector with comprehensive data extraction"""
    def __init__(self, date: str, strategy_version: str = None, trading_mode: str = None):
        self.date = date
        self.strategy_version = strategy_version or ReviewConfig.STRATEGY_VERSION
        self.trading_mode = trading_mode or ReviewConfig.TRADING_MODE  # 'shadow' or 'live'
        self.data_dir = ReviewConfig.DATA_BACKUP_DIR / f"{ReviewConfig.STRATEGY_NAME}_{self.strategy_version}"
        self.log_dir = ReviewConfig.LOG_DIR / f"{ReviewConfig.STRATEGY_NAME}_{self.strategy_version}" / "DEBUG"
        self.report_dir = ReviewConfig.REPORT_DIR
        
        logger.info(f"初始化数据收集器 - 日期: {self.date}, 策略版本: {self.strategy_version}, 交易模式: {self.trading_mode}")

    def collect_comprehensive_data(self) -> Dict[str, Any]:
        """Collect all relevant data with enhanced categorization"""
        logger.info(f"Collecting comprehensive data for {self.date}")

        data = {
            'date': self.date,
            'market_outcomes': {},
            'strategy_decisions': {},
            'detailed_filters': {},
            'positions': {},
            'orders': {},
            'blacklist': {},
            'shadow_data': {},
            'metrics': {},
            'log_data': {},
            'gene_data': {},
            'events': [],
            'event_summary': {},
            'review_context': {},
        }

        try:
            # Load limit-up gene data
            data['gene_data'] = self._load_limit_up_gene_data()

            # 加载策略共享数据
            shared_data_raw = self._load_shared_data()
            shared_data_info = None
            if shared_data_raw:
                shared_data_parsed = SharedDataParser.parse_shared_data(shared_data_raw)
                shared_data_info = SharedDataParser.extract_useful_info(shared_data_parsed)
                data['shared_data'] = shared_data_info

                # 提取关键数据
                if shared_data_info:
                    data['positions'] = shared_data_info.get('positions', {})
                    data['orders'] = shared_data_info.get('orders', {})
                    data['blacklist'] = data['shared_data'].get('blacklist', {})
                else:
                    logger.error(f"解析 {self.date} 的共享数据失败")
            else:
                logger.error(f"未能加载 {self.date} 的共享数据备份文件")

            events = self._load_trade_events()
            data['events'] = events
            data['event_summary'] = self._build_event_summary(events)

            # Parse strategy logs
            log_data = self._parse_comprehensive_logs(events)
            data['log_data'] = log_data

            # Categorize market outcomes
            data['market_outcomes'] = self._categorize_market_outcomes(
                log_data, shared_data_info, events)

            # Categorize strategy decisions
            data[
                'strategy_decisions'], approved_stocks, rejected_stocks = self._categorize_strategy_decisions(
                    log_data, shared_data_info, data['gene_data'], events)

            # Extract detailed filter information
            data['detailed_filters'] = self._extract_detailed_filters(
                log_data, approved_stocks)

            # Calculate initial metrics
            data['metrics'] = self._calculate_initial_metrics(data)

            # Build deterministic review context
            data['review_context'] = self._build_review_context(data)

            logger.info(
                f"Data collection complete - {len(data['market_outcomes'])} stocks analyzed"
            )

            self.save_comprehensive_data(data)

        except Exception as e:
            logger.exception(f"Error collecting comprehensive data: {e}")
            raise e

        return data

    def save_comprehensive_data(self, data: Dict):
        """
        保存复盘数据
        Save review data
        """
        logger.info("保存复盘数据 / Saving review data")

        try:
            # Create directories
            daily_dir = self.report_dir / "daily"
            daily_dir.mkdir(parents=True, exist_ok=True)

            # Save as JSON
            json_file = daily_dir / f"comprehensive_data_{self.date}.json"
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"JSON数据已保存: {json_file}")

            review_context = data.get('review_context') or {}
            review_context_file = daily_dir / f"review_context_{self.date}.json"
            with open(review_context_file, 'w', encoding='utf-8') as f:
                json.dump(review_context,
                          f,
                          ensure_ascii=False,
                          indent=2,
                          default=_json_default)
            logger.info(f"Review context 已保存: {review_context_file}")

            # # Save as pickle for Python analysis
            # pkl_file = daily_dir / f"review_{self.review_data.date}.pkl"
            # with open(pkl_file, 'wb') as f:
            #     pickle.dump(data, f)
            # logger.info(f"PKL数据已保存: {pkl_file}")

        except Exception as e:
            logger.exception(f"保存复盘数据失败: {e}")
            raise e

    def _load_limit_up_gene_data(self) -> Dict[str, Dict]:
        """
        加载涨停基因数据
        Load limit-up gene data from CSV
        """
        gene_data = {}
        try:
            csv_path = Path(f"output/涨停基因/涨停基因_{self.date}.csv")
            if not csv_path.exists():
                logger.warning(f"涨停基因数据文件不存在: {csv_path}")
                return {}

            df = pd.read_csv(csv_path)

            # Key columns to extract
            columns = [
                '股票代码', '连板率', '涨停次日收盘溢价超5%比例', '首板次日收盘红盘率', '首板封板率', '涨停次数',
                '首板涨停或炸板次日开盘平均溢价', '涨停基因打分'
            ]

            # Ensure columns exist
            available_cols = [c for c in columns if c in df.columns]

            for _, row in df[available_cols].iterrows():
                stock_code = row['股票代码']
                gene_data[stock_code] = row.to_dict()

            logger.info(
                f"Loaded limit-up gene data for {len(gene_data)} stocks")
            return gene_data

        except Exception as e:
            logger.error(f"Failed to load limit-up gene data: {e}")
            return {}

    def _load_shared_data(self) -> Optional[Dict]:
        """加载策略共享数据备份文件

        根据交易模式选择不同的数据文件:
        - shadow: 影子模式，使用 shadow_shared_data_backup_{date}.pkl
        - live: 实盘模式，使用 shared_data_backup_{date}.pkl
        """
        # 根据交易模式选择不同的文件名前缀
        if self.trading_mode == 'live':
            pattern = f"shared_data_backup_{self.date}*.pkl"
            mode_desc = "实盘"
        else:  # shadow mode (default)
            pattern = f"shadow_shared_data_backup_{self.date}*.pkl"
            mode_desc = "影子"

        backup_files = list(self.data_dir.glob(pattern))

        if not backup_files:
            logger.warning(f"未找到 {self.date} 的{mode_desc}数据备份文件, 文件模式: {pattern}，继续使用日志/事件流复盘")
            return None

        latest_file = max(backup_files, key=lambda p: p.stat().st_mtime)

        try:
            with open(latest_file, 'rb') as f:
                data = pickle.load(f)
                logger.info(f"已加载{mode_desc}数据: {latest_file}")
                return data
        except Exception as e:
            logger.exception(f"加载共享数据失败: {e}")
            return None

    def _load_trade_events(self) -> List[Dict[str, Any]]:
        """加载按日结构化事件流。"""
        event_file = ReviewConfig.TRADE_LOG_DIR / self.date / 'events.jsonl'
        if not event_file.exists():
            logger.warning(f"事件日志不存在: {event_file}")
            return []

        events = []
        try:
            with open(event_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except Exception as e:
                        logger.warning(f"解析事件失败: {e}")
        except Exception as e:
            logger.exception(f"读取事件日志失败: {e}")
            raise e

        logger.info(f"已加载事件 {len(events)} 条: {event_file}")
        return events

    def _build_event_summary(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        summary = {
            'candidate_seen': 0,
            'buy_decision': 0,
            'cancel_decision': 0,
            'sell_decision': 0,
            'watchlist_enter': 0,
            'watchlist_release': 0,
            'blacklist_enter': 0,
            'break_episode_start': 0,
            'break_episode_end': 0,
            'limit_up': 0,
            'order_submitted': 0,
            'buy_types': defaultdict(int),
            'sell_triggers': defaultdict(int),
            'event_count': len(events),
        }

        for event in events:
            event_type = event.get('event_type', '')
            if event_type in summary and isinstance(summary[event_type], int):
                summary[event_type] += 1

            if event_type == 'buy_decision':
                summary['buy_types'][event.get('buy_type', '未知')] += 1
            elif event_type == 'sell_decision':
                summary['sell_triggers'][event.get('order_remark', '未知')] += 1

        summary['buy_types'] = dict(summary['buy_types'])
        summary['sell_triggers'] = dict(summary['sell_triggers'])
        return summary

    def _index_events(self, events: List[Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        indexed: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        for event in events:
            stock_code = event.get('stock_code') or '__NO_STOCK__'
            event_type = event.get('event_type', 'unknown')
            indexed[stock_code][event_type].append(event)
        return indexed

    def _parse_comprehensive_logs(self, events: Optional[List[Dict[str, Any]]] = None) -> Dict:
        """Parse logs with enhanced extraction"""
        log_data = {
            'pre_market_filters': {},  # {stock_code: [reasons]}
            'yesterday_limit_up_list': [],  # List of stock codes
            'yesterday_first_limit_list': [],  # List of stock codes
            'limit_up_list': [],  # List of stock codes
            'first_limit_list': [],  # List of stock codes
            'break_list': [],  # List of stock codes
            'not_buy_reasons': {},  # {stock_code: [reasons]}
            'cancel_reasons': {},  # {stock_code: [reasons]}
            'filter_statistics': {},
            'buy_details':
            {},  # {stock_code: {'type': '扫板'/'排板', 'reason': '...'}}
            'scan_list': [],  # List of scan board stocks
            'queue_list': [],  # List of queue board stocks
        }

        try:
            # Find and read log files
            date_with_dash = f"{self.date[:4]}-{self.date[4:6]}-{self.date[6:8]}"
            log_pattern = f"Debug_打板策略_{self.strategy_version}_{date_with_dash}.log"
            log_files = list(self.log_dir.glob(log_pattern))

            if not log_files:
                logger.warning(f"No log files found for {self.date}, fallback to structured events only")

            for log_file in log_files:
                content = self._read_log_file(log_file)
                if content:
                    # Parse all components
                    log_data['pre_market_filters'].update(
                        self._parse_pre_market_filters(content))
                    log_data[
                        'yesterday_limit_up_list'] = self._parse_yesterday_limit_lists(
                            content)
                    log_data[
                        'yesterday_first_limit_list'] = self._parse_yesterday_first_limit_list(
                            content)
                    log_data['limit_up_list'].extend(
                        self._parse_limit_up_list(content))
                    log_data['first_limit_list'].extend(
                        self._parse_first_limit_list(content))
                    log_data['break_list'].extend(
                        self._parse_break_list(content))

                    log_data['not_buy_reasons'].update(
                        self._parse_not_buy_reasons(content))
                    log_data['cancel_reasons'].update(
                        self._parse_cancel_reasons(content))

                    log_data['filter_statistics'].update(
                        self._parse_filter_statistics(content))

                    # Parse buy details
                    details = self._parse_buy_details(content)
                    log_data['buy_details'].update(details)

            if events:
                event_index = self._index_events(events)
                for stock_code, event_groups in event_index.items():
                    if stock_code == '__NO_STOCK__':
                        continue

                    if stock_code not in log_data['limit_up_list'] and event_groups.get('limit_up'):
                        log_data['limit_up_list'].append(stock_code)
                    if stock_code not in log_data['break_list'] and event_groups.get('break_episode_start'):
                        log_data['break_list'].append(stock_code)

                    buy_events = event_groups.get('buy_decision', [])
                    if buy_events and stock_code not in log_data['buy_details']:
                        latest_buy = buy_events[-1]
                        inferred_buy_type = latest_buy.get('buy_type', '未知')
                        log_data['buy_details'][stock_code] = {
                            'type': inferred_buy_type,
                            'reason': latest_buy.get('reason', ''),
                        }

                    cancel_events = event_groups.get('cancel_decision', [])
                    if cancel_events and stock_code not in log_data['cancel_reasons']:
                        log_data['cancel_reasons'][stock_code] = _unique_keep_order([
                            event.get('reason', '') for event in cancel_events if event.get('reason')
                        ])

                    watchlist_events = event_groups.get('watchlist_enter', [])
                    if watchlist_events and stock_code not in log_data['not_buy_reasons']:
                        log_data['not_buy_reasons'][stock_code] = _unique_keep_order([
                            event.get('source', '观察名单') for event in watchlist_events if event.get('source')
                        ])

            log_data['limit_up_list'] = _unique_keep_order(log_data['limit_up_list'])
            log_data['first_limit_list'] = _unique_keep_order(log_data['first_limit_list'])
            log_data['break_list'] = _unique_keep_order(log_data['break_list'])

            # Categorize into scan and queue lists
            for stock, detail in log_data['buy_details'].items():
                if detail['type'] == '扫板':
                    log_data['scan_list'].append(stock)
                elif detail['type'] == '排板':
                    log_data['queue_list'].append(stock)

            log_data['scan_list'] = _unique_keep_order(log_data['scan_list'])
            log_data['queue_list'] = _unique_keep_order(log_data['queue_list'])

            # logger.info(log_data)

        except Exception as e:
            logger.exception(f"Error parsing comprehensive logs: {e}")
            raise e

        return log_data

    def _parse_buy_details(self, content: str) -> Dict:
        """
        解析买入详情（类型和原因）
        Parse buy details (type and reason)
        """
        buy_details = {}

        # Pattern to match order info log
        # 2025-11-17 ... [模拟] 订单信息: {'操作原因': '[扫板买入] ...', ..., '买入类型': '扫板', ...}
        # We look for the dictionary string representation

        # Regex to find the order info dict part
        # This is a bit complex because the dict string can be very long and contain nested structures
        # We'll try to match the specific fields we need within the log line context

        lines = content.split('\n')
        for line in lines:
            if "订单信息:" in line and "'委托类型': <OrderType.BUY: '买入'>" in line:
                try:
                    # Extract stock code
                    stock_match = re.search(r"'股票代码': '(\d{6}\.\w{2})'", line)
                    if not stock_match:
                        continue
                    stock_code = stock_match.group(1)

                    # Extract buy reason first
                    # The reason is in '操作原因': '...'
                    # It might contain newlines escaped as \n
                    reason_match = re.search(r"'操作原因': '(.+?)', '委托类型'", line)
                    if not reason_match:
                        # Try alternative pattern if reason is at the end or different order
                        reason_match = re.search(r"'操作原因': '(.+?)'", line)

                    buy_reason = reason_match.group(1) if reason_match else ""

                    # Clean up reason string (unescape newlines)
                    buy_reason = buy_reason.replace('\\n', '\n')

                    # Extract buy type
                    # First try to find explicit key
                    type_match = re.search(r"'买入类型': '([^']+)'", line)
                    if type_match:
                        buy_type = type_match.group(1)
                    else:
                        # Fallback: infer from reason string
                        if '[扫板买入]' in buy_reason:
                            buy_type = '扫板'
                        elif '[排板买入]' in buy_reason:
                            buy_type = '排板'
                        else:
                            buy_type = "未知"

                    buy_details[stock_code] = {
                        'type': buy_type,
                        'reason': buy_reason
                    }
                except Exception as e:
                    logger.warning(f"Error parsing buy details from line: {e}")
                    continue

        return buy_details

    def _parse_yesterday_limit_lists(self, content: str) -> List[str]:
        """
        解析昨日涨停列表
        Parse yesterday's limit-up lists
        """
        result = []
        # Parse yesterday's limit-up list
        # Pattern: 昨日20250909涨停列表:['600376.SH', '002759.SZ', ...]
        pattern = r'昨日\d+涨停列表:\[(.*?)\]'
        matches = re.findall(pattern, content)
        for match in matches:
            stock_codes = re.findall(r"'(\d{6}\.\w{2})'", match)
            result.extend(stock_codes)

        # Remove duplicates
        result = list(set(result))

        return result

    def _parse_yesterday_first_limit_list(self, content: str) -> List[str]:
        """
        解析昨日首版涨停列表
        Parse yesterday's first limit-up lists
        """
        result = []

        # Parse yesterday's first limit list
        # Pattern: 昨日20250909首版涨停列表:['002866.SZ', '600503.SH', ...]
        pattern = r'昨日\d+首版涨停列表:\[(.*?)\]'
        matches = re.findall(pattern, content)
        for match in matches:
            stock_codes = re.findall(r"'(\d{6}\.\w{2})'", match)
            result.extend(stock_codes)

        # Remove duplicates
        result = list(set(result))

        return result

    def _read_log_file(self, log_file: Path) -> Optional[str]:
        """Read log file with encoding detection"""
        for encoding in ['utf-8', 'gbk', 'gb2312', 'gb18030']:
            try:
                with open(log_file, 'r', encoding=encoding,
                          errors='ignore') as f:
                    return f.read()
            except Exception:
                continue
        raise UnicodeDecodeError(
            f"Failed to read log file {log_file} with multiple encodings")

    def _categorize_market_outcomes(
            self, log_data: Dict,
            shadow_info: Dict,
            events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, StockOutcome]:
        """Categorize stocks by market outcome"""
        outcomes = {}
        event_index = self._index_events(events or [])

        # Get limit-up stocks from log data
        limit_up_stocks = set(log_data.get('limit_up_list', []))

        # Get broken board stocks from log data
        break_stocks = set(log_data.get('break_list', []))

        for stock_code, groups in event_index.items():
            if stock_code == '__NO_STOCK__':
                continue
            if groups.get('limit_up'):
                limit_up_stocks.add(stock_code)
            if groups.get('break_episode_start'):
                break_stocks.add(stock_code)

        # Ensure mutual exclusivity using event end state when available
        overlapping = limit_up_stocks & break_stocks
        for stock_code in list(overlapping):
            if event_index.get(stock_code, {}).get('break_episode_end') or \
                    (shadow_info and stock_code in shadow_info.get('limit_up_stocks', {})):
                break_stocks.discard(stock_code)
            else:
                limit_up_stocks.discard(stock_code)

        # Create outcome objects
        for stock in limit_up_stocks:
            outcome_groups = event_index.get(stock, {})
            outcomes[stock] = StockOutcome(
                stock_code=stock,
                outcome_type='limit_up',
                limit_time=shadow_info.get('limit_up_stocks', {}).get(
                    stock, {}).get('times', []) if shadow_info else [
                        event.get('timestamp') for event in outcome_groups.get('limit_up', [])
                    ],
                break_time=shadow_info.get('break_stocks', {}).get(
                    stock, {}).get('times', []) if shadow_info else [
                        event.get('timestamp') for event in outcome_groups.get('break_episode_start', [])
                    ],
                seal_success_rate=1.0)

        for stock in break_stocks:
            outcome_groups = event_index.get(stock, {})
            outcomes[stock] = StockOutcome(
                stock_code=stock,
                outcome_type='broken_board',
                limit_time=shadow_info.get('limit_up_stocks', {}).get(
                    stock, {}).get('times', []) if shadow_info else [
                        event.get('timestamp') for event in outcome_groups.get('limit_up', [])
                    ],
                break_time=shadow_info.get('break_stocks', {}).get(
                    stock, {}).get('times', []) if shadow_info else [
                        event.get('timestamp') for event in outcome_groups.get('break_episode_start', [])
                    ],
                seal_success_rate=0.0)

        return outcomes

    def _build_event_context(self,
                             stock_code: str,
                             events_by_stock: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> Dict[str, Any]:
        groups = events_by_stock.get(stock_code, {})
        buy_events = groups.get('buy_decision', [])
        cancel_events = groups.get('cancel_decision', [])
        sell_events = groups.get('sell_decision', [])
        watchlist_events = groups.get('watchlist_enter', [])
        blacklist_events = groups.get('blacklist_enter', [])
        break_events = groups.get('break_episode_start', [])

        return {
            'buy_event_count': len(buy_events),
            'cancel_event_count': len(cancel_events),
            'sell_event_count': len(sell_events),
            'watchlist_event_count': len(watchlist_events),
            'blacklist_event_count': len(blacklist_events),
            'break_event_count': len(break_events),
            'latest_buy_event': buy_events[-1] if buy_events else None,
            'latest_cancel_event': cancel_events[-1] if cancel_events else None,
            'latest_sell_event': sell_events[-1] if sell_events else None,
            'latest_watchlist_event': watchlist_events[-1] if watchlist_events else None,
            'latest_blacklist_event': blacklist_events[-1] if blacklist_events else None,
        }

    def _categorize_strategy_decisions(
            self, log_data: Dict, shadow_info: Dict,
            gene_data: Dict,
            events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, StrategyDecision]:
        """Categorize stocks by strategy decision"""
        decisions = {}
        buy_details = log_data.get('buy_details', {})
        event_index = self._index_events(events or [])

        # Get positions (approved stocks)
        positions = set()
        if shadow_info and 'positions' in shadow_info:
            positions.update(shadow_info['positions'].keys())

        order_submitted = {
            stock_code for stock_code, groups in event_index.items()
            if stock_code != '__NO_STOCK__' and groups.get('order_submitted')
        }

        # Get late cancellations (also approved)
        late_cancels = set()
        for stock, reasons in log_data.get('cancel_reasons', {}).items():
            if any('尾盘未成交' in str(r) for r in reasons):
                late_cancels.add(stock)

        # All approved stocks
        approved = positions | late_cancels | order_submitted

        # Create decision objects for approved stocks
        for stock in approved:
            details = buy_details.get(stock, {})
            context = self._build_event_context(stock, event_index)
            latest_buy = context.get('latest_buy_event') or {}
            decisions[stock] = StrategyDecision(
                stock_code=stock,
                decision_type='approved',
                decision_reason='已成交' if stock in positions else '已发单' if stock in order_submitted else '未成交',
                buy_type=details.get('type', latest_buy.get('buy_type', '未知')),
                buy_reason=details.get('reason', latest_buy.get('reason', '')),
                timestamp=_to_event_time(latest_buy.get('timestamp')),
                gene_data=gene_data.get(stock, {}),
                event_context=context)

        # Get rejected stocks
        rejected = set()

        # Pre-market filtered
        for stock, reason in log_data.get('pre_market_filters', {}).items():
            if stock not in approved:
                rejected.add(stock)
                decisions[stock] = StrategyDecision(
                    stock_code=stock,
                    decision_type='rejected',
                    decision_reason=f'盘前过滤: {reason}',
                    filter_tags=_normalize_reason_tags(reason),
                    gene_data=gene_data.get(stock, {}),
                    event_context=self._build_event_context(stock, event_index))

        # Not buy reasons
        blacklist_reasons = shadow_info.get('blacklist', {}) if shadow_info else {}

        for stock, reasons in log_data.get('not_buy_reasons', {}).items():
            if stock not in approved:
                rejected.add(stock)
                if stock not in decisions:
                    refined_tags = []
                    for tag in _ensure_list(reasons):
                        if tag == '黑名单' and stock in blacklist_reasons:
                            blacklist_detail = blacklist_reasons[stock]
                            refined_tags.append(f'黑名单-{blacklist_detail}')
                        else:
                            refined_tags.append(tag)

                    refined_tags = _unique_keep_order(_normalize_reason_tags(refined_tags))
                    decisions[stock] = StrategyDecision(
                        stock_code=stock,
                        decision_type='rejected',
                        decision_reason=f'不满足买入条件: {refined_tags}',
                        filter_tags=refined_tags,
                        gene_data=gene_data.get(stock, {}),
                        event_context=self._build_event_context(stock, event_index))

        # Non-late cancellations
        for stock, reasons in log_data.get('cancel_reasons', {}).items():
            if stock not in approved:
                rejected.add(stock)
                if stock not in decisions:
                    decisions[stock] = StrategyDecision(
                        stock_code=stock,
                        decision_type='rejected',
                        decision_reason='撤单',
                        filter_tags=_unique_keep_order(_normalize_reason_tags(reasons)),
                        gene_data=gene_data.get(stock, {}),
                        event_context=self._build_event_context(stock, event_index))

        for stock_code, groups in event_index.items():
            if stock_code == '__NO_STOCK__' or stock_code in decisions:
                continue

            if groups.get('buy_decision') or groups.get('cancel_decision') or groups.get('blacklist_enter') or groups.get('watchlist_enter'):
                latest_buy = groups.get('buy_decision', [])[-1] if groups.get('buy_decision') else {}
                latest_cancel = groups.get('cancel_decision', [])[-1] if groups.get('cancel_decision') else {}
                latest_blacklist = groups.get('blacklist_enter', [])[-1] if groups.get('blacklist_enter') else {}
                latest_watchlist = groups.get('watchlist_enter', [])[-1] if groups.get('watchlist_enter') else {}

                filter_tags = _unique_keep_order(
                    _normalize_reason_tags(latest_cancel.get('reason')) +
                    _normalize_reason_tags(latest_blacklist.get('blacklist_reason')) +
                    _normalize_reason_tags(latest_watchlist.get('source'))
                )
                decision_reason = latest_cancel.get('reason') or latest_blacklist.get('reason') or latest_watchlist.get('reason') or latest_buy.get('reason', '')
                decision_type = 'approved' if groups.get('order_submitted') else 'rejected'

                decisions[stock_code] = StrategyDecision(
                    stock_code=stock_code,
                    decision_type=decision_type,
                    decision_reason=decision_reason or ('已发单' if decision_type == 'approved' else '事件流拒绝'),
                    filter_tags=filter_tags,
                    timestamp=_to_event_time((latest_cancel or latest_blacklist or latest_watchlist or latest_buy).get('timestamp')),
                    buy_type=latest_buy.get('buy_type', '未知'),
                    buy_reason=latest_buy.get('reason', ''),
                    gene_data=gene_data.get(stock_code, {}),
                    event_context=self._build_event_context(stock_code, event_index))
                if decision_type == 'approved':
                    approved.add(stock_code)
                else:
                    rejected.add(stock_code)

        return decisions, approved, rejected

    def _extract_detailed_filters(self, log_data: Dict,
                                  approved_stocks: Set[str]) -> Dict:
        """Extract detailed filter information"""
        filters = {'pre_market': {}, 'real_time': {}, 'cancel': {}}

        # Pre-market filters - remove approved stocks
        pre_market_data = log_data.get('pre_market_filters', {})
        filters['pre_market'] = {
            k: v
            for k, v in pre_market_data.items() if k not in approved_stocks
        }

        # Real-time filters (not buy reasons) - remove approved stocks
        real_time_data = log_data.get('not_buy_reasons', {})
        filters['real_time'] = {
            k: v
            for k, v in real_time_data.items() if k not in approved_stocks
        }

        # Cancel filters - remove approved stocks
        cancel_data = log_data.get('cancel_reasons', {})
        filters['cancel'] = {
            k: v
            for k, v in cancel_data.items() if k not in approved_stocks
        }

        return filters

    def _calculate_initial_metrics(self, data: Dict) -> Dict:
        """Calculate initial performance metrics"""
        metrics = {
            'total_limit_up': 0,
            'total_broken_board': 0,
            'strategy_approved': 0,
            'strategy_rejected': 0,
            'strategy_bought': 0,  # 实际买入数量
            'first_limit_up': 0,  # 首板涨停数
            'capture_rate': 0.0,
            'avoidance_rate': 0.0,
            'broken_board_rate': 0.0,
            'success_rate': 0.0,  # 买入成功率
            'first_limit_rate': 0.0,  # 首板率
            'candidate_count': data.get('event_summary', {}).get('candidate_seen', 0),
            'buy_decision_count': data.get('event_summary', {}).get('buy_decision', 0),
            'cancel_decision_count': data.get('event_summary', {}).get('cancel_decision', 0),
            'sell_decision_count': data.get('event_summary', {}).get('sell_decision', 0),
            'watchlist_enter_count': data.get('event_summary', {}).get('watchlist_enter', 0),
            'watchlist_release_count': data.get('event_summary', {}).get('watchlist_release', 0),
            'blacklist_enter_count': data.get('event_summary', {}).get('blacklist_enter', 0),
            'order_submitted_count': data.get('event_summary', {}).get('order_submitted', 0),
            'event_count': data.get('event_summary', {}).get('event_count', 0),
        }

        # Count outcomes
        for outcome in data['market_outcomes'].values():
            if outcome.outcome_type == 'limit_up':
                metrics['total_limit_up'] += 1
            elif outcome.outcome_type == 'broken_board':
                metrics['total_broken_board'] += 1

        # Count first limit-ups
        first_limit_list = data.get('log_data', {}).get('first_limit_list', [])
        metrics['first_limit_up'] = len(first_limit_list)

        # Count decisions
        for decision in data['strategy_decisions'].values():
            if decision.decision_type == 'approved':
                metrics['strategy_approved'] += 1
            elif decision.decision_type == 'rejected':
                metrics['strategy_rejected'] += 1

        # Count actual positions (bought stocks)
        positions = data.get('positions', {})
        metrics['strategy_bought'] = len(positions)

        # Calculate capture rate: 涨停封板的股票中有多少是被策略认可的
        if metrics['total_limit_up'] > 0:
            # Count limit-up stocks that were approved by strategy
            captured_limit_ups = sum(
                1 for stock in data['market_outcomes']
                if data['market_outcomes'][stock].outcome_type == 'limit_up'
                and stock in data['strategy_decisions'] and
                data['strategy_decisions'][stock].decision_type == 'approved')
            metrics['capture_rate'] = captured_limit_ups / metrics[
                'total_limit_up']

        # Calculate avoidance rate: 涨停炸板的股票中有多少是策略拒绝的
        if metrics['total_broken_board'] > 0:
            avoided_breaks = sum(
                1 for stock in data['market_outcomes']
                if data['market_outcomes'][stock].outcome_type ==
                'broken_board' and stock in data['strategy_decisions'] and
                data['strategy_decisions'][stock].decision_type == 'rejected')

            # 扫板买到的但是从未真正涨停的股票数量
            never_limit_up_bought = sum(
                1 for stock in positions
                if stock not in data['market_outcomes'])

            metrics['avoidance_rate'] = avoided_breaks / (
                metrics['total_broken_board'] + never_limit_up_bought)

        # 炸板率
        total_touched = metrics['total_limit_up'] + metrics[
            'total_broken_board']
        if total_touched > 0:
            metrics['broken_board_rate'] = metrics[
                'total_broken_board'] / total_touched

        # 买入成功率 (买入的股票中涨停的比例)
        if metrics['strategy_bought'] > 0:
            success_count = sum(
                1 for stock in positions if stock in data['market_outcomes']
                and data['market_outcomes'][stock].outcome_type == 'limit_up')
            metrics[
                'success_rate'] = success_count / metrics['strategy_bought']

        # 首板率
        if total_touched > 0:
            metrics[
                'first_limit_rate'] = metrics['first_limit_up'] / total_touched

        logger.info(f"Initial metrics calculated: {metrics}")

        return metrics

    def _build_review_context(self, data: Dict[str, Any]) -> Dict[str, Any]:
        events = data.get('events', [])
        event_index = self._index_events(events)
        decisions = data.get('strategy_decisions', {})
        market_outcomes = data.get('market_outcomes', {})
        shared_data = data.get('shared_data', {}) or {}

        candidates = []
        for stock_code in sorted({
                stock for stock in event_index.keys() if stock != '__NO_STOCK__'
        } | set(decisions.keys()) | set(market_outcomes.keys())):
            groups = event_index.get(stock_code, {})
            decision = decisions.get(stock_code)
            outcome = market_outcomes.get(stock_code)
            stock_features = shared_data.get('stock_features', {}).get(stock_code, {})
            break_stats = shared_data.get('break_statistics', {}).get(stock_code, {})
            watch_meta = shared_data.get('watchlist_metadata', {}).get(stock_code, {})
            episode_state = shared_data.get('break_episode_state', {}).get(stock_code, {})
            snapshot = shared_data.get('intraday_snapshots', {}).get(stock_code, {})
            decision_tag = shared_data.get('decision_tags', {}).get(stock_code, {})

            buy_events = groups.get('buy_decision', [])
            cancel_events = groups.get('cancel_decision', [])
            sell_events = groups.get('sell_decision', [])
            blacklist_events = groups.get('blacklist_enter', [])
            watchlist_enter_events = groups.get('watchlist_enter', [])
            watchlist_release_events = groups.get('watchlist_release', [])
            order_events = groups.get('order_submitted', [])

            latest_buy = buy_events[-1] if buy_events else {}
            latest_cancel = cancel_events[-1] if cancel_events else {}
            latest_sell = sell_events[-1] if sell_events else {}

            filter_tags = decision.filter_tags if decision else []
            if not filter_tags:
                filter_tags = _unique_keep_order(
                    _normalize_reason_tags(latest_cancel.get('reason')) +
                    _normalize_reason_tags(decision_tag.get('reason')) +
                    _normalize_reason_tags(latest_buy.get('reason'))
                )

            candidates.append({
                'stock_code': stock_code,
                'decision_type': decision.decision_type if decision else 'unknown',
                'decision_reason': decision.decision_reason if decision else '',
                'buy_type': decision.buy_type if decision else latest_buy.get('buy_type', '未知'),
                'buy_reason': decision.buy_reason if decision else latest_buy.get('reason', ''),
                'filter_tags': filter_tags,
                'outcome_type': outcome.outcome_type if outcome else 'unknown',
                'event_counts': {
                    'buy_decision': len(buy_events),
                    'cancel_decision': len(cancel_events),
                    'sell_decision': len(sell_events),
                    'order_submitted': len(order_events),
                    'watchlist_enter': len(watchlist_enter_events),
                    'watchlist_release': len(watchlist_release_events),
                    'blacklist_enter': len(blacklist_events),
                    'break_episode_start': len(groups.get('break_episode_start', [])),
                    'break_episode_end': len(groups.get('break_episode_end', [])),
                },
                'timestamps': {
                    'first_limit_up': groups.get('limit_up', [{}])[0].get('timestamp') if groups.get('limit_up') else None,
                    'last_limit_up': groups.get('limit_up', [{}])[-1].get('timestamp') if groups.get('limit_up') else None,
                    'latest_buy_decision': latest_buy.get('timestamp'),
                    'latest_cancel_decision': latest_cancel.get('timestamp'),
                    'latest_sell_decision': latest_sell.get('timestamp'),
                },
                'market_context': {
                    'market_sentiment': latest_buy.get('market_sentiment') or latest_cancel.get('market_sentiment') or latest_sell.get('market_sentiment'),
                    'stock_status': stock_features.get('股票状态'),
                },
                'features': {
                    'seal_amount': stock_features.get('封单金额'),
                    'float_shares': stock_features.get('流通股本'),
                    'first_limit_time': stock_features.get('首次涨停时间'),
                    'watchlist_position_ratio': watch_meta.get('position_ratio'),
                    'watchlist_turnover_rate': watch_meta.get('turnover_rate'),
                    'break_count': break_stats.get('开板次数'),
                    'max_break_duration': break_stats.get('最大回封时间'),
                    'fast_reseal_count': episode_state.get('fast_reseal_count'),
                    'deep_break_count': episode_state.get('deep_break_count'),
                    'episode_duration': episode_state.get('episode_duration'),
                },
                'latest_snapshot': snapshot,
                'latest_events': {
                    'buy': latest_buy,
                    'cancel': latest_cancel,
                    'sell': latest_sell,
                    'blacklist': blacklist_events[-1] if blacklist_events else None,
                    'watchlist_enter': watchlist_enter_events[-1] if watchlist_enter_events else None,
                    'watchlist_release': watchlist_release_events[-1] if watchlist_release_events else None,
                },
                'gene_data': data.get('gene_data', {}).get(stock_code, {}),
            })

        dimension_stats = {
            'buy_type': dict(Counter(item.get('buy_type', '未知') for item in candidates if item.get('buy_type'))),
            'decision_type': dict(Counter(item.get('decision_type', 'unknown') for item in candidates)),
            'outcome_type': dict(Counter(item.get('outcome_type', 'unknown') for item in candidates)),
            'filter_tags': dict(Counter(tag for item in candidates for tag in item.get('filter_tags', []))),
        }

        watchlist_candidates = [
            item for item in candidates
            if (item.get('event_counts', {}) or {}).get('watchlist_enter', 0) > 0
            or (item.get('features', {}) or {}).get('watchlist_position_ratio') is not None
        ]
        blacklist_candidates = [
            item for item in candidates
            if (item.get('event_counts', {}) or {}).get('blacklist_enter', 0) > 0
        ]
        break_candidates = [
            item for item in candidates
            if (item.get('event_counts', {}) or {}).get('break_episode_start', 0) > 0
            or (item.get('features', {}) or {}).get('break_count') is not None
        ]
        break_durations = [
            (item.get('features', {}) or {}).get('episode_duration')
            for item in break_candidates
            if isinstance((item.get('features', {}) or {}).get('episode_duration'), (int, float))
        ]

        special_control_summary = {
            'watchlist': {
                'candidate_count': len(watchlist_candidates),
                'released_count': sum(1 for item in watchlist_candidates if (item.get('event_counts', {}) or {}).get('watchlist_release', 0) > 0),
                'limit_up_count': sum(1 for item in watchlist_candidates if item.get('outcome_type') == 'limit_up'),
                'rejected_limit_up_count': sum(1 for item in watchlist_candidates if item.get('outcome_type') == 'limit_up' and item.get('decision_type') == 'rejected'),
                'approved_broken_count': sum(1 for item in watchlist_candidates if item.get('outcome_type') == 'broken_board' and item.get('decision_type') == 'approved'),
            },
            'blacklist': {
                'candidate_count': len(blacklist_candidates),
                'limit_up_count': sum(1 for item in blacklist_candidates if item.get('outcome_type') == 'limit_up'),
                'rejected_limit_up_count': sum(1 for item in blacklist_candidates if item.get('outcome_type') == 'limit_up' and item.get('decision_type') == 'rejected'),
            },
            'break_episode': {
                'candidate_count': len(break_candidates),
                'with_reseal_count': sum(1 for item in break_candidates if (item.get('features', {}) or {}).get('fast_reseal_count', 0)),
                'deep_break_stock_count': sum(1 for item in break_candidates if (item.get('features', {}) or {}).get('deep_break_count', 0)),
                'avg_duration': round(sum(break_durations) / len(break_durations), 2) if break_durations else 0,
                'max_duration': max(break_durations) if break_durations else 0,
            },
        }

        return {
            'date': data.get('date'),
            'summary': {
                'candidate_count': len(candidates),
                'event_summary': data.get('event_summary', {}),
                'metrics': data.get('metrics', {}),
            },
            'dimension_stats': dimension_stats,
            'candidates': candidates,
            'review_counters': shared_data.get('review_counters', {}),
            'special_control_summary': special_control_summary,
            'top_missed_opportunities': [
                item['stock_code'] for item in candidates
                if item.get('outcome_type') == 'limit_up' and item.get('decision_type') == 'rejected'
            ][:20],
            'avoidable_losses': [
                item['stock_code'] for item in candidates
                if item.get('outcome_type') == 'broken_board' and item.get('decision_type') == 'approved'
            ][:20],
        }

    def _parse_pre_market_filters(self, content: str) -> Dict[str, List[str]]:
        """
        解析盘前过滤内容
        Parse pre-market filter content
        """
        filters = {}

        # # Parse different filter patterns
        # patterns = [
        #     (r'【停牌】\s*(\d{6}\.\w{2})\s+停牌\s+\d+', '停牌'),
        #     (r'(\d{6}\.\w{2})\s+停牌\s+\d+', '停牌'),
        #     (r'【ST股】\s*(\d{6}\.\w{2})', 'ST'),
        #     (r'【未上市/新上市】\s*(\d{6}\.\w{2})', '新股'),
        #     (r'【上市时间小于100天】\s*(\d{6}\.\w{2})', '次新股'),
        #     (r'【股价小于2】\s*(\d{6}\.\w{2})', '低价股'),
        # ]

        # for pattern, reason in patterns:
        #     matches = re.findall(pattern, content)
        #     for stock_code in matches:
        #         if stock_code not in filters:
        #             filters[stock_code] = reason

        # Parse filter statistics lines
        # Example: 日期：20250910，流通市值小于1亿的股票数量：48
        stat_patterns = [
            (r'流通市值小于1亿的股票.*?\[(.*?)\]', '小市值'),
            (r'涨停价在60日均线以下的股票.*?\[(.*?)\]', '均线下方'),
            (r'近五日均成交额小于1亿的股票.*?\[(.*?)\]', '成交低迷'),
            (r'昨日收盘价小于5元的股票.*?\[(.*?)\]', '低价股'),
            (r'近五日有涨停的股票数量.*?\[(.*?)\]', '非严格首板'),
            (r'历史涨停次日无良好溢价的股票.*?\[(.*?)\]', '溢价差'),
            (r'近一年无涨停的股票.*?\[(.*?)\]', '无涨停基因'),
            (r'首版封板率小于0.7的股票.*?\[(.*?)\]', '封板率低'),
        ]

        for pattern, reason in stat_patterns:
            matches = re.findall(pattern, content)
            for stock_list_str in matches:
                # Parse stock codes from list string
                stock_codes = re.findall(r"'(\d{6}\.\w{2})'", stock_list_str)
                logger.info(
                    f'Pre-market filter - {reason}: {len(stock_codes)}')
                for stock_code in stock_codes:
                    if stock_code not in filters:
                        filters[stock_code] = [reason]
                    else:
                        filters[stock_code].append(reason)

        logger.info(f'盘前过滤总共过滤掉: {len(filters)} 只股票')

        return filters

    def _parse_not_buy_reasons(self, content: str) -> Dict[str, List[str]]:
        """
        解析详细的不买入原因
        Parse detailed not-buy reasons
        
        日志格式:
        - 实盘模式: [不买入-{原因}] {stock_code} ... 或 [不买入-{原因}] [{排板/扫板}] {stock_code} ...
        - 影子模式: [影子信号] [不买入-{原因}] {stock_code} ... 或 [影子信号] [不买入-{原因}] [{排板/扫板}] {stock_code} ...
        
        不买入原因标签:
        - 市场情绪: Market sentiment too low
        - 黑名单: In blacklist
        - 首次涨停时间: First limit-up time too late
        - 扫板时间: Scan time too late
        - 流通股本异常: Abnormal float shares
        - 换手率: Turnover rate too low
        - 量比: Volume ratio too low
        - 量比异常: Volume ratio abnormal
        - 板块效应: Insufficient sector effect
        - 资金流入: Insufficient capital inflow
        - 封单额: Seal amount too low
        - 封单量: Seal volume too low
        - 封单量+板块效应: Seal volume + sector effect insufficient
        - 拉板资金: Pull-up capital too high
        - 价格下跌: Price dropping
        """
        logger.info(f'开始解析不买入原因 (交易模式: {self.trading_mode})')
        not_buy_dict = defaultdict(set)  # Use set to avoid duplicates

        # 根据交易模式选择不同的正则表达式
        # 日志格式: {log_prefix}[不买入-{原因}] [{排板/扫板}] {stock_code} ...
        # 其中 log_prefix 在实盘模式下为空，影子模式下为 "[影子信号] "
        if self.trading_mode == 'live':
            # 实盘模式：[不买入-XXX] {stock_code} 或 [不买入-XXX] [排板/扫板] {stock_code}
            # 注意：实盘模式下没有 [影子信号] 前缀，但要排除影子模式的日志
            pattern = r'(?<!\[影子信号\]\s)\[不买入-([^\]]+)\]\s*(?:\[([^\]]+)\])?\s*(\d{6}\.\w{2})'
        else:
            # 影子模式：[影子信号] [不买入-XXX] {stock_code} 或 [影子信号] [不买入-XXX] [排板/扫板] {stock_code}
            pattern = r'\[影子信号\]\s*\[不买入-([^\]]+)\]\s*(?:\[([^\]]+)\])?\s*(\d{6}\.\w{2})'
        
        matches = re.findall(pattern, content)
        logger.info(f'找到 {len(matches)} 条不买入记录')

        for reason, board_type, stock_code in matches:
            # Clean up and simplify the reason tag
            reason_tag = reason.strip()

            # Handle special cases where we want to keep the tag simple
            if '封单量+板块效应' in reason_tag:
                reason_tag = '封单量+板块效应'
            elif '封单量' in reason_tag:
                reason_tag = '封单量'
            elif '封单额' in reason_tag:
                reason_tag = '封单额'
            elif '板块效应' in reason_tag:
                reason_tag = '板块效应'
            elif '市场情绪' in reason_tag:
                reason_tag = '市场情绪'
            elif '换手率' in reason_tag:
                reason_tag = '换手率'
            elif '量比异常' in reason_tag:
                reason_tag = '量比异常'
            elif '量比' in reason_tag:
                reason_tag = '量比'
            elif '黑名单' in reason_tag:
                # 保持黑名单标签，后续会在 _categorize_strategy_decisions 中细化
                reason_tag = '黑名单'
            elif '首次涨停时间' in reason_tag:
                reason_tag = '首次涨停时间'
            elif '扫板时间' in reason_tag:
                reason_tag = '扫板时间'
            elif '流通股本异常' in reason_tag:
                reason_tag = '流通股本异常'
            elif '资金流入' in reason_tag:
                reason_tag = '资金流入'
            elif '拉板资金' in reason_tag:
                reason_tag = '拉板资金'
            elif '价格下跌' in reason_tag:
                reason_tag = '价格下跌'
            else:
                logger.error(f'未知,{stock_code},{reason_tag}')

            # Add board type as suffix if needed
            full_tag = f"{reason_tag}_{board_type}" if board_type else reason_tag
            not_buy_dict[stock_code].add(full_tag)

        # Convert sets back to lists for final output
        result = {}
        for stock_code, reasons in not_buy_dict.items():
            result[stock_code] = list(reasons)

        return result

    def _parse_cancel_reasons(self, content: str) -> Dict[str, List[str]]:
        """
        解析详细的撤单原因
        Parse detailed cancel reasons
        
        日志格式:
        - 实盘模式: {stock_code} {stock_name} {撤单原因}，撤单
        - 影子模式: [影子信号] {stock_code} {stock_name} {撤单原因}，撤单
        
        基于should_cancel函数提取所有撤单原因类型:
        撤买原因:
        - 封单金额小于阈值 -> Tag: 封单不足
        - 封单金额变化率 < -20% -> Tag: 封单变化
        - 换手率超过阈值 -> Tag: 换手率超限
        - 个股资金流入不存在 -> Tag: 资金流入不足
        - 尾盘未成交 -> Tag: 尾盘未成交
        
        撤卖原因:
        - 跌破止损位 -> Tag: 止损
        - 尾盘卖出未成交 -> Tag: 尾盘清仓
        """
        logger.info(f'开始解析撤单原因 (交易模式: {self.trading_mode})')
        reasons = defaultdict(set)

        # 根据交易模式选择不同的正则表达式
        # 日志格式: {log_prefix}{stock_code} {stock_name} {撤单原因}，撤单
        # 其中 log_prefix 在实盘模式下为空，影子模式下为 "[影子信号] "
        if self.trading_mode == 'live':
            # 实盘模式：{stock_code} {stock_name} {撤单原因}，撤单
            # 注意：实盘模式下没有 [影子信号] 前缀，需要排除影子模式的日志
            pattern = r'(?<!\[影子信号\]\s)(\d{6}\.\w{2})\s+\S+\s+(.+?)(?:，撤单|$)'
        else:
            # 影子模式：[影子信号] {stock_code} {stock_name} {撤单原因}，撤单
            pattern = r'\[影子信号\]\s*(\d{6}\.\w{2})\s+\S+\s+(.+?)(?:，撤单|$)'
        
        lines = content.split('\n')
        for i, line in enumerate(lines):
            # 检查是否是撤单原因行 (跟在委托撤单日志后面)
            if '，撤单' in line or ('撤单' in line and re.search(r'\d{6}\.\w{2}\s+\S+\s+', line)):
                match = re.search(pattern, line)
                if match:
                    stock_code = match.group(1)
                    reason_text = match.group(2).strip()
                    
                    # 解析撤单原因类型
                    if '封单金额' in reason_text and '变化率' not in reason_text:
                        reasons[stock_code].add('封单不足')
                    elif '封单金额变化率' in reason_text:
                        reasons[stock_code].add('封单变化')
                    elif '换手率' in reason_text:
                        reasons[stock_code].add('换手率超限')
                    elif '个股资金流入不存在' in reason_text:
                        reasons[stock_code].add('资金流入不足')
                    elif '尾盘未成交' in reason_text and '卖出' not in reason_text:
                        reasons[stock_code].add('尾盘未成交')
                    elif '尾盘卖出未成交' in reason_text:
                        reasons[stock_code].add('尾盘清仓')
                    elif '跌破止损位' in reason_text:
                        reasons[stock_code].add('止损')
                    elif '板块效应不存在' in reason_text:
                        reasons[stock_code].add('板块效应不足')
                    else:
                        # 保留原始原因
                        if reason_text and '撤单' not in reason_text:
                            reasons[stock_code].add(reason_text)

        logger.info(f'解析到 {len(reasons)} 只股票的撤单原因：{dict(reasons)}')
        return {k: list(v) for k, v in reasons.items()}

    def _parse_limit_up_list(self, content: str) -> List[str]:
        """Parse limit-up stock list"""
        stocks = []

        # Pattern 1: From summary line - 涨停股票: xxx, xxx
        pattern = r'-\s+涨停股票:\s+([\d\.\w\s,]+)'
        matches = re.findall(pattern, content)
        for match in matches:
            stock_codes = re.findall(r'(\d{6}\.\w{2})', match)
            stocks.extend(stock_codes)

        return list(set(stocks))

    def _parse_first_limit_list(self, content: str) -> List[str]:
        """Parse first limit-up stock list"""
        stocks = []

        # Pattern: 首次涨停股票: stock_codes
        pattern = r'-\s+首次涨停股票:\s+([\d\.\w\s,]+)'
        matches = re.findall(pattern, content)

        for match in matches:
            stock_codes = re.findall(r'(\d{6}\.\w{2})', match)
            stocks.extend(stock_codes)

        return list(set(stocks))

    def _parse_break_list(self, content: str) -> List[str]:
        """Parse broken board stock list"""
        stocks = []

        # Pattern: 炸板股票: stock_codes
        pattern = r'-\s+炸板股票:\s+([\d\.\w\s,]+)'
        matches = re.findall(pattern, content)

        for match in matches:
            stock_codes = re.findall(r'(\d{6}\.\w{2})', match)
            stocks.extend(stock_codes)

        return list(set(stocks))

    def _parse_filter_statistics(self, content: str) -> Dict:
        """
        解析过滤统计信息
        Parse filter statistics
        """
        stats = {}

        # Parse initial screening results
        # Pattern: 日期：20250910，初筛过滤掉的股票数量：3601，剩余数量：762
        pattern = r'初筛过滤掉的股票数量：(\d+)，剩余数量：(\d+)'
        match = re.search(pattern, content)
        if match:
            stats['total_filtered'] = int(match.group(1))
            stats['remaining_after_filter'] = int(match.group(2))

        # Parse strong stock count
        # Pattern: 日期：20250910，强势股票数量：762
        pattern = r'强势股票数量：(\d+)'
        match = re.search(pattern, content)
        if match:
            stats['strong_stock_count'] = int(match.group(1))

        # Parse individual filter counts
        filter_patterns = [
            (r'流通市值小于1亿的股票数量：(\d+)', '小市值'),
            (r'涨停价在60日均线以下的股票数量：(\d+)', '均线下方'),
            (r'近五日均成交额小于1亿的股票数量：(\d+)', '成交低迷'),
            (r'昨日收盘价小于5元的股票数量：(\d+)', '低价股'),
            (r'近五日有涨停的股票数量：(\d+)', '非严格首板'),
            (r'历史涨停次日无良好溢价的股票数量：(\d+)', '溢价差'),
            (r'近一年无涨停的股票数量：(\d+)', '无涨停基因'),
            (r'首版封板率小于0.7的股票数量：(\d+)', '封板率低'),
        ]

        if 'filter_counts' not in stats:
            stats['filter_counts'] = {}

        for pattern, filter_name in filter_patterns:
            match = re.search(pattern, content)
            if match:
                stats['filter_counts'][filter_name] = int(match.group(1))

        return stats


# NOTE: EnhancedDataCollector 已检查完

# ============================================================================
# Filter Performance Analyzer
# ============================================================================


class FilterPerformanceAnalyzer:
    """Analyze filter performance with precision/recall metrics"""
    def __init__(self, data: Dict):
        self.data = data
        self.filter_metrics = {}
        self.all_filters = {
            # Pre-market filter tags (盘前过滤标签)
            '小市值',  # 流通市值小于1亿的股票
            '均线下方',  # 涨停价在60日均线以下的股票
            '成交低迷',  # 近五日均成交额小于1亿的股票
            '低价股',  # 昨日收盘价小于5元的股票
            '非严格首板',  # 近五日有涨停的股票数量大于0
            '溢价差',  # 历史涨停次日无良好溢价的股票
            '无涨停基因',  # 近一年无涨停的股票
            '封板率低',  # 首版封板率小于0.7的股票

            # Not buy reason tags (不买入原因标签)
            '市场情绪',  # Market sentiment too low
            # '黑名单',  # In blacklist
            '首次涨停时间',  # First limit-up time too late
            '扫板时间',  # Scan time too late
            '流通股本异常',  # Abnormal float shares
            '换手率',  # Turnover rate too low
            '量比',  # Volume ratio too low
            '量比异常',  # Volume ratio abnormal
            '板块效应',  # Insufficient sector effect
            '资金流入',  # Insufficient capital inflow
            '封单额',  # Seal amount too low
            '封单量',  # Seal volume too low
            '封单量+板块效应',  # Seal volume + sector effect insufficient
            '拉板资金',  # Pull-up capital too high
            '价格下跌',  # Price dropping

            # Cancel reason tags (撤单原因标签)
            '封单不足',  # 封单金额小于阈值
            '封单变化',  # 封单金额变化率 < -20%
            '换手率超限',  # 换手率超过阈值
            '板块效应不足',  # 板块效应不存在
            '资金流入不足',  # 个股资金流入不存在
            # '尾盘未成交',  # 尾盘未成交
            # '止损',        # 跌破止损位
            # '尾盘清仓',    # 尾盘未成交(卖出)

            # Blacklist reason tags (黑名单原因标签)
            '换手率过高',  # 换手率相关问题
            '开板次数过多',  # 开板次数过多
            '开板时间过长',  # 开板时间过长
            '开板后跌幅过大',  # 开板后股价下跌超过阈值
            '未知原因',  # 其他未分类的黑名单原因
        }

        self.pre_market_filters = {
            '小市值',  # 流通市值小于1亿的股票
            '均线下方',  # 涨停价在60日均线以下的股票
            '成交低迷',  # 近五日均成交额小于1亿的股票
            '低价股',  # 昨日收盘价小于5元的股票
            '非严格首板',  # 近五日有涨停的股票数量大于0
            '溢价差',  # 历史涨停次日无良好溢价的股票
            '无涨停基因',  # 近一年无涨停的股票
            '封板率低',  # 首版封板率小于0.7的股票
        }

    def analyze_all_filters(self) -> Dict[str, FilterMetrics]:
        """Analyze performance of all filters"""
        logger.info("Analyzing filter performance")
        # Analyze each filter
        for filter_name in self.all_filters:
            self.filter_metrics[filter_name] = self._analyze_single_filter(
                filter_name)

        return self.filter_metrics

    def _analyze_single_filter(self, filter_name: str) -> FilterMetrics:
        """Analyze a single filter's performance"""
        metrics = FilterMetrics(filter_name=filter_name)

        # Get stocks affected by this filter
        filtered_stocks = set()

        # Check pre-market filters
        for stock, f_list in self.data.get('detailed_filters',
                                           {}).get('pre_market', {}).items():
            if isinstance(f_list, list):
                if filter_name in f_list:
                    filtered_stocks.add(stock)
            elif f_list == filter_name:
                filtered_stocks.add(stock)

        # Check real-time filters
        for stock, f_list in self.data.get('detailed_filters',
                                           {}).get('real_time', {}).items():
            if isinstance(f_list, list):
                if filter_name in f_list:
                    filtered_stocks.add(stock)
            elif f_list == filter_name:
                filtered_stocks.add(stock)

        # Check cancel filters
        for stock, f_list in self.data.get('detailed_filters',
                                           {}).get('cancel', {}).items():
            if isinstance(f_list, list):
                if filter_name in f_list:
                    filtered_stocks.add(stock)
            elif f_list == filter_name:
                filtered_stocks.add(stock)

        # check blacklist filters
        for stock, f_name in self.data.get('blacklist', {}).items():
            if filter_name == f_name:
                filtered_stocks.add(stock)

        # Calculate metrics based on outcomes
        outcomes = self.data.get('market_outcomes', {})
        decisions = self.data.get('strategy_decisions', {})

        for stock in outcomes:
            if outcomes[stock].outcome_type == 'limit_up':
                if stock in filtered_stocks:
                    metrics.false_positive += 1  # Filter missed a good stock
                    metrics.filtered_limit_up_stocks.append(stock)  # 记录误伤的涨停股
                else:
                    metrics.true_negative += 1  # Filter passed a good stock
            elif outcomes[stock].outcome_type == 'broken_board':
                if stock in filtered_stocks:
                    metrics.true_positive += 1  # Filter blocked a bad stock
                    metrics.filtered_broken_stocks.append(stock)  # 记录正确过滤的炸板股
                else:
                    metrics.false_negative += 1  # Filter missed a bad stock

        # 策略成功过滤掉的未涨停股票 (只记录非盘前过滤的情况)
        if filter_name not in self.pre_market_filters:
            for stock in filtered_stocks:
                if stock not in outcomes:
                    metrics.true_positive += 1  # Filter blocked a bad stock
                    metrics.filtered_other_stocks.append(stock)  # 记录其他被过滤的股票

        for stock in decisions:
            # 对于扫板买入但从未涨停的股票，应该被过滤掉。
            if decisions[
                    stock].decision_type == 'approved' and stock not in outcomes:
                metrics.false_negative += 1  # Filter missed a bad stock

        # Calculate metrics
        metrics.calculate_metrics()

        return metrics


# ============================================================================
# Missed Opportunity Analyzer
# ============================================================================


class MissedOpportunityAnalyzer:
    """Deep dive analysis of missed opportunities"""
    def __init__(self, data: Dict):
        self.data = data
        self.missed_opportunities = []

    def analyze_missed_opportunities(self) -> List[Dict]:
        """Identify and analyze all missed opportunities"""
        logger.info("Analyzing missed opportunities")

        outcomes = self.data.get('market_outcomes', {})
        decisions = self.data.get('strategy_decisions', {})

        for stock, outcome in outcomes.items():
            if outcome.outcome_type == 'limit_up':
                if stock in decisions and decisions[
                        stock].decision_type == 'rejected':
                    # This is a missed opportunity
                    analysis = self._analyze_single_miss(
                        stock, decisions[stock])
                    self.missed_opportunities.append(analysis)

        # Sort by impact
        self.missed_opportunities.sort(key=lambda x: x['impact_score'],
                                       reverse=True)

        return self.missed_opportunities

    def _analyze_single_miss(self, stock: str,
                             decision: StrategyDecision) -> Dict:
        """Analyze a single missed opportunity"""
        analysis = {
            'stock_code': stock,
            'rejection_reason': decision.decision_reason,
            'filter_tags': decision.filter_tags,
            'impact_score': 0,
            'pattern': '',
            'recommendation': '',
            'gene_score': 0,
            'gene_data': decision.gene_data
        }

        # Calculate impact score based on various factors
        impact_score = 10  # Base score for missing a limit-up

        # Check if it was a first limit-up (higher impact)
        if stock in self.data.get('log_data', {}).get('first_limit_list', []):
            impact_score += 5
            analysis['pattern'] = '首板股'

        # Incorporate Limit-Up Gene Score
        gene_score = decision.gene_data.get('涨停基因打分', 0)
        analysis['gene_score'] = gene_score

        if gene_score > 80:
            impact_score += 10
            analysis['pattern'] += ' 强基因' if analysis['pattern'] else '强基因'
            analysis[
                'recommendation'] += f'该股涨停基因评分高达{gene_score:.1f}，属重点错失对象\n'
        elif gene_score > 60:
            impact_score += 5

        # Check specific gene metrics
        seal_rate = decision.gene_data.get('首板封板率', 0)
        if seal_rate > 0.8:
            analysis['recommendation'] += f'历史首板封板率高({seal_rate:.1%})，股性较好\n'

        red_rate = decision.gene_data.get('首板次日收盘红盘率', 0)
        if red_rate > 0.7:
            analysis['recommendation'] += f'首板次日红盘率高({red_rate:.1%})，溢价预期强\n'
            impact_score += 2

        premium_rate = decision.gene_data.get('涨停次日收盘溢价超5%比例', 0)
        if premium_rate > 0.5:
            analysis['recommendation'] += f'高溢价比例高({premium_rate:.1%})，连板潜力大\n'
            impact_score += 3

        analysis['impact_score'] = impact_score

        # Generate recommendation based on filters
        if '封单不足' in decision.filter_tags:
            analysis['recommendation'] += '考虑降低封单额要求，当前可能过于严格\n'
        if '换手率超限' in decision.filter_tags:
            analysis['recommendation'] += '考虑放宽换手率限制，部分活跃股票换手率较高\n'
        if '板块效应不足' in decision.filter_tags:
            analysis['recommendation'] += '重新评估板块效应计算方法，可能遗漏独立强势股\n'

        if not analysis['recommendation']:
            analysis['recommendation'] += '建议人工复核该股票，判断过滤逻辑是否合理\n'

        return analysis

    def get_pattern_summary(self) -> Dict:
        """Summarize patterns in missed opportunities"""
        patterns = defaultdict(list)

        for miss in self.missed_opportunities:
            for tag in miss['filter_tags']:
                patterns[tag].append(miss['stock_code'])

        summary = {}
        for tag, stocks in patterns.items():
            summary[tag] = {
                'count':
                len(stocks),
                'stocks':
                stocks,
                'percentage':
                len(stocks) / len(self.missed_opportunities) *
                100 if self.missed_opportunities else 0
            }
        logger.info(summary)
        return summary


# ============================================================================
# Report Generator
# ============================================================================


class EnhancedReportGenerator:
    """Generate comprehensive HTML and JSON reports"""
    def __init__(self, data: Dict, analysis_results: Dict):
        self.data = data
        self.analysis = analysis_results
        self.report_dir = ReviewConfig.REPORT_DIR

    def generate_html_report(self) -> str:
        """Generate interactive HTML report"""
        html_template = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>打板策略复盘报告 - {date}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{
            font-family: 'Microsoft YaHei', Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #2c3e50;
            border-bottom: 3px solid #3498db;
            padding-bottom: 10px;
            background: linear-gradient(90deg, #3498db, #2980b9);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}
        h2 {{
            color: #34495e;
            margin-top: 30px;
            border-bottom: 2px solid #ecf0f1;
            padding-bottom: 8px;
        }}
        .summary-cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }}
        .card {{
            padding: 20px;
            border-radius: 12px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            transition: transform 0.3s ease;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        .card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 10px 25px rgba(0,0,0,0.15);
        }}
        .card.success {{
            background: linear-gradient(135deg, #84fab0 0%, #8fd3f4 100%);
            color: #2c3e50;
        }}
        .card.warning {{
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        }}
        .card.info {{
            background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
        }}
        .card.primary {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }}
        .card.danger {{
            background: linear-gradient(135deg, #fa709a 0%, #fee140 100%);
        }}
        .metric-value {{
            font-size: 2.2em;
            font-weight: bold;
            margin: 10px 0;
        }}
        .metric-label {{
            font-size: 0.9em;
            opacity: 0.95;
            font-weight: 500;
        }}
        .stats-summary {{
            background: #f8f9fa;
            border-radius: 8px;
            padding: 15px;
            margin: 20px 0;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
        }}
        .stat-item {{
            text-align: center;
        }}
        .stat-value {{
            font-size: 1.5em;
            font-weight: bold;
            color: #3498db;
        }}
        .stat-label {{
            font-size: 0.85em;
            color: #7f8c8d;
            margin-top: 5px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 10px rgba(0,0,0,0.05);
            table-layout: fixed;
        }}
        th {{
            background: linear-gradient(135deg, #3498db, #2980b9);
            color: white;
            padding: 12px;
            text-align: left;
            position: sticky;
            top: 0;
            z-index: 10;
            white-space: nowrap;
        }}
        td {{
            padding: 10px;
            border-bottom: 1px solid #ecf0f1;
            word-wrap: break-word;
            overflow-wrap: break-word;
        }}
        /* 特定表格的自适应布局 */
        table.auto-layout {{
            table-layout: auto;
        }}
        tr:hover {{
            background: #f8f9fa;
            transition: background 0.3s ease;
        }}
        tr.highlight-row {{
            background: #fffbf0;
            font-weight: 500;
        }}
        tr.highlight-row:hover {{
            background: #fff5e1;
        }}
        .strategy-held {{
            display: inline-block;
            margin-left: 8px;
            padding: 2px 6px;
            background: #ffc107;
            color: #fff;
            border-radius: 3px;
            font-size: 0.75em;
            font-weight: bold;
        }}
        .recommendation {{
            background: linear-gradient(135deg, #fff3cd, #ffe8a1);
            border-left: 4px solid #ffc107;
            padding: 15px;
            margin: 15px 0;
            border-radius: 8px;
        }}
        .recommendation h3 {{
            margin-top: 0;
            color: #856404;
        }}
        .chart-container {{
            position: relative;
            height: 400px;
            margin: 30px 0;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.05);
        }}
        .tag {{
            display: inline-block;
            padding: 4px 10px;
            margin: 2px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 500;
        }}
        .tag.approved {{
            background: #d4edda;
            color: #155724;
        }}
        .tag.rejected {{
            background: #f8d7da;
            color: #721c24;
        }}
        .tag.success {{
            background: #d1ecf1;
            color: #0c5460;
        }}
        .tag.info {{
            background: linear-gradient(135deg, #e3f2fd, #bbdefb);
            color: #0277bd;
            border: 1px solid #81d4fa;
        }}
        .tag.warning {{
            background: linear-gradient(135deg, #fff8e1, #ffecb3);
            color: #ef6c00;
            border: 1px solid #ffcc02;
        }}
        .tag.danger {{
            background: linear-gradient(135deg, #ffebee, #ffcdd2);
            color: #c62828;
            border: 1px solid #ef5350;
        }}
        .tag.default {{
            background: #e9ecef;
            color: #495057;
        }}
        .tag.primary {{
            background: linear-gradient(135deg, #f3e5f5, #e1bee7);
            color: #7b1fa2;
            border: 1px solid #ba68c8;
        }}
        /* Stock list collapsible styles */
        .collapsible-stocks {{
            display: block;
            width: 100%;
        }}
        .stock-list-full {{
            word-break: break-word;
            white-space: normal;
            max-width: 100%;
        }}
        td {{
            vertical-align: top;
        }}
        .collapsible {{
            background-color: #f1f1f1;
            color: #444;
            cursor: pointer;
            padding: 18px;
            width: 100%;
            border: none;
            text-align: left;
            outline: none;
            font-size: 15px;
            margin: 10px 0;
            border-radius: 8px;
            transition: background-color 0.3s ease;
        }}
        .collapsible:hover {{
            background-color: #ddd;
        }}
        .collapsible.active {{
            background-color: #3498db;
            color: white;
        }}
        .content {{
            padding: 0 18px;
            display: none;
            overflow: hidden;
            background-color: #f9f9f9;
            border-radius: 0 0 8px 8px;
            margin-top: -10px;
        }}
        .alert {{
            padding: 12px;
            margin: 15px 0;
            border-radius: 8px;
        }}
        .alert-info {{
            background: #d1ecf1;
            color: #0c5460;
            border-left: 4px solid #17a2b8;
        }}
        .alert-success {{
            background: #d4edda;
            color: #155724;
            border-left: 4px solid #28a745;
        }}
        .alert-warning {{
            background: #fff3cd;
            color: #856404;
            border-left: 4px solid #ffc107;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 打板策略复盘报告 - {date}</h1>
        
        <!-- Executive Summary -->
        <section>
            <h2>一、执行摘要</h2>
            
            <!-- Key Metrics Cards -->
            <div class="summary-cards">
                <div class="card success">
                    <div class="metric-label">买入成功率</div>
                    <div class="metric-value">{success_rate}%</div>
                </div>
                <div class="card info">
                    <div class="metric-label">机会捕获率</div>
                    <div class="metric-value">{capture_rate}%</div>
                </div>
                <div class="card warning">
                    <div class="metric-label">风险规避率</div>
                    <div class="metric-value">{avoid_rate}%</div>
                </div>
                <div class="card primary">
                    <div class="metric-label">过滤器效率</div>
                    <div class="metric-value">{filter_efficiency}%</div>
                </div>
            </div>
            
            <!-- Statistics Summary -->
            <div class="stats-summary">
                <div class="stat-item">
                    <div class="stat-value">{total_limit_up}</div>
                    <div class="stat-label">涨停股票数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{total_broken_board}</div>
                    <div class="stat-label">炸板股票数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{strategy_approved}</div>
                    <div class="stat-label">策略认可股票数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{strategy_bought}</div>
                    <div class="stat-label">策略买入数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{strategy_rejected}</div>
                    <div class="stat-label">策略拒绝数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{first_limit_up}</div>
                    <div class="stat-label">首板涨停数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{broken_rate}%</div>
                    <div class="stat-label">炸板率</div>
                </div>
            </div>
            
            {key_findings}
        </section>
        
        <!-- Stock Categorization -->
        <section>
            <h2>二、股票分类分析</h2>
            {stock_categorization}
        </section>
        
        <!-- Deterministic Review Context -->
        <section>
            <h2>三、结构化事件归因</h2>
            {deterministic_review}
        </section>

        <!-- Filter Performance -->
        <section>
            <h2>四、过滤器效果分析</h2>
            <div class="chart-container">
                <canvas id="filterChart"></canvas>
            </div>
            {filter_analysis}
        </section>

        <!-- Missed Opportunities -->
        <section>
            <h2>五、错失机会分析</h2>
            {missed_opportunities}
        </section>

        <!-- Avoidable Losses -->
        <section>
            <h2>六、可避免损失</h2>
            {avoidable_losses}
        </section>

        <!-- Detailed Stock Analysis -->
        <section>
            <h2>七、个股详细分析</h2>
            <button class="collapsible">点击展开详细数据</button>
            <div class="content">
                {stock_details}
            </div>
        </section>
    </div>
    
    <script>
        // Collapsible sections
        var coll = document.getElementsByClassName("collapsible");
        for (var i = 0; i < coll.length; i++) {{
            coll[i].addEventListener("click", function() {{
                this.classList.toggle("active");
                var content = this.nextElementSibling;
                if (content.style.display === "block") {{
                    content.style.display = "none";
                }} else {{
                    content.style.display = "block";
                }}
            }});
        }}
        
        // Toggle stock list function
        function toggleStockList(event, listId) {{
            event.preventDefault();
            event.stopPropagation();
            var list = document.getElementById(listId);
            var button = event.target.closest('button');
            var toggleText = button.querySelector('.toggle-text');
            
            if (list.style.display === "none" || list.style.display === "") {{
                list.style.display = "block";
                toggleText.textContent = toggleText.textContent.replace('显示全部', '收起').replace('▼', '▲');
                button.style.marginBottom = '8px';
            }} else {{
                list.style.display = "none";
                toggleText.textContent = toggleText.textContent.replace('收起', '显示全部').replace('▲', '▼');
                button.style.marginBottom = '0';
            }}
        }}
        
        // Toggle buy reason
        function toggleReason(id) {{
            var x = document.getElementById(id);
            if (x.style.display === "none") {{
                x.style.display = "block";
            }} else {{
                x.style.display = "none";
            }}
        }}
        
        // Toggle filter detail (for filter analysis section)
        function toggleFilterDetail(id) {{
            var x = document.getElementById(id);
            if (x.style.display === "none") {{
                x.style.display = "block";
            }} else {{
                x.style.display = "none";
            }}
        }}
        
        // Table filtering
        function filterTable(tableId, colIndex) {{
            var input, filter, table, tr, td, i, txtValue;
            input = document.getElementById(tableId === 'missedTable' ? 'missedSearch' : 'detailsSearch');
            filter = input.value.toUpperCase();
            table = document.getElementById(tableId);
            tr = table.getElementsByTagName("tr");
            
            for (i = 0; i < tr.length; i++) {{
                td = tr[i].getElementsByTagName("td")[colIndex];
                if (td) {{
                    txtValue = td.textContent || td.innerText;
                    if (txtValue.toUpperCase().indexOf(filter) > -1) {{
                        tr[i].style.display = "";
                    }} else {{
                        tr[i].style.display = "none";
                    }}
                }}
            }}
        }}
        
        function filterTableByTag(tableId, colIndex) {{
            var select, filter, table, tr, td, i, txtValue;
            select = document.getElementById('missedFilter');
            filter = select.value.toUpperCase();
            table = document.getElementById(tableId);
            tr = table.getElementsByTagName("tr");
            
            for (i = 0; i < tr.length; i++) {{
                td = tr[i].getElementsByTagName("td")[colIndex];
                if (td) {{
                    txtValue = td.textContent || td.innerText;
                    if (filter === "" || txtValue.toUpperCase().indexOf(filter) > -1) {{
                        tr[i].style.display = "";
                    }} else {{
                        tr[i].style.display = "none";
                    }}
                }}
            }}
        }}
        
        // Filter performance chart
        {chart_script}
    </script>
</body>
</html>
        """

        # Calculate summary metrics
        metrics = self.data.get('metrics', {})
        success_rate = metrics.get('success_rate', 0) * 100
        capture_rate = metrics.get('capture_rate', 0) * 100
        avoid_rate = metrics.get('avoidance_rate', 0) * 100
        filter_efficiency = self._calculate_filter_efficiency()

        # Calculate seal rates
        seal_stats = self._calculate_seal_rate()

        # Additional statistics
        total_limit_up = metrics.get('total_limit_up', 0)
        total_broken_board = metrics.get('total_broken_board', 0)
        strategy_bought = metrics.get('strategy_bought', 0)
        strategy_rejected = metrics.get('strategy_rejected', 0)
        first_limit_up = metrics.get('first_limit_up', 0)
        broken_rate = metrics.get('broken_board_rate', 0) * 100

        # Calculate strategy approved count (includes both bought and late cancelled stocks)
        strategy_approved = 0
        if self.data.get('strategy_decisions'):
            for decision in self.data['strategy_decisions'].values():
                if decision.decision_type == 'approved':
                    strategy_approved += 1

        # Generate sections
        key_findings = self._generate_key_findings()

        # Add seal rate analysis to key findings
        seal_analysis_html = f"""
        <div class="stats-summary" style="margin-top: 15px; background: #e8f4f8;">
            <h4>封板率分析</h4>
            <div class="stat-item">
                <div class="stat-value">{seal_stats['total']['rate']:.1f}%</div>
                <div class="stat-label">整体封板率</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{seal_stats['scan']['rate']:.1f}%</div>
                <div class="stat-label">扫板封板率<br><span style="font-size:0.8em">({seal_stats['scan']['success']}/{seal_stats['scan']['count']})</span></div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{seal_stats['queue']['rate']:.1f}%</div>
                <div class="stat-label">排板封板率<br><span style="font-size:0.8em">({seal_stats['queue']['success']}/{seal_stats['queue']['count']})</span></div>
            </div>
        </div>
        """
        # Insert after stats summary in template
        key_findings = seal_analysis_html + key_findings

        stock_categorization = self._generate_stock_categorization()
        deterministic_review = self._generate_deterministic_review()
        filter_analysis = self._generate_filter_analysis()
        missed_opportunities = self._generate_missed_opportunities()
        avoidable_losses = self._generate_avoidable_losses()
        stock_details = self._generate_stock_details()
        chart_script = self._generate_chart_script()

        # Fill template
        html = html_template.format(
            date=self.data['date'],
            success_rate=f"{success_rate:.1f}",
            capture_rate=f"{capture_rate:.1f}",
            avoid_rate=f"{avoid_rate:.1f}",
            filter_efficiency=f"{filter_efficiency:.1f}",
            total_limit_up=total_limit_up,
            total_broken_board=total_broken_board,
            strategy_approved=strategy_approved,
            strategy_bought=strategy_bought,
            strategy_rejected=strategy_rejected,
            first_limit_up=first_limit_up,
            broken_rate=f"{broken_rate:.1f}",
            key_findings=key_findings,
            stock_categorization=stock_categorization,
            deterministic_review=deterministic_review,
            filter_analysis=filter_analysis,
            missed_opportunities=missed_opportunities,
            avoidable_losses=avoidable_losses,
            stock_details=stock_details,
            chart_script=chart_script)

        return html

    def _calculate_seal_rate(self) -> Dict[str, float]:
        """Calculate board sealing success rate by buy type"""
        positions = self.data.get('positions', {})
        decisions = self.data.get('strategy_decisions', {})

        stats = {
            'total': {
                'count': 0,
                'success': 0,
                'rate': 0.0
            },
            'scan': {
                'count': 0,
                'success': 0,
                'rate': 0.0
            },
            'queue': {
                'count': 0,
                'success': 0,
                'rate': 0.0
            }
        }

        if not positions:
            return stats

        for stock in positions:
            if stock not in decisions:
                continue

            decision = decisions[stock]
            buy_type = decision.buy_type

            # Determine outcome
            is_success = False
            if stock in self.data.get('market_outcomes', {}):
                outcome = self.data['market_outcomes'][stock]
                if outcome.outcome_type == 'limit_up':
                    is_success = True

            # Update total stats
            stats['total']['count'] += 1
            if is_success:
                stats['total']['success'] += 1

            # Update type specific stats
            if buy_type == '扫板':
                stats['scan']['count'] += 1
                if is_success:
                    stats['scan']['success'] += 1
            elif buy_type == '排板':
                stats['queue']['count'] += 1
                if is_success:
                    stats['queue']['success'] += 1

        # Calculate rates
        for key in stats:
            if stats[key]['count'] > 0:
                stats[key]['rate'] = (stats[key]['success'] /
                                      stats[key]['count']) * 100

        return stats

    def _calculate_filter_efficiency(self) -> float:
        """Calculate overall filter efficiency - percentage of effective filters based on precision"""
        filter_metrics = self.analysis.get('filter_metrics', {})
        if not filter_metrics:
            return 0.0

        # Count filters with precision > 0.6 as effective
        effective_filters = sum(1 for m in filter_metrics.values()
                                if m.precision > 0.6)
        return (effective_filters /
                len(filter_metrics)) * 100 if filter_metrics else 0.0

    def _render_dimension_cards(self, stats: Dict[str, Any], label_map: Dict[str, str], tag_class: str = 'info') -> str:
        """Render dimension statistics as cards."""
        if not stats:
            return "<p class='alert alert-info'>暂无结构化统计。</p>"

        cards = []
        for key, value in sorted(stats.items(), key=lambda item: item[1], reverse=True)[:8]:
            display = html_lib.escape(str(label_map.get(key, key or '未知')))
            cards.append(f"""
            <div class=\"stat-item\">
                <div class=\"stat-value\">{value}</div>
                <div class=\"stat-label\"><span class=\"tag {tag_class}\">{display}</span></div>
            </div>
            """)
        return "<div class='stats-summary'>" + ''.join(cards) + "</div>"

    def _render_candidate_table(self, candidates: List[Dict[str, Any]]) -> str:
        """Render candidate table for deterministic review."""
        if not candidates:
            return "<p class='alert alert-info'>暂无候选股明细。</p>"

        rows = []
        for candidate in candidates[:15]:
            stock_code = html_lib.escape(str(candidate.get('stock_code', '')))
            decision_type = html_lib.escape(str(candidate.get('decision_type', '未知')))
            outcome_type = html_lib.escape(str(candidate.get('outcome_type', '未知')))
            buy_type = html_lib.escape(str(candidate.get('buy_type', '未知')))
            filter_tags = candidate.get('filter_tags', []) or []
            tags_html = ''.join(
                f'<span class="tag warning">{html_lib.escape(str(tag))}</span>'
                for tag in filter_tags[:4]) or '<span class="tag default">无</span>'
            event_counts = candidate.get('event_counts', {}) or {}
            event_summary = '/'.join([
                f"候选{event_counts.get('candidate_seen', 0)}",
                f"买入{event_counts.get('buy_decision', 0)}",
                f"撤单{event_counts.get('cancel_decision', 0)}",
                f"卖出{event_counts.get('sell_decision', 0)}",
            ])
            rows.append(f"""
                <tr>
                    <td><strong>{stock_code}</strong></td>
                    <td><span class="tag primary">{decision_type}</span></td>
                    <td><span class="tag info">{outcome_type}</span></td>
                    <td><span class="tag default">{buy_type}</span></td>
                    <td>{tags_html}</td>
                    <td>{html_lib.escape(event_summary)}</td>
                </tr>
            """)

        return """
        <div style="overflow-x:auto;">
            <table class="auto-layout">
                <thead>
                    <tr>
                        <th>股票</th>
                        <th>决策</th>
                        <th>结果</th>
                        <th>买入类型</th>
                        <th>标签</th>
                        <th>事件计数</th>
                    </tr>
                </thead>
                <tbody>
        """ + ''.join(rows) + """
                </tbody>
            </table>
        </div>
        """

    def _render_ranked_list(self, items: List[Dict[str, Any]], title: str, empty_text: str) -> str:
        """Render ranked review items."""
        html = f"<h4>{html_lib.escape(title)}</h4>"
        if not items:
            return html + f"<p class='alert alert-info'>{html_lib.escape(empty_text)}</p>"

        rows = []
        for idx, item in enumerate(items[:10], 1):
            stock_code = html_lib.escape(str(item.get('stock_code', '')))
            score = item.get('impact_score', item.get('loss_score', 0))
            reason = html_lib.escape(str(item.get('rejection_reason', item.get('decision_reason', ''))))
            recommendation = html_lib.escape(str(item.get('recommendation', item.get('analysis', '')))).replace('\n', '<br>')
            tags = item.get('filter_tags', []) or []
            tags_html = ''.join(
                f'<span class="tag warning">{html_lib.escape(str(tag))}</span>'
                for tag in tags[:4]) or '<span class="tag default">无</span>'
            rows.append(f"""
                <tr>
                    <td>{idx}</td>
                    <td><strong>{stock_code}</strong></td>
                    <td>{score}</td>
                    <td>{reason or '-'}</td>
                    <td>{tags_html}</td>
                    <td>{recommendation or '-'}</td>
                </tr>
            """)

        return html + """
        <div style="overflow-x:auto;">
            <table class="auto-layout">
                <thead>
                    <tr>
                        <th>序号</th>
                        <th>股票</th>
                        <th>评分</th>
                        <th>原因</th>
                        <th>标签</th>
                        <th>建议</th>
                    </tr>
                </thead>
                <tbody>
        """ + ''.join(rows) + """
                </tbody>
            </table>
        </div>
        """

    def _generate_special_control_summary(self, summary: Dict[str, Any]) -> str:
        """Render summarized special control metrics."""
        if not summary:
            return "<p class='alert alert-info'>暂无特殊管控统计摘要。</p>"

        watchlist = summary.get('watchlist', {}) or {}
        blacklist = summary.get('blacklist', {}) or {}
        break_episode = summary.get('break_episode', {}) or {}

        return f"""
        <div class="stats-summary">
            <div class="stat-item"><div class="stat-value">{watchlist.get('candidate_count', 0)}</div><div class="stat-label">观察名单样本</div></div>
            <div class="stat-item"><div class="stat-value">{watchlist.get('released_count', 0)}</div><div class="stat-label">观察名单释放数</div></div>
            <div class="stat-item"><div class="stat-value">{watchlist.get('rejected_limit_up_count', 0)}</div><div class="stat-label">观察名单误伤涨停</div></div>
            <div class="stat-item"><div class="stat-value">{watchlist.get('approved_broken_count', 0)}</div><div class="stat-label">观察名单后仍炸板</div></div>
            <div class="stat-item"><div class="stat-value">{blacklist.get('candidate_count', 0)}</div><div class="stat-label">黑名单样本</div></div>
            <div class="stat-item"><div class="stat-value">{blacklist.get('rejected_limit_up_count', 0)}</div><div class="stat-label">黑名单误伤涨停</div></div>
            <div class="stat-item"><div class="stat-value">{break_episode.get('candidate_count', 0)}</div><div class="stat-label">炸板样本</div></div>
            <div class="stat-item"><div class="stat-value">{break_episode.get('with_reseal_count', 0)}</div><div class="stat-label">快速回封样本</div></div>
            <div class="stat-item"><div class="stat-value">{break_episode.get('deep_break_stock_count', 0)}</div><div class="stat-label">深炸样本</div></div>
            <div class="stat-item"><div class="stat-value">{break_episode.get('avg_duration', 0)}</div><div class="stat-label">平均炸板时长</div></div>
            <div class="stat-item"><div class="stat-value">{break_episode.get('max_duration', 0)}</div><div class="stat-label">最大炸板时长</div></div>
        </div>
        """

    def _generate_special_control_diagnostics(self, summary: Dict[str, Any]) -> str:
        """Render conclusion-style diagnostics for watchlist, blacklist and break episodes."""
        if not summary:
            return "<p class='alert alert-info'>暂无特殊管控诊断结论。</p>"

        watchlist = summary.get('watchlist', {}) or {}
        blacklist = summary.get('blacklist', {}) or {}
        break_episode = summary.get('break_episode', {}) or {}

        def safe_ratio(numerator: Any, denominator: Any) -> float:
            try:
                numerator_value = float(numerator or 0)
                denominator_value = float(denominator or 0)
                if denominator_value <= 0:
                    return 0.0
                return numerator_value / denominator_value
            except (TypeError, ValueError):
                return 0.0

        diagnostics = []

        watchlist_count = int(watchlist.get('candidate_count', 0) or 0)
        watchlist_false_reject_ratio = safe_ratio(watchlist.get('rejected_limit_up_count', 0), watchlist_count)
        watchlist_broken_ratio = safe_ratio(watchlist.get('approved_broken_count', 0), watchlist_count)
        released_ratio = safe_ratio(watchlist.get('released_count', 0), watchlist_count)
        if watchlist_count <= 0:
            diagnostics.append(('info', '观察名单', '暂无观察名单样本，当前无法判断观察名单阈值是否偏严或释放是否及时。'))
        elif watchlist_false_reject_ratio >= 0.3:
            diagnostics.append(('warning', '观察名单偏严', f"观察名单样本 {watchlist_count} 只中，误伤涨停 {watchlist.get('rejected_limit_up_count', 0)} 只，占比 {watchlist_false_reject_ratio:.0%}。建议优先复核观察名单换手阈值与释放条件。"))
        elif watchlist_broken_ratio >= 0.5 and released_ratio < 0.3:
            diagnostics.append(('warning', '观察名单释放偏慢', f"观察名单样本里，后续仍炸板 {watchlist.get('approved_broken_count', 0)} 只，占比 {watchlist_broken_ratio:.0%}；释放数 {watchlist.get('released_count', 0)} 较少。更像是名单持续压制但没有形成有效分流，建议复核自动释放逻辑。"))
        else:
            diagnostics.append(('success', '观察名单表现可控', f"观察名单样本 {watchlist_count} 只，误伤涨停 {watchlist.get('rejected_limit_up_count', 0)} 只，释放 {watchlist.get('released_count', 0)} 只。当前更适合继续观察，不建议仅凭单日样本大改阈值。"))

        blacklist_count = int(blacklist.get('candidate_count', 0) or 0)
        blacklist_false_reject_ratio = safe_ratio(blacklist.get('rejected_limit_up_count', 0), blacklist_count)
        if blacklist_count <= 0:
            diagnostics.append(('info', '黑名单', '暂无黑名单样本，当前无法判断黑名单规则是否过严。'))
        elif blacklist_false_reject_ratio >= 0.25:
            diagnostics.append(('warning', '黑名单可能误杀', f"黑名单样本 {blacklist_count} 只中，误伤涨停 {blacklist.get('rejected_limit_up_count', 0)} 只，占比 {blacklist_false_reject_ratio:.0%}。建议把部分硬拒绝改成先降级评分或缩仓观察。"))
        else:
            diagnostics.append(('success', '黑名单暂未显示过严', f"黑名单样本 {blacklist_count} 只，误伤涨停 {blacklist.get('rejected_limit_up_count', 0)} 只。当前更像是在拦截高风险样本，可继续积累多日统计后再调。"))

        break_count = int(break_episode.get('candidate_count', 0) or 0)
        reseal_ratio = safe_ratio(break_episode.get('with_reseal_count', 0), break_count)
        deep_break_ratio = safe_ratio(break_episode.get('deep_break_stock_count', 0), break_count)
        avg_duration = break_episode.get('avg_duration', 0) or 0
        max_duration = break_episode.get('max_duration', 0) or 0
        if break_count <= 0:
            diagnostics.append(('info', '炸板 episode', '暂无炸板 episode 样本，当前无法判断回封质量规则是否需要调整。'))
        elif reseal_ratio >= 0.5 and deep_break_ratio < 0.3:
            diagnostics.append(('warning', '炸板后回封占比较高', f"炸板样本 {break_count} 只中，快速回封 {break_episode.get('with_reseal_count', 0)} 只，占比 {reseal_ratio:.0%}，深炸仅 {break_episode.get('deep_break_stock_count', 0)} 只。建议优先考虑缩仓或观察名单，而不是一刀切拒绝。"))
        elif deep_break_ratio >= 0.4 or avg_duration >= 300:
            diagnostics.append(('warning', '炸板样本偏弱', f"炸板样本 {break_count} 只中，深炸 {break_episode.get('deep_break_stock_count', 0)} 只，占比 {deep_break_ratio:.0%}；平均时长 {avg_duration} 秒，最长 {max_duration} 秒。说明炸板后承接偏弱，当前严格控制仍有必要。"))
        else:
            diagnostics.append(('success', '炸板规则可继续观察', f"炸板样本 {break_count} 只，快速回封 {break_episode.get('with_reseal_count', 0)} 只，深炸 {break_episode.get('deep_break_stock_count', 0)} 只，平均时长 {avg_duration} 秒。短期更适合继续累计样本后再调回封参与规则。"))

        blocks = []
        for level, title, message in diagnostics:
            blocks.append(
                f"<div class='alert alert-{level}'><strong>{html_lib.escape(title)}</strong>：{html_lib.escape(message)}</div>"
            )
        return ''.join(blocks)

    def _generate_special_control_attribution(self, candidates: List[Dict[str, Any]]) -> str:
        """Render watchlist, blacklist and break-episode attribution."""
        special_candidates = []
        for candidate in candidates:
            event_counts = candidate.get('event_counts', {}) or {}
            features = candidate.get('features', {}) or {}
            if any([
                    event_counts.get('watchlist_enter', 0),
                    event_counts.get('watchlist_release', 0),
                    event_counts.get('blacklist_enter', 0),
                    event_counts.get('break_episode_start', 0),
                    event_counts.get('break_episode_end', 0),
                    features.get('break_count'),
                    features.get('episode_duration'),
                    features.get('fast_reseal_count'),
                    features.get('deep_break_count')
            ]):
                special_candidates.append(candidate)

        if not special_candidates:
            return "<p class='alert alert-info'>暂无观察名单、黑名单或炸板 episode 归因样本。</p>"

        watchlist_count = sum(1 for c in special_candidates if (c.get('event_counts', {}) or {}).get('watchlist_enter', 0) > 0)
        released_count = sum(1 for c in special_candidates if (c.get('event_counts', {}) or {}).get('watchlist_release', 0) > 0)
        blacklist_count = sum(1 for c in special_candidates if (c.get('event_counts', {}) or {}).get('blacklist_enter', 0) > 0)
        break_count = sum(1 for c in special_candidates if ((c.get('event_counts', {}) or {}).get('break_episode_start', 0) > 0 or (c.get('features', {}) or {}).get('break_count')))

        rows = []
        for candidate in special_candidates[:20]:
            stock_code = html_lib.escape(str(candidate.get('stock_code', '')))
            decision_type = html_lib.escape(str(candidate.get('decision_type', '未知')))
            outcome_type = html_lib.escape(str(candidate.get('outcome_type', '未知')))
            event_counts = candidate.get('event_counts', {}) or {}
            features = candidate.get('features', {}) or {}

            watch_ratio = features.get('watchlist_position_ratio')
            watch_turnover = features.get('watchlist_turnover_rate')
            break_duration = features.get('episode_duration') or features.get('max_break_duration')

            rows.append(f"""
                <tr>
                    <td><strong>{stock_code}</strong></td>
                    <td><span class="tag primary">{decision_type}</span></td>
                    <td><span class="tag info">{outcome_type}</span></td>
                    <td>{event_counts.get('watchlist_enter', 0)}/{event_counts.get('watchlist_release', 0)}</td>
                    <td>{watch_ratio if watch_ratio is not None else '-'}</td>
                    <td>{watch_turnover if watch_turnover is not None else '-'}</td>
                    <td>{event_counts.get('blacklist_enter', 0)}</td>
                    <td>{event_counts.get('break_episode_start', 0)}/{event_counts.get('break_episode_end', 0)}</td>
                    <td>{features.get('break_count', '-')}</td>
                    <td>{break_duration if break_duration is not None else '-'}</td>
                    <td>{features.get('fast_reseal_count', '-')}</td>
                    <td>{features.get('deep_break_count', '-')}</td>
                </tr>
            """)

        return f"""
        <div class="stats-summary">
            <div class="stat-item"><div class="stat-value">{len(special_candidates)}</div><div class="stat-label">特殊管控样本</div></div>
            <div class="stat-item"><div class="stat-value">{watchlist_count}</div><div class="stat-label">观察名单样本</div></div>
            <div class="stat-item"><div class="stat-value">{released_count}</div><div class="stat-label">已释放样本</div></div>
            <div class="stat-item"><div class="stat-value">{blacklist_count}</div><div class="stat-label">黑名单样本</div></div>
            <div class="stat-item"><div class="stat-value">{break_count}</div><div class="stat-label">炸板 episode 样本</div></div>
        </div>
        <div style="overflow-x:auto;">
            <table class="auto-layout">
                <thead>
                    <tr>
                        <th>股票</th>
                        <th>决策</th>
                        <th>结果</th>
                        <th>观察名单 进/出</th>
                        <th>缩仓比例</th>
                        <th>观察换手率</th>
                        <th>黑名单</th>
                        <th>炸板 开/闭</th>
                        <th>开板次数</th>
                        <th>持续时长</th>
                        <th>快速回封</th>
                        <th>深炸次数</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """

    def _generate_deterministic_review(self) -> str:
        """Generate deterministic event-attribution review section."""
        review_context = self.data.get('review_context', {}) or {}
        summary = review_context.get('summary', {}) or {}
        event_summary = summary.get('event_summary', {}) or self.data.get('event_summary', {}) or {}
        metrics = summary.get('metrics', {}) or self.data.get('metrics', {}) or {}
        dimension_stats = review_context.get('dimension_stats', {}) or {}
        candidates = review_context.get('candidates', []) or []
        review_counters = review_context.get('review_counters', {}) or {}

        funnel_cards = f"""
        <div class="stats-summary">
            <div class="stat-item"><div class="stat-value">{event_summary.get('candidate_seen', 0)}</div><div class="stat-label">候选事件</div></div>
            <div class="stat-item"><div class="stat-value">{event_summary.get('buy_decision', 0)}</div><div class="stat-label">买入决策</div></div>
            <div class="stat-item"><div class="stat-value">{event_summary.get('order_submitted', 0)}</div><div class="stat-label">发单事件</div></div>
            <div class="stat-item"><div class="stat-value">{event_summary.get('cancel_decision', 0)}</div><div class="stat-label">撤单决策</div></div>
            <div class="stat-item"><div class="stat-value">{event_summary.get('sell_decision', 0)}</div><div class="stat-label">卖出决策</div></div>
            <div class="stat-item"><div class="stat-value">{event_summary.get('watchlist_enter', 0)}/{event_summary.get('watchlist_release', 0)}</div><div class="stat-label">观察名单进/出</div></div>
            <div class="stat-item"><div class="stat-value">{event_summary.get('blacklist_enter', 0)}</div><div class="stat-label">黑名单事件</div></div>
            <div class="stat-item"><div class="stat-value">{event_summary.get('break_episode_start', 0)}/{event_summary.get('break_episode_end', 0)}</div><div class="stat-label">炸板 episode 开/闭</div></div>
        </div>
        """

        buy_type_cards = self._render_dimension_cards(
            dimension_stats.get('buy_type', {}), {'排板': '排板', '扫板': '扫板', '未知': '未知'}, 'info')
        decision_cards = self._render_dimension_cards(
            dimension_stats.get('decision_type', {}), {'approved': '买入', 'rejected': '拒绝', 'unknown': '未知'}, 'primary')
        outcome_cards = self._render_dimension_cards(
            dimension_stats.get('outcome_type', {}), {'limit_up': '涨停', 'broken_board': '炸板', 'normal': '普通', 'unknown': '未知'}, 'success')
        filter_cards = self._render_dimension_cards(dimension_stats.get('filter_tags', {}), {}, 'warning')

        review_counter_html = ""
        if review_counters:
            review_counter_html = self._render_dimension_cards(review_counters, {}, 'default')
        else:
            review_counter_html = "<p class='alert alert-info'>暂无复盘计数器。</p>"

        special_control_summary = review_context.get('special_control_summary', {}) or {}
        special_control_summary_html = self._generate_special_control_summary(special_control_summary)
        special_control_diagnostics_html = self._generate_special_control_diagnostics(special_control_summary)
        candidate_table = self._render_candidate_table(candidates)
        special_control_html = self._generate_special_control_attribution(candidates)
        deterministic_missed = self._render_ranked_list(
            self._build_stock_code_review_items(review_context.get('top_missed_opportunities', []) or [], 'missed'),
            '结构化错失机会 Top',
            '暂无结构化错失机会。')
        deterministic_losses = self._render_ranked_list(
            self._build_stock_code_review_items(review_context.get('avoidable_losses', []) or [], 'loss'),
            '结构化可避免损失 Top',
            '暂无结构化可避免损失。')

        return f"""
        <div class="alert alert-info">
            结构化事件总数 <strong>{event_summary.get('event_count', 0)}</strong>，候选股 <strong>{len(candidates)}</strong>，
            策略买入 <strong>{metrics.get('strategy_bought', 0)}</strong>，策略拒绝 <strong>{metrics.get('strategy_rejected', 0)}</strong>。
        </div>
        <h4>事件漏斗</h4>
        {funnel_cards}
        <h4>决策维度分布</h4>
        <div class="stats-summary" style="display:block; padding:12px 15px;">
            <div><strong>买入类型</strong></div>
            {buy_type_cards}
            <div><strong>决策类型</strong></div>
            {decision_cards}
            <div><strong>结果类型</strong></div>
            {outcome_cards}
            <div><strong>过滤标签 Top</strong></div>
            {filter_cards}
        </div>
        <h4>特殊管控统计摘要</h4>
        {special_control_summary_html}
        <h4>特殊管控结论提示</h4>
        {special_control_diagnostics_html}
        <h4>候选股归因样本</h4>
        {candidate_table}
        <h4>观察名单 / 黑名单 / 炸板 episode 归因</h4>
        {special_control_html}
        <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 20px; align-items:start;">
            <div>{deterministic_missed}</div>
            <div>{deterministic_losses}</div>
        </div>
        <h4>复盘计数器</h4>
        {review_counter_html}
        """

    def _build_stock_code_review_items(self, stock_codes: List[str], item_type: str) -> List[Dict[str, Any]]:
        """Build review items from stock code lists in review_context."""
        decisions = self.data.get('strategy_decisions', {}) or {}
        outcomes = self.data.get('market_outcomes', {}) or {}
        review_context = self.data.get('review_context', {}) or {}
        candidates = {
            item.get('stock_code'): item
            for item in review_context.get('candidates', []) or []
            if item.get('stock_code')
        }

        items = []
        for idx, stock_code in enumerate(stock_codes, 1):
            decision = decisions.get(stock_code)
            outcome = outcomes.get(stock_code)
            candidate = candidates.get(stock_code, {})
            filter_tags = []
            if decision and getattr(decision, 'filter_tags', None):
                filter_tags = list(decision.filter_tags)
            elif candidate.get('filter_tags'):
                filter_tags = list(candidate.get('filter_tags', []))

            if item_type == 'missed':
                recommendation = '复核拒绝条件，确认是否存在过滤过严或边界误伤。'
                reason = getattr(decision, 'decision_reason', '') if decision else candidate.get('decision_reason', '')
                score_key = 'impact_score'
            else:
                recommendation = '复核买入、撤单与卖出链路，确认是否可提前规避炸板。'
                reason = getattr(decision, 'buy_reason', '') if decision else candidate.get('decision_reason', '')
                score_key = 'loss_score'

            if not reason and outcome is not None:
                reason = getattr(outcome, 'outcome_type', '')

            items.append({
                'stock_code': stock_code,
                score_key: len(stock_codes) - idx + 1,
                'decision_reason': reason,
                'filter_tags': filter_tags,
                'recommendation': recommendation,
            })

        return items

    def _generate_key_findings(self) -> str:
        """Generate key findings section"""
        findings = []
        metrics = self.data.get('metrics', {})

        # Analyze performance
        success_rate = metrics.get('success_rate', 0)
        capture_rate = metrics.get('capture_rate', 0)
        avoidance_rate = metrics.get('avoidance_rate', 0)

        # Success rate analysis
        if success_rate > 0.8:
            findings.append(("success", "🎯 买入成功率极高，策略选股精准"))
        elif success_rate > 0.6:
            findings.append(("info", "✅ 买入成功率良好，策略表现稳定"))
        elif success_rate < 0.3:
            findings.append(("warning", "⚠️ 买入成功率偏低，需要优化选股条件"))

        # Capture rate analysis
        if capture_rate > 0.5:
            findings.append(
                ("success", f"📈 捕获了 {capture_rate:.0%} 的涨停机会，表现优秀"))
        elif capture_rate < 0.3:
            findings.append(("warning", "⚠️ 机会捕获率偏低，可能过滤条件过严"))

        # Avoidance rate analysis
        if avoidance_rate > 0.8:
            findings.append(("success", f"🛡️ 成功规避 {avoidance_rate:.0%} 的炸板风险"))
        elif avoidance_rate < 0.5:
            findings.append(("warning", "⚠️ 风险规避能力不足，需加强风控"))

        # Filter recommendations
        if self.analysis.get('top_recommendations'):
            rec_count = len(self.analysis['top_recommendations'])
            findings.append(("info", f"🔧 发现 {rec_count} 个可优化的过滤器"))

        # Missed opportunities
        missed_count = len(self.analysis.get('missed_opportunities', []))
        if missed_count > 10:
            findings.append(("warning", f"⚠️ 错失 {missed_count} 个涨停机会，需重点分析"))
        elif missed_count == 0:
            findings.append(("success", "✅ 无明显错失机会，过滤器设置合理"))

        # Generate HTML
        html = "<div class='key-findings'>"

        for alert_type, message in findings[:5]:  # Show top 5 findings
            html += f"""
            <div class="alert alert-{alert_type}">
                {message}
            </div>
            """

        html += "</div>"
        return html

    def _format_buy_reason(self, reason: str) -> str:
        """格式化买入原因为规范化显示"""
        if not reason:
            return ""
        
        # 解析买入原因各部分
        html_parts = []
        
        # 提取买入类型和股票信息
        header_match = re.match(r'\[([^\]]+)\]\s+(\d{6}\.\w{2})\s+(\S+)\s+满足买入条件', reason)
        if header_match:
            buy_type = header_match.group(1)
            stock_code = header_match.group(2)
            stock_name = header_match.group(3)
            buy_type_class = 'warning' if '扫板' in buy_type else 'info'
            html_parts.append(f'<div style="margin-bottom:8px;"><span class="tag {buy_type_class}">{buy_type}</span> <strong>{stock_code}</strong> {stock_name}</div>')
        
        # 解析各个条件
        conditions = []
        
        # 换手率
        turnover_match = re.search(r'\[换手率\]\s*满足买入条件[,，]\s*换手率\s*([\d.]+)\s*>=\s*([\d.]+)', reason)
        if turnover_match:
            conditions.append(('换手率', f'{float(turnover_match.group(1)):.2f}%', f'>= {turnover_match.group(2)}%', 'success'))
        
        # 量比
        volume_ratio_match = re.search(r'\[量比\]\s*满足买入条件[,，]\s*量比\s*([\d.]+)\s*>=\s*([\d.]+)', reason)
        if volume_ratio_match:
            conditions.append(('量比', volume_ratio_match.group(1), f'>= {volume_ratio_match.group(2)}', 'success'))
        
        # 板块效应
        sector_match = re.search(r'\[板块效应\]\s*满足买入条件[,，]\s*板块效应个数[：:]\s*(\d+)[,，]\s*领涨个数[：:]\s*(\d+)', reason)
        if sector_match:
            sector_count = sector_match.group(1)
            lead_count = sector_match.group(2)
            # 提取板块详情
            sector_detail_match = re.search(r'详情[：:]\s*\[(.*?)\](?:\s*\n|\s*$|\s*\[)', reason, re.DOTALL)
            sector_names = []
            if sector_detail_match:
                try:
                    import json
                    details_str = '[' + sector_detail_match.group(1) + ']'
                    details = json.loads(details_str)
                    sector_names = [d.get('板块名称', '') for d in details if d.get('板块名称')]
                except:
                    pass
            sector_info = f'{sector_count}个板块' + (f' ({", ".join(sector_names[:2])})' if sector_names else '')
            conditions.append(('板块效应', sector_info, f'领涨{lead_count}', 'success'))
        
        # 资金流入
        capital_match = re.search(r'\[资金流入\]\s*满足买入条件[,，]\s*主力净流入[：:]\s*([\d.]+)[,，]\s*主力净流入占比[：:]\s*([\d.]+)', reason)
        if capital_match:
            inflow = float(capital_match.group(1))
            inflow_pct = float(capital_match.group(2))
            inflow_display = f'{inflow/10000:.1f}万' if inflow < 100000 else f'{inflow:.0f}'
            conditions.append(('资金流入', f'主力 {inflow_display} ({inflow_pct:.1f}%)', '', 'success'))
        
        # 市场情绪
        sentiment_match = re.search(r'\[市场情绪\]\s*满足买入条件[,，]\s*市场情绪评分\s*([\d.]+)\s*>=\s*([\d.]+)', reason)
        if sentiment_match:
            score = sentiment_match.group(1)
            threshold = sentiment_match.group(2)
            extra_info = ''
            if '不考虑封单量' in reason:
                extra_info = ' (直接买入)'
            conditions.append(('市场情绪', f'{score}分{extra_info}', f'>= {threshold}', 'success'))
        
        # 封单信息
        seal_match = re.search(r'封单量[：:]\s*(\d+)[,，]\s*封单额[：:]\s*([\d.]+)', reason)
        if seal_match:
            seal_vol = int(seal_match.group(1))
            seal_amt = float(seal_match.group(2))
            seal_amt_display = f'{seal_amt/10000:.0f}万' if seal_amt >= 10000 else f'{seal_amt:.0f}'
            conditions.append(('封单', f'{seal_vol}手 / {seal_amt_display}', '', 'info'))
        
        # 生成条件表格
        if conditions:
            html_parts.append('<table style="width:100%; font-size:0.85em; border-collapse:collapse; margin-top:5px;">')
            for cond_name, cond_value, cond_threshold, cond_class in conditions:
                html_parts.append(f'''
                    <tr style="border-bottom:1px solid #eee;">
                        <td style="padding:3px 8px; color:#666; width:70px;">{cond_name}</td>
                        <td style="padding:3px 8px; font-weight:500;">{cond_value}</td>
                        <td style="padding:3px 8px; color:#999; font-size:0.9em;">{cond_threshold}</td>
                    </tr>
                ''')
            html_parts.append('</table>')
        
        return ''.join(html_parts) if html_parts else reason

    def _generate_stock_categorization(self) -> str:
        """Generate stock categorization section with strategy-held stocks highlighted"""
        html = """
        <table class="auto-layout">
            <thead>
                <tr>
                    <th style="width:120px;">分类</th>
                    <th style="width:80px;">股票数量</th>
                    <th style="width:60px;">占比</th>
                    <th style="min-width:400px;">示例股票</th>
                </tr>
            </thead>
            <tbody>
        """

        # Calculate categorization stats
        outcomes = self.data.get('market_outcomes', {})
        decisions = self.data.get('strategy_decisions', {})
        positions = self.data.get('positions',
                                  {})  # Get actual positions (bought stocks)

        # Define category order - strategy bought categories first
        categories = {
            '策略买入+涨停': [],
            '策略买入+炸板': [],
            '策略拒绝+涨停': [],
            '策略拒绝+炸板': []
        }

        for stock, decision in decisions.items():
            if stock in outcomes:
                outcome = outcomes[stock]
                if decision.decision_type == 'approved':
                    if outcome.outcome_type == 'limit_up':
                        categories['策略买入+涨停'].append(stock)
                    elif outcome.outcome_type == 'broken_board':
                        categories['策略买入+炸板'].append(stock)
                elif decision.decision_type == 'rejected':
                    if outcome.outcome_type == 'limit_up':
                        categories['策略拒绝+涨停'].append(stock)
                    elif outcome.outcome_type == 'broken_board':
                        categories['策略拒绝+炸板'].append(stock)

        total = sum(len(stocks) for stocks in categories.values())

        # Display categories in order, with strategy-bought categories first
        category_order = ['策略买入+涨停', '策略买入+炸板', '策略拒绝+涨停', '策略拒绝+炸板']

        for category in category_order:
            stocks = categories[category]
            percentage = (len(stocks) / total * 100) if total > 0 else 0

            # Sort stocks to put actual positions first
            stocks_sorted = []
            stocks_not_held = []
            for stock in stocks:
                if stock in positions:
                    stocks_sorted.append(stock)
                else:
                    stocks_not_held.append(stock)
            stocks = stocks_sorted + stocks_not_held

            # Helper to format stock with details
            def format_stock_display(stock_code):
                display = stock_code
                decision = decisions.get(stock_code)

                # Add position tag
                if stock_code in positions:
                    display += '<span class="strategy-held">持仓</span>'

                # Add buy type tag for approved stocks
                if decision and decision.decision_type == 'approved':
                    buy_type = decision.buy_type
                    if buy_type == '扫板':
                        display += '<span class="tag warning" style="font-size:0.7em; margin-left:4px">扫板</span>'
                    elif buy_type == '排板':
                        display += '<span class="tag info" style="font-size:0.7em; margin-left:4px">排板</span>'

                    # Add buy reason tooltip/expandable with formatted display
                    if decision.buy_reason:
                        reason_id = f"reason_{stock_code.replace('.', '_')}"
                        formatted_reason = self._format_buy_reason(decision.buy_reason)
                        display += f'''
                        <span class="info-icon" onclick="toggleReason('{reason_id}')" style="cursor:pointer; color:#3498db; margin-left:4px" title="点击查看买入原因">ℹ️</span>
                        <div id="{reason_id}" style="display:none; margin-top:8px; padding:12px; background:#f0f7ff; border-left:3px solid #3498db; border-radius:4px;">{formatted_reason}</div>
                        '''
                return display

            # Create collapsible stock list if more than 3 stocks
            if len(stocks) > 3:
                stock_list_id = category.replace('+', '_').replace(' ', '_')
                # Format first 3 stocks
                first_stocks = [format_stock_display(s) for s in stocks[:3]]

                # Format remaining stocks
                remaining_stocks = [
                    format_stock_display(s) for s in stocks[3:]
                ]

                examples = f"""
                    <div class="collapsible-stocks">
                        <div style="display: inline-block;">
                            <span>{'<br>'.join(first_stocks)}</span>
                            <button onclick="toggleStockList(event, '{stock_list_id}')" style="margin-left: 10px; padding: 2px 8px; border: 1px solid #3498db; background: white; color: #3498db; border-radius: 4px; cursor: pointer; font-size: 12px; vertical-align: middle;">
                                <span class="toggle-text">显示全部 {len(stocks)} 只 ▼</span>
                            </button>
                        </div>
                        <div id="{stock_list_id}" class="stock-list-full" style="display:none; margin-top: 8px; padding: 8px; background: #f8f9fa; border-radius: 4px; line-height: 1.8; word-break: break-word;">
                            <span style="color: #666; font-size: 0.9em;">其他股票：</span><br>{'<br>'.join(remaining_stocks)}
                        </div>
                    </div>
                """
            else:
                # Format all stocks
                formatted_stocks = [format_stock_display(s) for s in stocks]
                examples = '<br>'.join(
                    formatted_stocks) if formatted_stocks else '无'

            # Highlight rows for categories containing strategy-bought stocks
            row_class = 'highlight-row' if '买入' in category and stocks else ''
            tag_class = 'approved' if '买入' in category else 'rejected'

            html += f"""
                <tr class="{row_class}">
                    <td><span class="tag {tag_class}">{category}</span></td>
                    <td>{len(stocks)}</td>
                    <td>{percentage:.1f}%</td>
                    <td>{examples}</td>
                </tr>
            """

        html += """
            </tbody>
        </table>
        """

        return html

    def _generate_filter_analysis(self) -> str:
        """Generate filter analysis section - only showing precision with expandable stock details"""
        filter_metrics = self.analysis.get('filter_metrics', {})

        if not filter_metrics:
            return "<p class='alert alert-info'>今日暂无过滤器数据</p>"

        # Define filter categories
        filter_categories = {
            '盘前过滤':
            ['小市值', '均线下方', '成交低迷', '低价股', '非严格首板', '溢价差', '无涨停基因', '封板率低'],
            '买入条件': [
                '市场情绪', '首次涨停时间', '扫板时间', '流通股本异常', '换手率', '量比', '量比异常',
                '板块效应', '资金流入', '封单额', '封单量', '封单量+板块效应', '拉板资金', '价格下跌'
            ],
            '撤单原因': ['封单不足', '封单变化', '换手率超限', '板块效应不足', '资金流入不足'],
            '黑名单类': ['换手率过高', '开板次数过多', '开板时间过长', '开板后跌幅过大', '未知原因']
        }

        # Sort all filters by precision
        sorted_filters = sorted(filter_metrics.items(),
                                key=lambda x: x[1].precision,
                                reverse=True)

        html = """
        <div class="alert alert-info">
            <strong>说明：</strong> 显示所有过滤器效果分析，按类别分组，每组内按精确率降序排列
        </div>
        """

        # Generate table for each category
        for category_name, category_filters in filter_categories.items():
            # Get filters in this category
            category_data = [(name, metrics)
                             for name, metrics in sorted_filters
                             if name in category_filters]

            if not category_data:
                continue

            html += f"""
            <h4 style="margin-top: 20px; color: #2c3e50;">{category_name}</h4>
            <table>
                <thead>
                    <tr>
                        <th style="width: 60px;">排名</th>
                        <th style="min-width: 100px;">过滤器</th>
                        <th style="width: 80px;">分类</th>
                        <th style="width: 80px;">精确率</th>
                        <th style="min-width: 120px;">误伤涨停</th>
                        <th style="min-width: 120px;">正确过滤</th>
                        <th style="width: 80px;">评价</th>
                    </tr>
                </thead>
                <tbody>
            """

            for local_rank, (filter_name,
                             metrics) in enumerate(category_data, 1):
                # Get global rank
                global_rank = next(i
                                   for i, (n,
                                           _) in enumerate(sorted_filters, 1)
                                   if n == filter_name)

                # Determine evaluation and styling based on precision
                if metrics.precision >= 0.8:
                    evaluation = "优秀"
                    eval_class = "success"
                    row_class = "style='background-color: #f0fff4;'"
                elif metrics.precision >= 0.6:
                    evaluation = "良好"
                    eval_class = "info"
                    row_class = ""
                elif metrics.precision >= 0.4:
                    evaluation = "一般"
                    eval_class = "warning"
                    row_class = ""
                else:
                    evaluation = "需优化"
                    eval_class = "rejected"
                    row_class = "style='background-color: #fff5f5;'"

                # Add medal for global top 3
                rank_display = f"{global_rank}"
                if global_rank == 1:
                    rank_display = f"🥇 {global_rank}"
                elif global_rank == 2:
                    rank_display = f"🥈 {global_rank}"
                elif global_rank == 3:
                    rank_display = f"🥉 {global_rank}"

                # Category tag color with better visual distinction
                category_colors = {
                    '盘前过滤': 'info',  # 蓝色渐变 - 代表信息筛选
                    '买入条件': 'warning',  # 橙色渐变 - 代表决策条件
                    '撤单原因': 'primary',  # 紫色渐变 - 代表操作原因
                    '黑名单类': 'danger'  # 红色渐变 - 代表风险控制
                }
                category_class = category_colors.get(category_name, 'default')

                # Generate expandable stock details
                filter_id = f"filter_{filter_name.replace('+', '_').replace(' ', '_')}_{local_rank}"
                
                # Format stock lists for display - show all stocks
                false_positive_html = ""
                if metrics.false_positive > 0:
                    limit_up_stocks = getattr(metrics, 'filtered_limit_up_stocks', [])
                    if limit_up_stocks:
                        stocks_display = ', '.join(limit_up_stocks)  # 显示全部股票
                        false_positive_html = f'''
                            <span style="cursor:pointer; color:#e74c3c;" onclick="toggleFilterDetail('{filter_id}_fp')" title="点击查看详情">
                                {metrics.false_positive} <span style="font-size:0.8em;">🔍</span>
                            </span>
                            <div id="{filter_id}_fp" style="display:none; margin-top:5px; padding:8px; background:#fff5f5; border-left:3px solid #e74c3c; font-size:0.85em; max-height:200px; overflow-y:auto;">
                                <strong>误伤涨停股票 ({len(limit_up_stocks)}只)：</strong><br>{stocks_display}
                            </div>
                        '''
                    else:
                        false_positive_html = f'{metrics.false_positive}'
                else:
                    false_positive_html = f'{metrics.false_positive}'
                
                true_positive_html = ""
                if metrics.true_positive > 0:
                    broken_stocks = getattr(metrics, 'filtered_broken_stocks', [])
                    other_stocks = getattr(metrics, 'filtered_other_stocks', [])
                    all_correct = broken_stocks + other_stocks
                    if all_correct:
                        true_positive_html = f'''
                            <span style="cursor:pointer; color:#27ae60;" onclick="toggleFilterDetail('{filter_id}_tp')" title="点击查看详情">
                                {metrics.true_positive} <span style="font-size:0.8em;">🔍</span>
                            </span>
                            <div id="{filter_id}_tp" style="display:none; margin-top:5px; padding:8px; background:#f0fff4; border-left:3px solid #27ae60; font-size:0.85em; max-height:200px; overflow-y:auto;">
                                <strong>正确过滤股票 ({len(all_correct)}只)：</strong><br>
                                {f'炸板 ({len(broken_stocks)}只)：{", ".join(broken_stocks)}<br>' if broken_stocks else ''}
                                {f'其他 ({len(other_stocks)}只)：{", ".join(other_stocks)}' if other_stocks else ''}
                            </div>
                        '''
                    else:
                        true_positive_html = f'{metrics.true_positive}'
                else:
                    true_positive_html = f'{metrics.true_positive}'

                html += f"""
                    <tr {row_class}>
                        <td>{rank_display}</td>
                        <td><strong>{filter_name}</strong></td>
                        <td><span class="tag {category_class}" style="font-size: 0.8em;">{category_name}</span></td>
                        <td><strong>{metrics.precision:.2%}</strong></td>
                        <td>{false_positive_html}</td>
                        <td>{true_positive_html}</td>
                        <td><span class="tag {eval_class}">{evaluation}</span></td>
                    </tr>
                """

            html += """
                </tbody>
            </table>
            """

        # Add overall summary statistics
        avg_precision = sum(m.precision
                            for m in filter_metrics.values()) / len(
                                filter_metrics) if filter_metrics else 0
        total_false_positives = sum(m.false_positive
                                    for m in filter_metrics.values())
        total_true_positives = sum(m.true_positive
                                   for m in filter_metrics.values())

        html += f"""
        <div class="stats-summary" style="margin-top: 30px;">
            <h4>整体统计</h4>
            <div class="stat-item">
                <div class="stat-value">{len(filter_metrics)}</div>
                <div class="stat-label">过滤器总数</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{avg_precision:.1%}</div>
                <div class="stat-label">平均精确率</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{total_false_positives}</div>
                <div class="stat-label">总误伤数</div>
            </div>
            <div class="stat-item">
                <div class="stat-value">{total_true_positives}</div>
                <div class="stat-label">总正确过滤数</div>
            </div>
        </div>
        """

        return html

    def _generate_avoidable_losses(self) -> str:
        """Generate avoidable losses section from deterministic review context."""
        review_context = self.data.get('review_context', {}) or {}
        avoidable_stock_codes = review_context.get('avoidable_losses', []) or []
        items = self._build_stock_code_review_items(avoidable_stock_codes, 'loss')
        return self._render_ranked_list(items, '可避免损失样本', '暂无可避免损失样本。')

    def _generate_missed_opportunities(self) -> str:
        """Generate missed opportunities section with gene details in separate columns"""
        missed = self.analysis.get('missed_opportunities', [])

        if not missed:
            return "<p class='alert alert-success'>✅ 今日无明显错失机会，策略表现良好！</p>"

        # Group missed opportunities by filter tags
        filter_groups = {}
        for miss in missed:
            for tag in miss.get('filter_tags', []):
                if tag not in filter_groups:
                    filter_groups[tag] = []
                filter_groups[tag].append(miss)

        # Sort groups by count
        sorted_groups = sorted(filter_groups.items(),
                               key=lambda x: len(x[1]),
                               reverse=True)

        html = f"""
        <div class="alert alert-warning">
            ⚠️ 发现 <strong>{len(missed)}</strong> 个错失的涨停机会，需要重点关注以下过滤条件
        </div>
        """

        # Summary by filter
        if sorted_groups:
            html += """
            <div style="margin: 20px 0;">
                <h4>过滤原因分布</h4>
                <div class="stats-summary">
            """
            for filter_tag, stocks in sorted_groups[:5]:  # Top 5 filters
                percentage = len(stocks) / len(missed) * 100
                html += f"""
                    <div class="stat-item">
                        <div class="stat-value">{len(stocks)}</div>
                        <div class="stat-label">{filter_tag}<br>({percentage:.0f}%)</div>
                    </div>
                """
            html += """
                </div>
            </div>
            """

        # Get unique filter tags for dropdown
        all_filter_tags = set()
        for miss in missed:
            for tag in miss.get('filter_tags', []):
                all_filter_tags.add(tag)

        filter_options = '\n'.join([
            f'<option value="{tag}">{tag}</option>'
            for tag in sorted(all_filter_tags)
        ])

        # Detailed table - show all missed opportunities with gene details in separate columns
        html += f"""
        <h4>详细列表（共 {len(missed)} 只）</h4>
        <div style="margin-bottom: 10px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center;">
            <input type="text" id="missedSearch" onkeyup="filterMissedTable()" placeholder="搜索股票代码..." style="padding: 8px 12px; border-radius: 4px; border: 1px solid #ddd; min-width: 150px;">
            <select id="missedFilterTag" onchange="filterMissedTable()" style="padding: 8px 12px; border-radius: 4px; border: 1px solid #ddd;">
                <option value="">所有过滤标签</option>
                {filter_options}
            </select>
            <select id="missedFilterType" onchange="filterMissedTable()" style="padding: 8px 12px; border-radius: 4px; border: 1px solid #ddd;">
                <option value="">所有类型</option>
                <option value="首板股">首板股</option>
                <option value="强基因">强基因</option>
            </select>
            <button onclick="resetMissedFilters()" style="padding: 8px 12px; border-radius: 4px; border: 1px solid #3498db; background: white; color: #3498db; cursor: pointer;">重置筛选</button>
        </div>
        <div style="overflow-x: auto;">
        <table id="missedTable" class="sortable-table">
            <thead>
                <tr>
                    <th onclick="sortMissedTable(0)" style="cursor:pointer">序号 ⇅</th>
                    <th onclick="sortMissedTable(1)" style="cursor:pointer">股票代码 ⇅</th>
                    <th onclick="sortMissedTable(2)" style="cursor:pointer">拒绝原因 ⇅</th>
                    <th onclick="sortMissedTable(3)" style="cursor:pointer">过滤标签 ⇅</th>
                    <th onclick="sortMissedTable(4)" style="cursor:pointer">类型 ⇅</th>
                    <th onclick="sortMissedTable(5)" style="cursor:pointer">首板封板率 ⇅</th>
                    <th onclick="sortMissedTable(6)" style="cursor:pointer">次日红盘率 ⇅</th>
                    <th onclick="sortMissedTable(7)" style="cursor:pointer">高溢价比例 ⇅</th>
                    <th onclick="sortMissedTable(8)" style="cursor:pointer">次日开盘溢价 ⇅</th>
                    <th>优化建议</th>
                </tr>
            </thead>
            <tbody>
        """

        # Show all missed opportunities without pagination
        for idx, miss in enumerate(missed, 1):
            # Format filter tags with category colors
            filter_tags_html = ''
            for tag in miss.get('filter_tags', []):
                # Determine tag category for coloring
                tag_class = 'rejected'  # default
                if tag in [
                        '小市值', '均线下方', '成交低迷', '低价股', '非严格首板', '溢价差', '无涨停基因',
                        '封板率低'
                ]:
                    tag_class = 'info'
                elif tag in ['封单不足', '封单变化', '换手率超限', '板块效应不足', '资金流入不足']:
                    tag_class = 'warning'
                elif tag in ['换手率过高', '开板次数过多', '开板时间过长', '开板后跌幅过大', '未知原因']:
                    tag_class = 'danger'

                filter_tags_html += f'<span class="tag {tag_class}">{tag}</span> '

            # Pattern badge
            pattern_badge = ''
            if miss.get('pattern'):
                pattern_badge = f'<span class="tag warning">{miss.get("pattern", "-")}</span>'

            # Gene data - extract individual columns
            gene_data = miss.get('gene_data', {})
            seal_rate = gene_data.get('首板封板率', None)
            red_rate = gene_data.get('首板次日收盘红盘率', None)
            high_premium_rate = gene_data.get('涨停次日收盘溢价超5%比例', None)
            next_day_premium = gene_data.get('首板涨停或炸板次日开盘平均溢价', None)

            # Format with color coding
            def format_rate(rate, thresholds=(0.7, 0.5)):
                if pd.isna(rate):
                    return '-'
                if rate >= thresholds[0]:
                    return f'<span style="color:green; font-weight:bold">{rate:.0%}</span>'
                elif rate >= thresholds[1]:
                    return f'<span style="color:orange">{rate:.0%}</span>'
                else:
                    return f'<span style="color:gray">{rate:.0%}</span>'

            def format_premium(premium):
                if pd.isna(premium):
                    return '-'
                if premium >= 3:
                    return f'<span style="color:green; font-weight:bold">{premium:.1f}%</span>'
                elif premium >= 0:
                    return f'<span style="color:orange">{premium:.1f}%</span>'
                else:
                    return f'<span style="color:red">{premium:.1f}%</span>'

            html += f"""
                <tr data-stock="{miss['stock_code']}" data-tags="{','.join(miss.get('filter_tags', []))}" data-type="{miss.get('pattern', '')}">
                    <td>{idx}</td>
                    <td><strong>{miss['stock_code']}</strong></td>
                    <td>{miss['rejection_reason']}</td>
                    <td>{filter_tags_html if filter_tags_html else '-'}</td>
                    <td>{pattern_badge if pattern_badge else '-'}</td>
                    <td data-value="{seal_rate}">{format_rate(seal_rate, (0.8, 0.6))}</td>
                    <td data-value="{red_rate}">{format_rate(red_rate, (0.7, 0.5))}</td>
                    <td data-value="{high_premium_rate}">{format_rate(high_premium_rate, (0.5, 0.3))}</td>
                    <td data-value="{next_day_premium}">{format_premium(next_day_premium*100 if next_day_premium is not None else next_day_premium)}</td>
                    <td style="font-size: 0.85em;">{miss['recommendation']}</td>
                </tr>
            """

        html += """
            </tbody>
        </table>
        </div>
        """

        return html

    def _generate_stock_details(self) -> str:
        """Generate detailed stock analysis table with gene details in separate columns, labels beautified, and custom sorting"""
        outcomes = self.data.get('market_outcomes', {})
        decisions = self.data.get('strategy_decisions', {})
        positions = self.data.get('positions', {})

        all_stocks = set(outcomes.keys()) | set(decisions.keys())

        # Custom sorting: bought stocks first, then approved, then rejected but limit-up, then others
        def get_sort_priority(stock):
            decision = decisions.get(stock,
                                     StrategyDecision(stock, 'unknown', ''))
            outcome = outcomes.get(stock, StockOutcome(stock, 'normal'))

            # Priority 1: Bought stocks (in positions)
            if stock in positions:
                return (0, stock)
            # Priority 2: Approved by strategy (not bought but approved)
            elif decision.decision_type == 'approved':
                return (1, stock)
            # Priority 3: Rejected but achieved limit-up
            elif decision.decision_type == 'rejected' and outcome.outcome_type == 'limit_up':
                return (2, stock)
            # Priority 4: Rejected and broken board
            elif decision.decision_type == 'rejected' and outcome.outcome_type == 'broken_board':
                return (3, stock)
            # Priority 5: Others
            else:
                return (4, stock)

        sorted_stocks = sorted(all_stocks, key=get_sort_priority)

        html = """
        <div style="margin-bottom: 15px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center;">
            <input type="text" id="detailsSearch" onkeyup="filterDetailsTable()" placeholder="搜索股票代码..." style="padding: 8px 12px; border-radius: 4px; border: 1px solid #ddd; min-width: 150px;">
            <select id="detailsOutcome" onchange="filterDetailsTable()" style="padding: 8px 12px; border-radius: 4px; border: 1px solid #ddd;">
                <option value="">所有市场结果</option>
                <option value="limit_up">涨停</option>
                <option value="broken_board">炸板</option>
                <option value="normal">普通</option>
            </select>
            <select id="detailsDecision" onchange="filterDetailsTable()" style="padding: 8px 12px; border-radius: 4px; border: 1px solid #ddd;">
                <option value="">所有策略决策</option>
                <option value="approved">买入</option>
                <option value="rejected">拒绝</option>
            </select>
            <select id="detailsBuyType" onchange="filterDetailsTable()" style="padding: 8px 12px; border-radius: 4px; border: 1px solid #ddd;">
                <option value="">所有买入类型</option>
                <option value="扫板">扫板</option>
                <option value="排板">排板</option>
            </select>
            <button onclick="resetDetailsFilters()" style="padding: 8px 12px; border-radius: 4px; border: 1px solid #3498db; background: white; color: #3498db; cursor: pointer;">重置筛选</button>
        </div>
        <div style="overflow-x: auto;">
        <table id="detailsTable" class="sortable-table">
            <thead>
                <tr>
                    <th onclick="sortDetailsTable(0)" style="cursor:pointer">股票代码 ⇅</th>
                    <th onclick="sortDetailsTable(1)" style="cursor:pointer">市场结果 ⇅</th>
                    <th onclick="sortDetailsTable(2)" style="cursor:pointer">策略决策 ⇅</th>
                    <th onclick="sortDetailsTable(3)" style="cursor:pointer">买入类型 ⇅</th>
                    <th onclick="sortDetailsTable(4)" style="cursor:pointer">首板封板率 ⇅</th>
                    <th onclick="sortDetailsTable(5)" style="cursor:pointer">次日红盘率 ⇅</th>
                    <th onclick="sortDetailsTable(6)" style="cursor:pointer">高溢价比例 ⇅</th>
                    <th onclick="sortDetailsTable(7)" style="cursor:pointer">次日开盘溢价 ⇅</th>
                    <th>过滤原因</th>
                    <th>分析备注</th>
                </tr>
            </thead>
            <tbody>
        """

        for stock in sorted_stocks:
            outcome = outcomes.get(stock, StockOutcome(stock, 'normal'))
            decision = decisions.get(stock,
                                     StrategyDecision(stock, 'unknown', ''))

            # Beautified outcome labels with tag styling
            outcome_config = {
                'limit_up': ('涨停', 'success', 'limit_up'),
                'broken_board': ('炸板', 'danger', 'broken_board'),
                'normal': ('普通', 'default', 'normal')
            }
            outcome_text, outcome_class, outcome_value = outcome_config.get(
                outcome.outcome_type,
                (outcome.outcome_type, 'default', outcome.outcome_type))
            outcome_label = f'<span class="tag {outcome_class}">{outcome_text}</span>'

            # Beautified decision labels with tag styling
            decision_config = {
                'approved': ('买入', 'approved', 'approved'),
                'rejected': ('拒绝', 'rejected', 'rejected'),
                'unknown': ('未知', 'default', 'unknown')
            }
            decision_text, decision_class, decision_value = decision_config.get(
                decision.decision_type,
                (decision.decision_type, 'default', decision.decision_type))
            decision_label = f'<span class="tag {decision_class}">{decision_text}</span>'

            # Beautified buy type labels
            buy_type = decision.buy_type if decision.buy_type else '-'
            buy_type_value = decision.buy_type if decision.buy_type else ''
            if buy_type == '扫板':
                buy_type_label = '<span class="tag warning">扫板</span>'
            elif buy_type == '排板':
                buy_type_label = '<span class="tag info">排板</span>'
            else:
                buy_type_label = '<span class="tag default">-</span>'

            # Gene data - extract individual columns
            gene_data = decision.gene_data if decision.gene_data else {}
            seal_rate = gene_data.get('首板封板率', None)
            red_rate = gene_data.get('首板次日收盘红盘率', None)
            high_premium_rate = gene_data.get('涨停次日收盘溢价超5%比例', None)
            next_day_premium = gene_data.get('首板涨停或炸板次日开盘平均溢价', None)

            # Format with color coding
            def format_rate(rate, thresholds=(0.7, 0.5)):
                if pd.isna(rate):
                    return '-'
                if rate >= thresholds[0]:
                    return f'<span style="color:green; font-weight:bold">{rate:.0%}</span>'
                elif rate >= thresholds[1]:
                    return f'<span style="color:orange">{rate:.0%}</span>'
                else:
                    return f'<span style="color:gray">{rate:.0%}</span>'

            def format_premium(premium):
                if pd.isna(premium):
                    return '-'
                if premium >= 3:
                    return f'<span style="color:green; font-weight:bold">{premium:.1f}%</span>'
                elif premium >= 0:
                    return f'<span style="color:orange">{premium:.1f}%</span>'
                else:
                    return f'<span style="color:red">{premium:.1f}%</span>'

            filter_reasons = ', '.join(
                decision.filter_tags) if decision.filter_tags else '-'

            # Analysis note with better styling
            note = ""
            note_class = ""
            if outcome.outcome_type == 'limit_up' and decision.decision_type == 'rejected':
                note = "⚠️ 错失机会"
                note_class = "style='color: #e67e22; font-weight: bold;'"
            elif outcome.outcome_type == 'broken_board' and decision.decision_type == 'rejected':
                note = "✅ 成功规避"
                note_class = "style='color: #27ae60;'"
            elif outcome.outcome_type == 'limit_up' and decision.decision_type == 'approved':
                note = "✅ 成功捕获"
                note_class = "style='color: #27ae60; font-weight: bold;'"
            elif outcome.outcome_type == 'broken_board' and decision.decision_type == 'approved':
                note = "❌ 判断失误"
                note_class = "style='color: #e74c3c; font-weight: bold;'"

            # Add position indicator for bought stocks
            stock_display = f"<strong>{stock}</strong>"
            if stock in positions:
                stock_display += '<span class="strategy-held">持仓</span>'

            # Row highlighting for important rows
            row_class = ""
            if stock in positions:
                row_class = "class='highlight-row'"

            html += f"""
                <tr {row_class} data-stock="{stock}" data-outcome="{outcome_value}" data-decision="{decision_value}" data-buytype="{buy_type_value}">
                    <td>{stock_display}</td>
                    <td data-value="{outcome_value}">{outcome_label}</td>
                    <td data-value="{decision_value}">{decision_label}</td>
                    <td data-value="{buy_type_value}">{buy_type_label}</td>
                    <td data-value="{seal_rate}">{format_rate(seal_rate, (0.8, 0.6))}</td>
                    <td data-value="{red_rate}">{format_rate(red_rate, (0.7, 0.5))}</td>
                    <td data-value="{high_premium_rate}">{format_rate(high_premium_rate, (0.5, 0.3))}</td>
                    <td data-value="{next_day_premium}">{format_premium(next_day_premium*100 if next_day_premium is not None else next_day_premium)}</td>
                    <td>{filter_reasons}</td>
                    <td {note_class}>{note}</td>
                </tr>
            """

        html += """
            </tbody>
        </table>
        </div>
        """

        return html

    def _generate_chart_script(self) -> str:
        """Generate Chart.js script for filter performance visualization - precision only"""
        filter_metrics = self.analysis.get('filter_metrics', {})

        labels = list(filter_metrics.keys())
        precision_data = [m.precision for m in filter_metrics.values()]

        script = f"""
        var ctx = document.getElementById('filterChart').getContext('2d');
        var filterChart = new Chart(ctx, {{
            type: 'bar',
            data: {{
                labels: {labels},
                datasets: [{{
                    label: '精确率',
                    data: {precision_data},
                    backgroundColor: 'rgba(54, 162, 235, 0.6)',
                    borderColor: 'rgba(54, 162, 235, 1)',
                    borderWidth: 1
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{
                    legend: {{
                        display: true,
                        position: 'top'
                    }},
                    title: {{
                        display: true,
                        text: '过滤器精确率分析'
                    }}
                }},
                scales: {{
                    y: {{
                        beginAtZero: true,
                        max: 1,
                        title: {{
                            display: true,
                            text: '精确率'
                        }}
                    }},
                    x: {{
                        title: {{
                            display: true,
                            text: '过滤器'
                        }}
                    }}
                }}
            }}
        }});
        
        // Missed opportunities table filtering
        function filterMissedTable() {{
            var searchInput = document.getElementById('missedSearch').value.toUpperCase();
            var tagFilter = document.getElementById('missedFilterTag').value;
            var typeFilter = document.getElementById('missedFilterType').value;
            var table = document.getElementById('missedTable');
            var rows = table.getElementsByTagName('tbody')[0].getElementsByTagName('tr');
            
            for (var i = 0; i < rows.length; i++) {{
                var row = rows[i];
                var stock = row.getAttribute('data-stock') || '';
                var tags = row.getAttribute('data-tags') || '';
                var type = row.getAttribute('data-type') || '';
                
                var matchSearch = stock.toUpperCase().indexOf(searchInput) > -1;
                var matchTag = tagFilter === '' || tags.indexOf(tagFilter) > -1;
                var matchType = typeFilter === '' || type.indexOf(typeFilter) > -1;
                
                if (matchSearch && matchTag && matchType) {{
                    row.style.display = '';
                }} else {{
                    row.style.display = 'none';
                }}
            }}
        }}
        
        function resetMissedFilters() {{
            document.getElementById('missedSearch').value = '';
            document.getElementById('missedFilterTag').value = '';
            document.getElementById('missedFilterType').value = '';
            filterMissedTable();
        }}
        
        var missedSortDirection = {{}};
        function sortMissedTable(colIndex) {{
            var table = document.getElementById('missedTable');
            var tbody = table.getElementsByTagName('tbody')[0];
            var rows = Array.from(tbody.getElementsByTagName('tr'));
            
            missedSortDirection[colIndex] = !missedSortDirection[colIndex];
            var ascending = missedSortDirection[colIndex];
            
            rows.sort(function(a, b) {{
                var aCell = a.getElementsByTagName('td')[colIndex];
                var bCell = b.getElementsByTagName('td')[colIndex];
                
                var aValue = aCell.getAttribute('data-value') || aCell.textContent || aCell.innerText;
                var bValue = bCell.getAttribute('data-value') || bCell.textContent || bCell.innerText;
                
                // Try to parse as number
                var aNum = parseFloat(aValue);
                var bNum = parseFloat(bValue);
                
                if (!isNaN(aNum) && !isNaN(bNum)) {{
                    return ascending ? aNum - bNum : bNum - aNum;
                }}
                
                return ascending ? aValue.localeCompare(bValue) : bValue.localeCompare(aValue);
            }});
            
            rows.forEach(function(row) {{
                tbody.appendChild(row);
            }});
        }}
        
        // Stock details table filtering
        function filterDetailsTable() {{
            var searchInput = document.getElementById('detailsSearch').value.toUpperCase();
            var outcomeFilter = document.getElementById('detailsOutcome') ? document.getElementById('detailsOutcome').value : '';
            var decisionFilter = document.getElementById('detailsDecision') ? document.getElementById('detailsDecision').value : '';
            var buyTypeFilter = document.getElementById('detailsBuyType') ? document.getElementById('detailsBuyType').value : '';
            var table = document.getElementById('detailsTable');
            var rows = table.getElementsByTagName('tbody')[0].getElementsByTagName('tr');
            
            for (var i = 0; i < rows.length; i++) {{
                var row = rows[i];
                var stock = row.getAttribute('data-stock') || '';
                var outcome = row.getAttribute('data-outcome') || '';
                var decision = row.getAttribute('data-decision') || '';
                var buyType = row.getAttribute('data-buytype') || '';
                
                var matchSearch = stock.toUpperCase().indexOf(searchInput) > -1;
                var matchOutcome = outcomeFilter === '' || outcome === outcomeFilter;
                var matchDecision = decisionFilter === '' || decision === decisionFilter;
                var matchBuyType = buyTypeFilter === '' || buyType === buyTypeFilter;
                
                if (matchSearch && matchOutcome && matchDecision && matchBuyType) {{
                    row.style.display = '';
                }} else {{
                    row.style.display = 'none';
                }}
            }}
        }}
        
        function resetDetailsFilters() {{
            document.getElementById('detailsSearch').value = '';
            if (document.getElementById('detailsOutcome')) document.getElementById('detailsOutcome').value = '';
            if (document.getElementById('detailsDecision')) document.getElementById('detailsDecision').value = '';
            if (document.getElementById('detailsBuyType')) document.getElementById('detailsBuyType').value = '';
            filterDetailsTable();
        }}
        
        var detailsSortDirection = {{}};
        function sortDetailsTable(colIndex) {{
            var table = document.getElementById('detailsTable');
            var tbody = table.getElementsByTagName('tbody')[0];
            var rows = Array.from(tbody.getElementsByTagName('tr'));
            
            detailsSortDirection[colIndex] = !detailsSortDirection[colIndex];
            var ascending = detailsSortDirection[colIndex];
            
            rows.sort(function(a, b) {{
                var aCell = a.getElementsByTagName('td')[colIndex];
                var bCell = b.getElementsByTagName('td')[colIndex];
                
                var aValue = aCell.getAttribute('data-value') || aCell.textContent || aCell.innerText;
                var bValue = bCell.getAttribute('data-value') || bCell.textContent || bCell.innerText;
                
                // Try to parse as number
                var aNum = parseFloat(aValue);
                var bNum = parseFloat(bValue);
                
                if (!isNaN(aNum) && !isNaN(bNum)) {{
                    return ascending ? aNum - bNum : bNum - aNum;
                }}
                
                return ascending ? aValue.localeCompare(bValue) : bValue.localeCompare(aValue);
            }});
            
            rows.forEach(function(row) {{
                tbody.appendChild(row);
            }});
        }}
        """

        return script

    def save_reports(self, trading_mode: str = None):
        """Save both HTML and JSON reports
        
        Args:
            trading_mode: 交易模式 ('shadow' 或 'live')，用于报告文件名后缀
        """
        # Ensure directory exists
        daily_dir = self.report_dir / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)

        # 根据交易模式添加文件名后缀
        mode_suffix = f"_{trading_mode}" if trading_mode else ""

        # Generate HTML report
        html_content = self.generate_html_report()
        html_file = daily_dir / f"review_{self.data['date']}{mode_suffix}.html"

        with open(html_file, 'w', encoding='utf-8') as f:
            f.write(html_content)

        logger.info(f"HTML report saved to {html_file}")

        # Generate JSON report
        json_data = {
            'date': self.data['date'],
            'metrics': self.data.get('metrics', {}),
            'event_summary': self.data.get('event_summary', {}),
            'review_context': self.data.get('review_context', {}),
            'filter_metrics': {
                k: asdict(v)
                for k, v in self.analysis.get('filter_metrics', {}).items()
            },
            'missed_opportunities': self.analysis.get('missed_opportunities',
                                                      [])
        }

        json_file = daily_dir / f"review_{self.data['date']}{mode_suffix}.json"

        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

        logger.info(f"JSON report saved to {json_file}")


# ============================================================================
# Main Review Analyzer
# ============================================================================


class EnhancedPostMarketReviewAnalyzer:
    """Main enhanced post-market review analyzer"""
    def __init__(self,
                 date: Optional[str] = None,
                 strategy_version: str = None,
                 trading_mode: str = None):
        if date is None:
            date = datetime.now().strftime('%Y%m%d')
        self.date = date
        self.strategy_version = strategy_version or ReviewConfig.STRATEGY_VERSION
        self.trading_mode = trading_mode or ReviewConfig.TRADING_MODE
        self.collector = EnhancedDataCollector(date, strategy_version, trading_mode)
        self.data = {}
        self.analysis_results = {}

    def run_complete_analysis(self):
        """Run complete post-market analysis"""
        logger.info(f"Starting enhanced post-market review for {self.date}")

        try:
            # Step 1: Collect comprehensive data
            logger.info("Step 1: Collecting comprehensive data")
            self.data = self.collector.collect_comprehensive_data()

            # Step 2: Analyze filter performance
            logger.info("Step 2: Analyzing filter performance")
            filter_analyzer = FilterPerformanceAnalyzer(self.data)
            filter_metrics = filter_analyzer.analyze_all_filters()
            self.analysis_results['filter_metrics'] = filter_metrics

            # Step 3: Analyze missed opportunities
            logger.info("Step 3: Analyzing missed opportunities")
            miss_analyzer = MissedOpportunityAnalyzer(self.data)
            missed_opps = miss_analyzer.analyze_missed_opportunities()
            self.analysis_results['missed_opportunities'] = missed_opps

            #TODO： 加入被通过的炸板股票，打印出炸板股票的各个指标，进行分析。

            # Step 4: Get pattern summary
            logger.info("Step 4: Summarizing patterns")
            pattern_summary = miss_analyzer.get_pattern_summary()
            self.analysis_results['pattern_summary'] = pattern_summary

            # Step 5: Generate and save reports
            logger.info("Step 5: Generating reports")
            report_gen = EnhancedReportGenerator(self.data,
                                                 self.analysis_results)
            report_gen.save_reports(self.trading_mode)

            logger.info("Enhanced post-market review completed successfully")

            # Print summary
            self._print_summary()

        except Exception as e:
            logger.exception(f"Error during analysis: {e}")
            raise e

    def _print_summary(self):
        """Print analysis summary to console"""
        logger.info("\n" + "=" * 80)
        logger.info(f"📊 打板策略复盘分析完成 - {self.date}")
        logger.info("=" * 80)

        metrics = self.data.get('metrics', {})
        logger.info(f"\n📈 核心指标:")
        logger.info(f"  • 策略认可股票: {metrics.get('strategy_approved', 0)} 只")
        logger.info(f"  • 策略拒绝股票: {metrics.get('strategy_rejected', 0)} 只")
        logger.info(f"  • 机会捕获率: {metrics.get('capture_rate', 0):.1%}")
        logger.info(f"  • 风险规避率: {metrics.get('avoidance_rate', 0):.1%}")

        if self.analysis_results.get('top_recommendations'):
            logger.info(f"\n🔧 优化建议 (前3项):")
            for i, rec in enumerate(
                    self.analysis_results['top_recommendations'][:3], 1):
                logger.info(
                    f"  {i}. {rec.filter_name}: {rec.recommendation_type} - {rec.expected_impact}"
                )

        if self.analysis_results.get('missed_opportunities'):
            logger.info(
                f"\n⚠️ 错失机会: {len(self.analysis_results['missed_opportunities'])} 只股票"
            )

        logger.info(f"\n📁 报告已保存至: {ReviewConfig.REPORT_DIR / 'daily'}")
        logger.info("=" * 80 + "\n")


# ============================================================================
# Command Line Interface
# ============================================================================


def main():
    """Main entry point for command line execution"""
    import argparse

    parser = argparse.ArgumentParser(
        description=
        'Enhanced Post-Market Review Analyzer for A-Share Limit-Up Trading Strategy'
    )
    parser.add_argument(
        '--date',
        type=str,
        help='Analysis date (YYYYMMDD format). Defaults to today.',
        default=None)
    parser.add_argument(
        '--strategy-version',
        type=str,
        help='Strategy version (e.g., v2.1, v2.2, v2.3). Defaults to v2.1.',
        default=None)
    parser.add_argument(
        '--trading-mode',
        type=str,
        choices=['shadow', 'live'],
        help='Trading mode: "shadow" for shadow signal mode, "live" for live trading mode. Defaults to shadow.',
        default=None)
    parser.add_argument(
        '--multi-day',
        action='store_true',
        help='Run multi-day analysis to aggregate statistics across multiple days')
    parser.add_argument(
        '--days',
        type=int,
        default=7,
        help='Number of days to analyze in multi-day mode (default: 7)')

    args = parser.parse_args()

    execution_status = "success"
    error_message = ""
    date_processed = ""

    try:
        if args.multi_day:
            # Multi-day analysis mode
            trading_mode = args.trading_mode or ReviewConfig.TRADING_MODE
            end_date = args.date or datetime.now().strftime('%Y%m%d')
            date_processed = f"近{args.days}天 (截止 {end_date})"
            
            logger.info(f"Starting multi-day analysis for {args.days} days ending at {end_date}")
            report_path = run_multi_day_analysis(
                days=args.days, 
                end_date=end_date, 
                trading_mode=trading_mode
            )
            
            if report_path:
                logger.info(f"Multi-day analysis completed successfully. Report: {report_path}")
            else:
                execution_status = "failed"
                error_message = "Failed to generate multi-day report"
        
        else:
            # Single day analysis
            date = args.date or datetime.now().strftime('%Y%m%d')
            date_processed = date
            strategy_version = args.strategy_version or ReviewConfig.STRATEGY_VERSION
            logger.info(
                f"Starting post-market review analysis for {date} (Strategy: {strategy_version})"
            )

            trading_mode = args.trading_mode or ReviewConfig.TRADING_MODE
            analyzer = EnhancedPostMarketReviewAnalyzer(date, strategy_version, trading_mode)
            analyzer.run_complete_analysis()

            logger.info(
                f"Post-market review analysis completed successfully for {date} (Mode: {trading_mode})"
            )

    except Exception as e:
        execution_status = "failed"
        error_message = str(e)
        error_traceback = traceback.format_exc()

        logger.error(f"Post-market review analysis failed: {error_message}")
        logger.error(f"Traceback:\n{error_traceback}")

        logger.error(f"\n{'='*80}")
        logger.error(f"❌ ERROR: Post-market review analysis failed")
        logger.error(f"{'='*80}")
        logger.error(f"Error: {error_message}")
        logger.error(f"\nSee log file for detailed traceback")
        logger.error(f"{'='*80}\n")

    finally:
        # Send email notification
        send_completion_email(execution_status, date_processed,
                              error_message)


def send_completion_email(status: str,
                          date_processed: str,
                          error_message: str = ""):
    """
    Send email notification about script completion status
    
    Args:
        status: "success" or "failed"
        date_processed: Date or date range processed
        error_message: Error message if status is "failed"
    """
    try:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if status == "success":
            subject = f"✅ 盘后复盘分析完成 - {date_processed}"
            content = f"""盘后复盘分析已成功完成

分析日期: {date_processed}
完成时间: {current_time}
状态: 正常完成

报告已生成，请查看 reports/review 目录获取详细分析结果。
"""
        else:
            subject = f"❌ 盘后复盘分析失败 - {date_processed}"
            content = f"""盘后复盘分析执行失败

分析日期: {date_processed}
失败时间: {current_time}
状态: 执行异常

错误信息:
{error_message}

请检查日志文件获取详细错误信息。
"""

        logger.info(f"Sending email notification: {subject}")
        result = send_email(subject, content, add_timestamp=False)

        if result == "Success":
            logger.info("Email notification sent successfully")
        else:
            logger.warning(f"Failed to send email notification: {result}")

    except Exception as e:
        logger.exception(f"Error sending email notification: {e}")


def run_daily_review(strategy_version: str = None):
    """Run daily review (called by scheduler)"""
    date = datetime.now().strftime('%Y%m%d')
    strategy_version = strategy_version or ReviewConfig.STRATEGY_VERSION
    logger.info(
        f"Running scheduled review for {date} (Strategy: {strategy_version})")

    execution_status = "success"
    error_message = ""

    try:
        analyzer = EnhancedPostMarketReviewAnalyzer(date, strategy_version)
        analyzer.run_complete_analysis()
        logger.info(f"Scheduled review completed successfully for {date}")

    except Exception as e:
        execution_status = "failed"
        error_message = str(e)
        error_traceback = traceback.format_exc()

        logger.exception(
            f"Scheduled review failed for {date}: {error_message}")

    finally:
        # Always send email notification for scheduled runs
        send_completion_email(execution_status, date, error_message)


def run_backtest(start_date: str, end_date: str, strategy_version: str = None):
    """Run backtest analysis for date range"""
    strategy_version = strategy_version or ReviewConfig.STRATEGY_VERSION
    logger.info(
        f"Running backtest from {start_date} to {end_date} (Strategy: {strategy_version})"
    )

    start = datetime.strptime(start_date, '%Y%m%d')
    end = datetime.strptime(end_date, '%Y%m%d')
    current = start

    results = []

    while current <= end:
        # Skip weekends
        if current.weekday() < 5:
            date_str = current.strftime('%Y%m%d')
            logger.info(f"Processing {date_str}")

            try:
                analyzer = EnhancedPostMarketReviewAnalyzer(
                    date_str, strategy_version)
                analyzer.run_complete_analysis()

                results.append({
                    'date':
                    date_str,
                    'metrics':
                    analyzer.data.get('metrics', {}),
                    'recommendations':
                    len(
                        analyzer.analysis_results.get('top_recommendations',
                                                      [])),
                    'missed_opportunities':
                    len(
                        analyzer.analysis_results.get('missed_opportunities',
                                                      []))
                })

            except Exception as e:
                logger.exception(f"Failed to process {date_str}: {e}")

        current += timedelta(days=1)

    # Generate backtest summary
    generate_backtest_summary(results)


def generate_backtest_summary(results: List[Dict]):
    """Generate summary report for backtest results"""
    if not results:
        logger.warning("No backtest results to summarize")
        return

    df = pd.DataFrame(results)

    summary = {
        'date_range':
        f"{results[0]['date']} - {results[-1]['date']}",
        'total_days':
        len(results),
        'avg_capture_rate':
        df['metrics'].apply(lambda x: x.get('capture_rate', 0)).mean(),
        'avg_avoidance_rate':
        df['metrics'].apply(lambda x: x.get('avoidance_rate', 0)).mean(),
        'total_recommendations':
        df['recommendations'].sum(),
        'total_missed_opportunities':
        df['missed_opportunities'].sum(),
        'daily_stats':
        results
    }

    # Save summary
    summary_file = ReviewConfig.REPORT_DIR / 'backtest_summary.json'
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    logger.info(f"Backtest summary saved to {summary_file}")

    # Log summary
    logger.info("\n" + "=" * 80)
    logger.info("📊 Backtest Summary")
    logger.info("=" * 80)
    logger.info(f"Date Range: {summary['date_range']}")
    logger.info(f"Total Days: {summary['total_days']}")
    logger.info(f"Average Capture Rate: {summary['avg_capture_rate']:.1%}")
    logger.info(f"Average Avoidance Rate: {summary['avg_avoidance_rate']:.1%}")
    logger.info(f"Total Recommendations: {summary['total_recommendations']}")
    logger.info(
        f"Total Missed Opportunities: {summary['total_missed_opportunities']}")
    logger.info("=" * 80)


# ============================================================================
# Multi-Day Analysis Components
# ============================================================================

@dataclass
class MultiDayFilterStats:
    """Multi-day filter statistics"""
    filter_name: str
    total_true_positive: int = 0
    total_false_positive: int = 0
    total_true_negative: int = 0
    total_false_negative: int = 0
    daily_precision: List[float] = field(default_factory=list)
    daily_recall: List[float] = field(default_factory=list)
    daily_dates: List[str] = field(default_factory=list)
    avg_precision: float = 0.0
    avg_recall: float = 0.0
    avg_f1_score: float = 0.0
    precision_trend: str = "stable"  # 'improving', 'declining', 'stable'
    effectiveness_rating: str = "未评估"
    # 累计被过滤的股票
    all_filtered_limit_up_stocks: List[str] = field(default_factory=list)
    all_filtered_broken_stocks: List[str] = field(default_factory=list)
    all_filtered_other_stocks: List[str] = field(default_factory=list)


class MultiDayDataAggregator:
    """Aggregate data from multiple days for analysis"""
    
    def __init__(self, days: int = 7, trading_mode: str = None):
        self.days = days
        self.trading_mode = trading_mode or ReviewConfig.TRADING_MODE
        self.report_dir = ReviewConfig.REPORT_DIR / "daily"
        self.daily_data = []
        
    def load_daily_reports(self, end_date: str = None) -> List[Dict]:
        """Load daily review reports for the specified number of days"""
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')
        
        # 根据交易模式选择文件后缀
        mode_suffix = f"_{self.trading_mode}" if self.trading_mode else ""
        
        # 获取所有可用的日报告文件
        pattern = f"review_*{mode_suffix}.json"
        all_files = sorted(self.report_dir.glob(pattern), reverse=True)
        
        if not all_files:
            # 尝试不带后缀的文件
            pattern = "review_*.json"
            all_files = sorted([f for f in self.report_dir.glob(pattern) 
                               if not any(m in f.name for m in ['_live', '_shadow'])], 
                              reverse=True)
        
        logger.info(f"找到 {len(all_files)} 个日报告文件")
        
        # 筛选指定日期范围内的文件
        loaded_data = []
        end_dt = datetime.strptime(end_date, '%Y%m%d')
        
        for file_path in all_files:
            # 从文件名提取日期
            file_name = file_path.stem
            date_match = re.search(r'review_(\d{8})', file_name)
            if not date_match:
                continue
                
            file_date = date_match.group(1)
            file_dt = datetime.strptime(file_date, '%Y%m%d')
            
            # 检查日期是否在范围内
            if file_dt > end_dt:
                continue
                
            # 加载数据
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    loaded_data.append(data)
                    logger.info(f"已加载: {file_path.name}")
                    
                    if len(loaded_data) >= self.days:
                        break
            except Exception as e:
                logger.warning(f"加载文件失败 {file_path}: {e}")
                continue
        
        self.daily_data = loaded_data
        logger.info(f"成功加载 {len(loaded_data)} 天的数据")
        return loaded_data
    
    def get_date_range(self) -> Tuple[str, str]:
        """Get the date range of loaded data"""
        if not self.daily_data:
            return ("", "")
        dates = [d.get('date', '') for d in self.daily_data]
        return (min(dates), max(dates))


class MultiDayFilterAnalyzer:
    """Analyze filter performance across multiple days"""
    
    def __init__(self, daily_data: List[Dict]):
        self.daily_data = daily_data
        self.multi_day_stats = {}
        
    def analyze_filters(self) -> Dict[str, MultiDayFilterStats]:
        """Aggregate and analyze filter metrics across all days"""
        logger.info(f"分析 {len(self.daily_data)} 天的过滤器数据")
        
        # 收集所有过滤器名称
        all_filter_names = set()
        for day_data in self.daily_data:
            filter_metrics = day_data.get('filter_metrics', {})
            all_filter_names.update(filter_metrics.keys())
        
        # 为每个过滤器聚合统计
        for filter_name in all_filter_names:
            stats = MultiDayFilterStats(filter_name=filter_name)
            
            for day_data in self.daily_data:
                date = day_data.get('date', '')
                filter_metrics = day_data.get('filter_metrics', {})
                
                if filter_name in filter_metrics:
                    fm = filter_metrics[filter_name]
                    
                    # 累计计数
                    stats.total_true_positive += fm.get('true_positive', 0)
                    stats.total_false_positive += fm.get('false_positive', 0)
                    stats.total_true_negative += fm.get('true_negative', 0)
                    stats.total_false_negative += fm.get('false_negative', 0)
                    
                    # 记录每日精确率
                    precision = fm.get('precision', 0)
                    recall = fm.get('recall', 0)
                    stats.daily_precision.append(precision)
                    stats.daily_recall.append(recall)
                    stats.daily_dates.append(date)
                    
                    # 收集被过滤的股票
                    stats.all_filtered_limit_up_stocks.extend(
                        fm.get('filtered_limit_up_stocks', []))
                    stats.all_filtered_broken_stocks.extend(
                        fm.get('filtered_broken_stocks', []))
                    stats.all_filtered_other_stocks.extend(
                        fm.get('filtered_other_stocks', []))
            
            # 计算平均值
            if stats.daily_precision:
                stats.avg_precision = sum(stats.daily_precision) / len(stats.daily_precision)
            if stats.daily_recall:
                stats.avg_recall = sum(stats.daily_recall) / len(stats.daily_recall)
            
            # 计算F1
            if stats.avg_precision + stats.avg_recall > 0:
                stats.avg_f1_score = 2 * (stats.avg_precision * stats.avg_recall) / (stats.avg_precision + stats.avg_recall)
            
            # 分析趋势
            stats.precision_trend = self._analyze_trend(stats.daily_precision)
            
            # 评估效果
            stats.effectiveness_rating = self._rate_effectiveness(stats)
            
            self.multi_day_stats[filter_name] = stats
        
        return self.multi_day_stats
    
    def _analyze_trend(self, values: List[float]) -> str:
        """Analyze the trend of a series of values"""
        if len(values) < 3:
            return "stable"
        
        # 简单线性趋势分析
        n = len(values)
        x_mean = (n - 1) / 2
        y_mean = sum(values) / n
        
        numerator = sum((i - x_mean) * (values[i] - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        
        if denominator == 0:
            return "stable"
        
        slope = numerator / denominator
        
        if slope > 0.02:
            return "improving"
        elif slope < -0.02:
            return "declining"
        else:
            return "stable"
    
    def _rate_effectiveness(self, stats: MultiDayFilterStats) -> str:
        """Rate the overall effectiveness of a filter"""
        if stats.avg_precision >= 0.8:
            return "优秀"
        elif stats.avg_precision >= 0.6:
            return "良好"
        elif stats.avg_precision >= 0.4:
            return "一般"
        else:
            return "需优化"
    
    def get_top_filters(self, n: int = 10) -> List[Tuple[str, MultiDayFilterStats]]:
        """Get top N filters by average precision"""
        sorted_filters = sorted(
            self.multi_day_stats.items(),
            key=lambda x: x[1].avg_precision,
            reverse=True
        )
        return sorted_filters[:n]
    
    def get_filters_needing_optimization(self) -> List[Tuple[str, MultiDayFilterStats]]:
        """Get filters that need optimization (low precision + high false positives)"""
        problematic = []
        for name, stats in self.multi_day_stats.items():
            # 低精确率且误伤较多
            if stats.avg_precision < 0.5 and stats.total_false_positive > 5:
                problematic.append((name, stats))
        
        return sorted(problematic, key=lambda x: x[1].total_false_positive, reverse=True)


class MultiDayReportGenerator:
    """Generate multi-day analysis HTML report"""
    
    def __init__(self, daily_data: List[Dict], filter_stats: Dict[str, MultiDayFilterStats]):
        self.daily_data = daily_data
        self.filter_stats = filter_stats
        self.report_dir = ReviewConfig.REPORT_DIR
        
    def generate_html_report(self) -> str:
        """Generate comprehensive multi-day HTML report"""
        # 计算日期范围
        dates = [d.get('date', '') for d in self.daily_data]
        start_date = min(dates) if dates else ''
        end_date = max(dates) if dates else ''
        num_days = len(self.daily_data)
        
        # 计算聚合指标
        agg_metrics = self._calculate_aggregated_metrics()
        
        html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>打板策略多日统计报告 - {start_date} 至 {end_date}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{
            font-family: 'Microsoft YaHei', Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
        }}
        .container {{
            max-width: 1600px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
        }}
        h1 {{
            color: #2c3e50;
            border-bottom: 3px solid #667eea;
            padding-bottom: 15px;
            margin-bottom: 30px;
        }}
        h2 {{
            color: #34495e;
            margin-top: 40px;
            padding-bottom: 10px;
            border-bottom: 2px solid #ecf0f1;
        }}
        .summary-cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 20px;
            margin: 25px 0;
        }}
        .card {{
            padding: 20px;
            border-radius: 12px;
            color: white;
            text-align: center;
            transition: transform 0.3s ease;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        .card:hover {{
            transform: translateY(-5px);
        }}
        .card.purple {{ background: linear-gradient(135deg, #667eea, #764ba2); }}
        .card.green {{ background: linear-gradient(135deg, #11998e, #38ef7d); }}
        .card.blue {{ background: linear-gradient(135deg, #2193b0, #6dd5ed); }}
        .card.orange {{ background: linear-gradient(135deg, #f2994a, #f2c94c); color: #333; }}
        .card.red {{ background: linear-gradient(135deg, #eb3349, #f45c43); }}
        .card.teal {{ background: linear-gradient(135deg, #11998e, #38ef7d); }}
        .metric-value {{
            font-size: 2.5em;
            font-weight: bold;
            margin: 10px 0;
        }}
        .metric-label {{
            font-size: 0.95em;
            opacity: 0.9;
        }}
        .chart-container {{
            position: relative;
            height: 400px;
            margin: 30px 0;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 10px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            font-size: 0.9em;
        }}
        th {{
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            padding: 12px 8px;
            text-align: left;
            position: sticky;
            top: 0;
        }}
        td {{
            padding: 10px 8px;
            border-bottom: 1px solid #eee;
        }}
        tr:hover {{ background: #f5f5f5; }}
        .tag {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 0.8em;
            font-weight: 500;
        }}
        .tag.excellent {{ background: #d4edda; color: #155724; }}
        .tag.good {{ background: #cce5ff; color: #004085; }}
        .tag.average {{ background: #fff3cd; color: #856404; }}
        .tag.poor {{ background: #f8d7da; color: #721c24; }}
        .trend-up {{ color: #28a745; }}
        .trend-down {{ color: #dc3545; }}
        .trend-stable {{ color: #6c757d; }}
        .daily-trend {{
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            margin: 20px 0;
        }}
        .day-card {{
            flex: 1;
            min-width: 120px;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 8px;
            text-align: center;
        }}
        .day-card .date {{ font-size: 0.85em; color: #666; }}
        .day-card .value {{ font-size: 1.3em; font-weight: bold; color: #667eea; }}
        .filter-detail {{
            background: #f8f9fa;
            padding: 15px;
            margin: 10px 0;
            border-radius: 8px;
            border-left: 4px solid #667eea;
        }}
        .stock-list {{
            max-height: 150px;
            overflow-y: auto;
            font-size: 0.85em;
            color: #666;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 打板策略多日统计报告</h1>
        <p style="color: #666; font-size: 1.1em;">
            统计周期: <strong>{start_date}</strong> 至 <strong>{end_date}</strong> (共 <strong>{num_days}</strong> 个交易日)
        </p>
        
        <h2>一、整体表现概览</h2>
        <div class="summary-cards">
            <div class="card purple">
                <div class="metric-label">平均买入成功率</div>
                <div class="metric-value">{agg_metrics['avg_success_rate']:.1f}%</div>
            </div>
            <div class="card green">
                <div class="metric-label">平均机会捕获率</div>
                <div class="metric-value">{agg_metrics['avg_capture_rate']:.1f}%</div>
            </div>
            <div class="card blue">
                <div class="metric-label">平均风险规避率</div>
                <div class="metric-value">{agg_metrics['avg_avoidance_rate']:.1f}%</div>
            </div>
            <div class="card orange">
                <div class="metric-label">累计涨停股票</div>
                <div class="metric-value">{agg_metrics['total_limit_up']}</div>
            </div>
            <div class="card red">
                <div class="metric-label">累计炸板股票</div>
                <div class="metric-value">{agg_metrics['total_broken']}</div>
            </div>
            <div class="card teal">
                <div class="metric-label">平均炸板率</div>
                <div class="metric-value">{agg_metrics['avg_broken_rate']:.1f}%</div>
            </div>
        </div>
        
        {self._generate_daily_trend_section()}
        
        <h2>二、过滤器效果排名</h2>
        <p style="color: #666;">按平均精确率排序，展示各过滤器在多日内的综合表现</p>
        {self._generate_filter_ranking_table()}
        
        <h2>三、过滤器趋势分析</h2>
        <div class="chart-container">
            <canvas id="filterTrendChart"></canvas>
        </div>
        
        <h2>四、需优化过滤器</h2>
        {self._generate_optimization_section()}
        
        <h2>五、每日详细数据</h2>
        {self._generate_daily_details_table()}
    </div>
    
    <script>
    {self._generate_chart_scripts()}
    </script>
</body>
</html>
"""
        return html
    
    def _calculate_aggregated_metrics(self) -> Dict:
        """Calculate aggregated metrics across all days"""
        metrics = {
            'avg_success_rate': 0,
            'avg_capture_rate': 0,
            'avg_avoidance_rate': 0,
            'avg_broken_rate': 0,
            'total_limit_up': 0,
            'total_broken': 0,
            'total_bought': 0,
        }
        
        if not self.daily_data:
            return metrics
        
        success_rates = []
        capture_rates = []
        avoidance_rates = []
        broken_rates = []
        
        for day_data in self.daily_data:
            day_metrics = day_data.get('metrics', {})
            
            if day_metrics.get('success_rate') is not None:
                success_rates.append(day_metrics.get('success_rate', 0) * 100)
            if day_metrics.get('capture_rate') is not None:
                capture_rates.append(day_metrics.get('capture_rate', 0) * 100)
            if day_metrics.get('avoidance_rate') is not None:
                avoidance_rates.append(day_metrics.get('avoidance_rate', 0) * 100)
            if day_metrics.get('broken_board_rate') is not None:
                broken_rates.append(day_metrics.get('broken_board_rate', 0) * 100)
            
            metrics['total_limit_up'] += day_metrics.get('total_limit_up', 0)
            metrics['total_broken'] += day_metrics.get('total_broken_board', 0)
            metrics['total_bought'] += day_metrics.get('strategy_bought', 0)
        
        if success_rates:
            metrics['avg_success_rate'] = sum(success_rates) / len(success_rates)
        if capture_rates:
            metrics['avg_capture_rate'] = sum(capture_rates) / len(capture_rates)
        if avoidance_rates:
            metrics['avg_avoidance_rate'] = sum(avoidance_rates) / len(avoidance_rates)
        if broken_rates:
            metrics['avg_broken_rate'] = sum(broken_rates) / len(broken_rates)
        
        return metrics
    
    def _generate_daily_trend_section(self) -> str:
        """Generate daily trend visualization section with seal rate chart"""
        if not self.daily_data:
            return "<p>暂无数据</p>"
        
        # 按日期排序
        sorted_data = sorted(self.daily_data, key=lambda x: x.get('date', ''))
        
        # 过滤掉没有买入的日期
        sorted_data_with_buys = [d for d in sorted_data if d.get('metrics', {}).get('strategy_bought', 0) > 0]
        
        html = """
        <h3 style="margin-top: 30px;">每日成功率趋势</h3>
        <div class="daily-trend">
        """
        
        for day_data in sorted_data:
            date = day_data.get('date', '')
            metrics = day_data.get('metrics', {})
            success_rate = metrics.get('success_rate', 0) * 100
            bought = metrics.get('strategy_bought', 0)
            
            # 格式化日期显示
            if len(date) == 8:
                display_date = f"{date[4:6]}/{date[6:8]}"
            else:
                display_date = date
            
            # 如果没有买入，显示灰色
            if bought == 0:
                html += f"""
                <div class="day-card" style="opacity: 0.5;">
                    <div class="date">{display_date}</div>
                    <div class="value" style="color: #999;">无买入</div>
                </div>
                """
            else:
                html += f"""
                <div class="day-card">
                    <div class="date">{display_date}</div>
                    <div class="value">{success_rate:.0f}%</div>
                    <div style="font-size: 0.75em; color: #888;">买入{bought}只</div>
                </div>
                """
        
        html += "</div>"
        
        # 添加封板率趋势图（仅显示有买入的日期）
        if len(sorted_data_with_buys) >= 2:
            html += self._generate_seal_rate_chart(sorted_data_with_buys)
        
        return html
    
    def _generate_seal_rate_chart(self, sorted_data: List[Dict]) -> str:
        """Generate seal rate trend chart for days with buys"""
        # 准备图表数据
        dates = []
        total_seal_rates = []
        
        for day_data in sorted_data:
            date = day_data.get('date', '')
            metrics = day_data.get('metrics', {})
            
            # 格式化日期
            if len(date) == 8:
                display_date = f"{date[4:6]}/{date[6:8]}"
            else:
                display_date = date
            
            dates.append(display_date)
            
            # 总封板率（买入成功率）
            success_rate = metrics.get('success_rate', 0) * 100
            total_seal_rates.append(round(success_rate, 1))
        
        html = f"""
        <h3 style="margin-top: 30px;">封板率趋势 (仅显示有买入的日期)</h3>
        <div class="chart-container" style="height: 300px;">
            <canvas id="sealRateTrendChart"></canvas>
        </div>
        <script>
        (function() {{
            var ctx = document.getElementById('sealRateTrendChart').getContext('2d');
            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: {json.dumps(dates)},
                    datasets: [{{
                        label: '总封板率',
                        data: {json.dumps(total_seal_rates)},
                        borderColor: '#667eea',
                        backgroundColor: 'rgba(102, 126, 234, 0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 6,
                        pointHoverRadius: 8,
                        pointBackgroundColor: '#667eea',
                        borderWidth: 3
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        title: {{
                            display: true,
                            text: '买入封板率趋势 (%)',
                            font: {{ size: 14 }}
                        }},
                        legend: {{
                            position: 'bottom',
                            labels: {{
                                usePointStyle: true,
                                padding: 20
                            }}
                        }},
                        tooltip: {{
                            callbacks: {{
                                label: function(context) {{
                                    return context.dataset.label + ': ' + context.parsed.y.toFixed(1) + '%';
                                }}
                            }}
                        }}
                    }},
                    scales: {{
                        y: {{
                            beginAtZero: true,
                            max: 100,
                            title: {{
                                display: true,
                                text: '封板率 (%)'
                            }},
                            grid: {{
                                color: 'rgba(0,0,0,0.05)'
                            }}
                        }},
                        x: {{
                            grid: {{
                                display: false
                            }}
                        }}
                    }}
                }}
            }});
        }})();
        </script>
        """
        return html
    
    def _generate_filter_ranking_table(self) -> str:
        """Generate filter ranking table"""
        if not self.filter_stats:
            return "<p>暂无过滤器数据</p>"
        
        # 定义过滤器类别
        filter_categories = {
            '盘前过滤': ['小市值', '均线下方', '成交低迷', '低价股', '非严格首板', '溢价差', '无涨停基因', '封板率低'],
            '买入条件': ['市场情绪', '首次涨停时间', '扫板时间', '流通股本异常', '换手率', '量比', '量比异常',
                        '板块效应', '资金流入', '封单额', '封单量', '封单量+板块效应', '拉板资金', '价格下跌'],
            '撤单原因': ['封单不足', '封单变化', '换手率超限', '板块效应不足', '资金流入不足'],
            '黑名单类': ['换手率过高', '开板次数过多', '开板时间过长', '开板后跌幅过大', '未知原因']
        }
        
        # 反向映射：过滤器名 -> 类别
        filter_to_category = {}
        for cat, filters in filter_categories.items():
            for f in filters:
                filter_to_category[f] = cat
        
        # 类别样式
        category_styles = {
            '盘前过滤': 'background: linear-gradient(135deg, #e3f2fd, #bbdefb); color: #0277bd; border: 1px solid #81d4fa;',
            '买入条件': 'background: linear-gradient(135deg, #fff8e1, #ffecb3); color: #ef6c00; border: 1px solid #ffcc02;',
            '撤单原因': 'background: linear-gradient(135deg, #f3e5f5, #e1bee7); color: #7b1fa2; border: 1px solid #ba68c8;',
            '黑名单类': 'background: linear-gradient(135deg, #ffebee, #ffcdd2); color: #c62828; border: 1px solid #ef5350;'
        }
        
        # 按平均精确率排序
        sorted_filters = sorted(
            self.filter_stats.items(),
            key=lambda x: x[1].avg_precision,
            reverse=True
        )
        
        html = """
        <table>
            <thead>
                <tr>
                    <th>排名</th>
                    <th>过滤器名称</th>
                    <th>类别</th>
                    <th>平均精确率</th>
                    <th>平均召回率</th>
                    <th>F1分数</th>
                    <th>趋势</th>
                    <th>累计正确过滤</th>
                    <th>累计误伤</th>
                    <th>评价</th>
                </tr>
            </thead>
            <tbody>
        """
        
        for rank, (name, stats) in enumerate(sorted_filters, 1):
            # 获取过滤器类别
            category = filter_to_category.get(name, '其他')
            category_style = category_styles.get(category, 'background: #e9ecef; color: #495057;')
            # 趋势图标
            if stats.precision_trend == 'improving':
                trend_html = '<span class="trend-up">↑ 上升</span>'
            elif stats.precision_trend == 'declining':
                trend_html = '<span class="trend-down">↓ 下降</span>'
            else:
                trend_html = '<span class="trend-stable">→ 稳定</span>'
            
            # 评价标签
            rating_class = {
                '优秀': 'excellent',
                '良好': 'good',
                '一般': 'average',
                '需优化': 'poor'
            }.get(stats.effectiveness_rating, 'average')
            
            # 排名奖牌
            rank_display = str(rank)
            if rank == 1:
                rank_display = "🥇 1"
            elif rank == 2:
                rank_display = "🥈 2"
            elif rank == 3:
                rank_display = "🥉 3"
            
            html += f"""
                <tr>
                    <td>{rank_display}</td>
                    <td><strong>{name}</strong></td>
                    <td><span class="tag" style="{category_style} padding: 3px 8px; border-radius: 12px; font-size: 0.8em;">{category}</span></td>
                    <td>{stats.avg_precision:.1%}</td>
                    <td>{stats.avg_recall:.1%}</td>
                    <td>{stats.avg_f1_score:.2f}</td>
                    <td>{trend_html}</td>
                    <td>{stats.total_true_positive}</td>
                    <td>{stats.total_false_positive}</td>
                    <td><span class="tag {rating_class}">{stats.effectiveness_rating}</span></td>
                </tr>
            """
        
        html += """
            </tbody>
        </table>
        """
        return html
    
    def _generate_optimization_section(self) -> str:
        """Generate section for filters needing optimization"""
        # 找出需要优化的过滤器
        problematic = []
        for name, stats in self.filter_stats.items():
            if stats.avg_precision < 0.5 and stats.total_false_positive > 3:
                problematic.append((name, stats))
        
        if not problematic:
            return '<div class="filter-detail" style="border-left-color: #28a745;">✅ 所有过滤器表现良好，暂无需要优化的过滤器</div>'
        
        problematic.sort(key=lambda x: x[1].total_false_positive, reverse=True)
        
        html = ""
        for name, stats in problematic[:5]:
            # 去重股票列表
            unique_limit_up = list(set(stats.all_filtered_limit_up_stocks))
            
            html += f"""
            <div class="filter-detail" style="border-left-color: #dc3545;">
                <h4 style="margin-top: 0; color: #dc3545;">⚠️ {name}</h4>
                <p>
                    平均精确率: <strong>{stats.avg_precision:.1%}</strong> | 
                    累计误伤: <strong>{stats.total_false_positive}</strong> 只涨停股 |
                    趋势: {stats.precision_trend}
                </p>
                <div class="stock-list">
                    <strong>误伤涨停股票 (去重后{len(unique_limit_up)}只):</strong> 
                    {', '.join(unique_limit_up[:20])}{'...' if len(unique_limit_up) > 20 else ''}
                </div>
                <p style="margin-top: 10px; color: #666; font-size: 0.9em;">
                    💡 建议: 考虑放宽该过滤条件的阈值，或结合其他条件进行综合判断
                </p>
            </div>
            """
        
        return html
    
    def _generate_daily_details_table(self) -> str:
        """Generate daily details table with stock information"""
        if not self.daily_data:
            return "<p>暂无数据</p>"
        
        # 按日期排序
        sorted_data = sorted(self.daily_data, key=lambda x: x.get('date', ''), reverse=True)
        
        # 使用普通字符串来生成CSS，避免花括号问题
        css_styles = """
        <style>
            .daily-details-container {
                border-radius: 12px;
                overflow: hidden;
                box-shadow: 0 4px 20px rgba(0,0,0,0.08);
                margin: 20px 0;
                overflow-x: auto;
            }
            .daily-details-table {
                width: 100%;
                border-collapse: collapse;
                font-size: 0.95em;
                min-width: 800px;
            }
            .daily-details-table th {
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
                padding: 14px 12px;
                text-align: center;
                font-weight: 600;
                font-size: 0.9em;
            }
            .daily-details-table td {
                padding: 12px 10px;
                text-align: center;
                border-bottom: 1px solid #eef2f7;
            }
            .expandable-row {
                cursor: pointer;
                transition: all 0.2s ease;
            }
            .expandable-row:hover {
                background: linear-gradient(135deg, #f8f9ff, #eef1ff) !important;
            }
            .stock-detail-row {
                display: none;
            }
            .stock-detail-row td {
                padding: 0;
                background: linear-gradient(135deg, #f5f7fa, #e4e8ed);
            }
            .detail-content {
                padding: 20px;
                display: flex;
                flex-wrap: wrap;
                gap: 15px;
                justify-content: flex-start;
                max-width: 100%;
                overflow-x: auto;
            }
            .stock-group {
                flex: 1;
                min-width: 250px;
                max-width: 350px;
                padding: 16px;
                background: white;
                border-radius: 12px;
                box-shadow: 0 2px 12px rgba(0,0,0,0.06);
            }
            .stock-group.missed { border-left: 4px solid #f39c12; }
            .stock-group.correct { border-left: 4px solid #27ae60; }
            .stock-group.injured { border-left: 4px solid #e74c3c; }
            .stock-group-header {
                display: flex;
                align-items: center;
                margin-bottom: 12px;
            }
            .stock-group-icon {
                width: 28px;
                height: 28px;
                border-radius: 6px;
                display: flex;
                align-items: center;
                justify-content: center;
                margin-right: 8px;
                font-size: 1em;
            }
            .stock-group-icon.missed { background: #fef3e2; }
            .stock-group-icon.correct { background: #e8f8f0; }
            .stock-group-icon.injured { background: #fde8e8; }
            .stock-group-title {
                font-weight: 600;
                color: #2c3e50;
                font-size: 0.9em;
            }
            .stock-group-count {
                margin-left: auto;
                background: #eef2f7;
                padding: 2px 8px;
                border-radius: 10px;
                font-size: 0.75em;
                color: #5a6a7a;
            }
            .stock-list {
                display: flex;
                flex-wrap: wrap;
                gap: 4px;
                max-height: 150px;
                overflow-y: auto;
            }
            .stock-code {
                display: inline-block;
                padding: 3px 6px;
                border-radius: 4px;
                font-size: 0.75em;
                font-weight: 500;
            }
            .stock-code.success { background: #d4edda; color: #155724; }
            .stock-code.danger { background: #f8d7da; color: #721c24; }
            .stock-code.warning { background: #fff3cd; color: #856404; }
            .expand-icon {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 18px;
                height: 18px;
                background: #eef2f7;
                border-radius: 3px;
                margin-right: 6px;
                font-size: 0.65em;
                transition: transform 0.2s ease;
            }
            .expand-icon.expanded {
                transform: rotate(90deg);
                background: #667eea;
                color: white;
            }
            .date-cell {
                display: flex;
                align-items: center;
                justify-content: flex-start;
                font-weight: 500;
                color: #2c3e50;
            }
            .rate-badge {
                display: inline-block;
                padding: 3px 8px;
                border-radius: 15px;
                font-weight: 600;
                font-size: 0.8em;
            }
            .rate-badge.excellent { background: #d4edda; color: #155724; }
            .rate-badge.good { background: #fff3cd; color: #856404; }
            .rate-badge.poor { background: #f8d7da; color: #721c24; }
            .metric-cell {
                font-weight: 500;
                color: #495057;
            }
            .no-data-message {
                text-align: center;
                color: #8898aa;
                font-style: italic;
                padding: 20px;
            }
        </style>
        """
        
        html = css_styles + """
        <div class="daily-details-container">
        <table class="daily-details-table">
            <thead>
                <tr>
                    <th style="width: 140px; text-align: left; padding-left: 20px;">📅 日期</th>
                    <th style="width: 80px;">📈 涨停</th>
                    <th style="width: 80px;">💥 炸板</th>
                    <th style="width: 80px;">🛒 买入</th>
                    <th style="width: 100px;">✅ 成功率</th>
                    <th style="width: 90px;">🎯 捕获率</th>
                    <th style="width: 90px;">🛡️ 规避率</th>
                    <th style="width: 90px;">⚡ 炸板率</th>
                </tr>
            </thead>
            <tbody>
        """
        
        for idx, day_data in enumerate(sorted_data):
            date = day_data.get('date', '')
            metrics = day_data.get('metrics', {})
            missed_opportunities = day_data.get('missed_opportunities', [])
            
            # 格式化日期
            if len(date) == 8:
                display_date = f"{date[4:6]}月{date[6:8]}日"
                weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
                try:
                    from datetime import datetime
                    dt = datetime.strptime(date, '%Y%m%d')
                    weekday = weekday_names[dt.weekday()]
                    display_date = f"{date[4:6]}/{date[6:8]} {weekday}"
                except:
                    display_date = f"{date[4:6]}/{date[6:8]}"
            else:
                display_date = date
            
            row_id = f"detail_row_{idx}"
            
            # 提取股票列表 - 显示全部股票
            missed_stocks = [m.get('stock_code', '') for m in missed_opportunities]
            
            # 从 filter_metrics 中提取正确过滤的股票
            filter_metrics = day_data.get('filter_metrics', {})
            correct_filtered = []
            injured_stocks = []
            for fm_name, fm_data in filter_metrics.items():
                correct_filtered.extend(fm_data.get('filtered_broken_stocks', []))
                injured_stocks.extend(fm_data.get('filtered_limit_up_stocks', []))
            correct_filtered = list(set(correct_filtered))
            injured_stocks = list(set(injured_stocks))
            
            # 成功率样式
            success_rate = metrics.get('success_rate', 0) * 100
            if success_rate >= 80:
                rate_badge_class = "excellent"
            elif success_rate >= 50:
                rate_badge_class = "good"
            else:
                rate_badge_class = "poor"
            
            # 计算炸板率
            broken_rate = metrics.get('broken_board_rate', 0) * 100
            
            html += f"""
                <tr class="expandable-row" onclick="toggleDailyDetail('{row_id}', this)">
                    <td style="text-align: left; padding-left: 20px;">
                        <div class="date-cell">
                            <span class="expand-icon" id="icon_{row_id}">▶</span>
                            {display_date}
                        </div>
                    </td>
                    <td class="metric-cell">{metrics.get('total_limit_up', 0)}</td>
                    <td class="metric-cell">{metrics.get('total_broken_board', 0)}</td>
                    <td class="metric-cell">{metrics.get('strategy_bought', 0)}</td>
                    <td><span class="rate-badge {rate_badge_class}">{success_rate:.0f}%</span></td>
                    <td class="metric-cell">{metrics.get('capture_rate', 0)*100:.0f}%</td>
                    <td class="metric-cell">{metrics.get('avoidance_rate', 0)*100:.0f}%</td>
                    <td class="metric-cell">{broken_rate:.0f}%</td>
                </tr>
                <tr id="{row_id}" class="stock-detail-row">
                    <td colspan="8">
                        <div class="detail-content">
            """
            
            # 添加错失股票 - 显示全部
            if missed_stocks:
                html += f"""
                            <div class="stock-group missed">
                                <div class="stock-group-header">
                                    <div class="stock-group-icon missed">⚠️</div>
                                    <div class="stock-group-title">错失涨停</div>
                                    <div class="stock-group-count">{len(missed_stocks)}只</div>
                                </div>
                                <div class="stock-list" style="max-height: 200px; overflow-y: auto;">
                                    {''.join(f'<span class="stock-code warning">{s}</span>' for s in missed_stocks)}
                                </div>
                            </div>
                """
            
            # 添加正确过滤的股票 - 显示全部
            if correct_filtered:
                html += f"""
                            <div class="stock-group correct">
                                <div class="stock-group-header">
                                    <div class="stock-group-icon correct">✅</div>
                                    <div class="stock-group-title">正确过滤炸板</div>
                                    <div class="stock-group-count">{len(correct_filtered)}只</div>
                                </div>
                                <div class="stock-list" style="max-height: 200px; overflow-y: auto;">
                                    {''.join(f'<span class="stock-code success">{s}</span>' for s in correct_filtered)}
                                </div>
                            </div>
                """
            
            # 添加误伤的股票 - 显示全部
            if injured_stocks:
                html += f"""
                            <div class="stock-group injured">
                                <div class="stock-group-header">
                                    <div class="stock-group-icon injured">❌</div>
                                    <div class="stock-group-title">误伤涨停</div>
                                    <div class="stock-group-count">{len(injured_stocks)}只</div>
                                </div>
                                <div class="stock-list" style="max-height: 200px; overflow-y: auto;">
                                    {''.join(f'<span class="stock-code danger">{s}</span>' for s in injured_stocks)}
                                </div>
                            </div>
                """
            
            if not missed_stocks and not correct_filtered and not injured_stocks:
                html += """
                            <div class="no-data-message">📭 暂无详细股票数据</div>
                """
            
            html += """
                        </div>
                    </td>
                </tr>
            """
        
        html += """
            </tbody>
        </table>
        </div>
        <script>
        function toggleDailyDetail(rowId, triggerRow) {
            var row = document.getElementById(rowId);
            var expandIcon = document.getElementById('icon_' + rowId);
            if (row.style.display === 'table-row') {
                row.style.display = 'none';
                expandIcon.textContent = '▶';
                expandIcon.classList.remove('expanded');
            } else {
                row.style.display = 'table-row';
                expandIcon.textContent = '▼';
                expandIcon.classList.add('expanded');
            }
        }
        </script>
        """
        return html
    
    def _generate_chart_scripts(self) -> str:
        """Generate JavaScript for charts"""
        if not self.daily_data:
            return ""
        
        # 准备数据
        sorted_data = sorted(self.daily_data, key=lambda x: x.get('date', ''))
        dates = [d.get('date', '')[-4:] for d in sorted_data]  # MM/DD
        
        # 选择几个重要过滤器展示趋势
        important_filters = ['封板率低', '成交低迷', '开板后跌幅过大', '量比', '板块效应']
        
        datasets = []
        colors = ['#667eea', '#28a745', '#dc3545', '#ffc107', '#17a2b8']
        
        for i, filter_name in enumerate(important_filters):
            if filter_name in self.filter_stats:
                stats = self.filter_stats[filter_name]
                # 确保数据长度匹配
                precision_data = stats.daily_precision[-len(dates):] if len(stats.daily_precision) >= len(dates) else stats.daily_precision
                
                datasets.append({
                    'label': filter_name,
                    'data': [p * 100 for p in precision_data],
                    'borderColor': colors[i % len(colors)],
                    'fill': False,
                    'tension': 0.1
                })
        
        script = f"""
        var ctx = document.getElementById('filterTrendChart').getContext('2d');
        new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: {dates},
                datasets: {json.dumps(datasets)}
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    title: {{
                        display: true,
                        text: '主要过滤器精确率趋势 (%)'
                    }},
                    legend: {{
                        position: 'bottom'
                    }}
                }},
                scales: {{
                    y: {{
                        beginAtZero: true,
                        max: 100,
                        title: {{
                            display: true,
                            text: '精确率 (%)'
                        }}
                    }}
                }}
            }}
        }});
        """
        return script
    
    def save_report(self, filename: str = None):
        """Save the multi-day report"""
        if filename is None:
            dates = [d.get('date', '') for d in self.daily_data]
            start_date = min(dates) if dates else datetime.now().strftime('%Y%m%d')
            end_date = max(dates) if dates else datetime.now().strftime('%Y%m%d')
            filename = f"multi_day_review_{start_date}_{end_date}.html"
        
        report_path = self.report_dir / filename
        report_path.parent.mkdir(parents=True, exist_ok=True)
        
        html_content = self.generate_html_report()
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        logger.info(f"多日统计报告已保存: {report_path}")
        return report_path


def run_multi_day_analysis(days: int = 7, end_date: str = None, trading_mode: str = None):
    """Run multi-day analysis and generate report"""
    logger.info(f"开始多日统计分析 (近 {days} 天)")
    
    # 1. 聚合数据
    aggregator = MultiDayDataAggregator(days=days, trading_mode=trading_mode)
    daily_data = aggregator.load_daily_reports(end_date)
    
    if not daily_data:
        logger.error("未能加载任何日报告数据")
        return None
    
    # 2. 分析过滤器
    analyzer = MultiDayFilterAnalyzer(daily_data)
    filter_stats = analyzer.analyze_filters()
    
    # 3. 生成报告
    report_gen = MultiDayReportGenerator(daily_data, filter_stats)
    report_path = report_gen.save_report()
    
    # 4. 打印摘要
    logger.info("\n" + "=" * 80)
    logger.info("📊 多日统计分析完成")
    logger.info("=" * 80)
    
    date_range = aggregator.get_date_range()
    logger.info(f"统计周期: {date_range[0]} 至 {date_range[1]}")
    logger.info(f"统计天数: {len(daily_data)} 天")
    
    # 显示Top 5过滤器
    top_filters = analyzer.get_top_filters(5)
    logger.info("\n🏆 精确率Top 5过滤器:")
    for i, (name, stats) in enumerate(top_filters, 1):
        logger.info(f"  {i}. {name}: 平均精确率 {stats.avg_precision:.1%}")
    
    # 显示需优化的过滤器
    problem_filters = analyzer.get_filters_needing_optimization()
    if problem_filters:
        logger.info("\n⚠️ 需要优化的过滤器:")
        for name, stats in problem_filters[:3]:
            logger.info(f"  • {name}: 平均精确率 {stats.avg_precision:.1%}, 累计误伤 {stats.total_false_positive} 只")
    
    logger.info(f"\n📁 详细报告已保存至: {report_path}")
    logger.info("=" * 80 + "\n")
    
    return report_path


if __name__ == "__main__":
    main()
