"""
config.py - 打板策略全局配置

所有策略参数、交易账户配置、阈值常量集中管理。
"""

import os
from datetime import datetime, time as dt_time

from infra.common_enums import PreMarketSellStrategy


def _env_bool(name, default=False):
    """Read a boolean environment variable with strict, visible semantics."""
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    raise ValueError(f'{name} 必须是 true/false，当前值无法识别')


def _env_int(name, default):
    value = os.getenv(name)
    return default if value in (None, '') else int(value)

# ---------------------------------------------------------------------------- #
#                                     全局变量                                  #
# ---------------------------------------------------------------------------- #
VERSION = 'v1.0'
# 安全默认值：公开仓库在未显式配置时只能运行模拟执行器。
EXECUTION_MODE = os.getenv('LIMIT_UP_EXECUTION_MODE', 'simulation').strip().lower()
if EXECUTION_MODE not in {'simulation', 'live'}:
    raise ValueError('LIMIT_UP_EXECUTION_MODE 只能是 simulation 或 live')
IS_LIVE_TRADING = EXECUTION_MODE == 'live'
DEBUG_MODE = not IS_LIVE_TRADING  # 兼容旧模块中的模式判断
ENABLE_SHADOW_SIGNAL = _env_bool('LIMIT_UP_ENABLE_SHADOW_SIGNAL', False)

# 板块数据源配置: 'THS' (同花顺) 或 'EM' (东方财富)，默认使用THS
SECTOR_DATA_SOURCE = 'THS'
AUTO_REFRESH_THS_SECTOR_MAPPING = False  # 设为 True 则策略启动时自动拉取问财板块映射（频繁运行可能被封）
IWENCAI_SECTOR_URL = os.getenv(
    'IWENCAI_SECTOR_URL',
    'https://www.iwencai.com/unifiedwap/result?'
    'w=%E8%82%A1%E7%A5%A8%E6%89%80%E5%B1%9E%E8%A1%8C%E4%B8%9A%E5%92%8C%E6%A6%82%E5%BF%B5'
    '&querytype=stock',
)
IWENCAI_DOWNLOAD_DIR = os.path.join('output', 'iwencai')
IWENCAI_BROWSER_USER_DATA_DIR = os.path.join('output', 'playwright', 'iwencai')
IWENCAI_BROWSER_CHANNEL = 'msedge'
IWENCAI_HEADLESS = False
IWENCAI_PAGE_SIZE = 100
IWENCAI_MAX_PAGES = None

STRATEGY_NAME = f'FirstLimitUp_{VERSION}_Debug' if DEBUG_MODE else f'FirstLimitUp_{VERSION}'  # 不要出现中文，以免FTP目录出现乱码
SHOULD_DOWNLOAD_KLINE = True

# 选择交易端配置。客户端路径和资金账号属于本机敏感配置，只从环境变量读取。
CLIENT_NAME = os.getenv('LIMIT_UP_CLIENT_NAME', 'GJ_SIM')
CLIENT_CONFIGS = {
    'CICC_LIVE': {
        'client_path': os.getenv('CICC_QMT_CLIENT_PATH', ''),
        'stock_account': os.getenv('CICC_STOCK_ACCOUNT', ''),
    },
    'GJ_SIM': {
        'client_path': os.getenv('GJ_SIM_QMT_CLIENT_PATH', ''),
        'stock_account': os.getenv('GJ_SIM_STOCK_ACCOUNT', ''),
    },
}

if CLIENT_NAME not in CLIENT_CONFIGS:
    available_clients = ', '.join(sorted(CLIENT_CONFIGS))
    raise ValueError(
        f'未知交易客户端 {CLIENT_NAME!r}；可选值：{available_clients}'
    )

_selected_client = CLIENT_CONFIGS[CLIENT_NAME]
CLIENT_PATH = _selected_client['client_path']
STOCK_ACCOUNT = _selected_client['stock_account']

# XTQuant 数据服务
IP = os.getenv('XTQUANT_HOST', '127.0.0.1')
PORT = _env_int('XTQUANT_PORT', 58610)

# ---------------------------------------------------------------------------- #

STOP_TIME = dt_time(15, 1) if not DEBUG_MODE else dt_time(23, 59)  # 停止时间
CLEAR_TIME = dt_time(14, 50) if not DEBUG_MODE else dt_time(23, 59)  # 清仓时间
BUY_ORDER_CANCEL_DEADLINE = dt_time(14, 55) if not DEBUG_MODE else dt_time(
    23, 59)  # 尾盘撤买时间
SELL_ORDER_CANCEL_DEADLINE = dt_time(14, 50) if not DEBUG_MODE else dt_time(
    23, 59)  # 尾盘撤卖时间
TODAY = datetime.now().strftime('%Y%m%d')
# TODAY = '20250722'

# 最大持仓数量
MAX_HOLDING_COUNT = 6
MAX_SAME_SECTOR_COUNT = 2  # v3.0: 同一概念板块最多持有2只，防止板块集中风险

LATENCY_THRESHOLD = 20  # 延迟阈值(s)
MONITOR_LOG_PATH = os.path.join(
    os.getenv('LIMIT_UP_LOG_DIR', os.path.join('logs', 'monitor')),
    VERSION,
    datetime.today().strftime('%Y-%m-%d-%H%M%S'),
)
MONITOR_INTERVAL = 1  # 监控间隔(s)
STOCK_TO_CONCEPT_MAPPING_FILE = r"output\concept_sector_data\THS\stock_to_concept_mapping.json" if SECTOR_DATA_SOURCE == 'THS' else r"output\concept_sector_data\stock_to_concept_mapping.json"  # 概念板块映射文件
STOCK_TO_INDUSTRY_MAPPING_FILE = r"output\industry_sector_data\THS\stock_to_industry_mapping.json" if SECTOR_DATA_SOURCE == 'THS' else r"output\industry_sector_data\stock_to_industry_mapping.json"  # 行业板块映射文件

# ---------------------------------------------------------------------------- #
MAX_UP_LIMIT_BREAK_COUNT = 5  # 最大涨停炸板次数
MAX_UP_LIMIT_BREAK_TIME = 1200  # 最大涨停炸板时间间隔(s)

MAX_TURNOVER_RATE_THRESHOLD = 15  # 最大换手率阈值，超过则加入观察名单
MAX_TURNOVER_RATE_BLACKLIST = 25  # 换手率直接拉黑阈值（U5升级）
WATCHLIST_POSITION_RATIO = 0.5  # 观察名单仓位缩减比例（U5升级）
WATCHLIST_RELEASE_MINUTES = 30  # 观察名单自动解除时间（分钟）
WATCHLIST_RELEASE_TURNOVER = 12  # 观察名单解除换手率阈值（%）
MIN_TURNOVER_RATE_THRESHOLD = 3  # 换手率最低阈值

MIN_VOLUME_RATIO_THRESHOLD = 0.7  # 最小成交量比率阈值

STOP_LOSS_RATE = 0.05  # 止损率，跌破止损价则卖出
MAX_CANCEL_COUNT = 25  # 最大撤单次数，超过则不再排板买入

# U6升级：波动率加权仓位管理
VOLATILITY_TARGET = 0.05  # 目标振幅（5%），振幅高于此则降低仓位，低于此则增加仓位
VOLATILITY_RATIO_MIN = 0.5  # 仓位调整最小倍数
VOLATILITY_RATIO_MAX = 1.5  # 仓位调整最大倍数

FIRST_LIMIT_TIME_CUTOFF = '14:30'  # 首次涨停时间截止点，超过则不扫首封板

# U7升级：LLM 盘前板块预判
ENABLE_PRE_MARKET_LLM_ANALYSIS = True  # 是否启用盘前LLM分析
LLM_SECTOR_PRIORITY_DISCOUNT = 0.3  # LLM优先板块最多降低封单门槛30%

MIN_LIMIT_ORDER_AMOUNT = 2e7  # 最小封单金额(元) - 低于此值不参与

# 封单金额阈值配置（基于市场情绪）
LIMIT_ORDER_AMOUNT_THRESHOLDS = {
    'STRONG_8': 3e7,      # 市场极强(>=8): 封单额>=3000万即可买入
    'STRONG_7': 5e7,      # 市场强势(>=7): 封单额>=5000万
    'NEUTRAL_55': 8e7,    # 中性偏强(>=5.5): 封单额>=8000万
    'NEUTRAL_4': 1e8,     # 中性(>=4): 封单额>=1亿
    'WEAK_25': 1.5e8,     # 弱势(>=2.5): 封单额>=1.5亿
}

# ---------------------------------------------------------------------------- #
#                              盘前卖出策略配置                                  #
# ---------------------------------------------------------------------------- #

PRE_MARKET_SELL_STRATEGY = PreMarketSellStrategy.FIXED_PREMIUM_SELL
FIXED_PREMIUM_RATE = 0.02  # 默认2%

# ---------------------------------------------------------------------------- #
#                              日内分档止盈配置 (v3.0)                           #
# ---------------------------------------------------------------------------- #
INTRADAY_TAKE_PROFIT_ENABLED = True
INTRADAY_TAKE_PROFIT_TIERS = [
    # (盈利比例, 卖出比例, 说明)
    (0.05, 0.25, '盈利5%卖出1/4'),
    (0.08, 0.25, '盈利8%再卖1/4'),
    (0.10, 0.25, '盈利10%再卖1/4'),
    # 剩余1/4跟随追踪止损
]

# ---------------------------------------------------------------------------- #
#                                 U8升级：交易日志                               #
# ---------------------------------------------------------------------------- #
TRADE_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output', 'trade_logs')
