"""
东方财富个股资金流向爬虫
基于sector_scraper重构，优化数据保存机制，移除数据库依赖
v1.9.1 - 增强反爬虫措施，包括动态ut参数、Cookie管理、请求指纹随机化等
"""

import requests
import json
import time
import logging
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
from enum import Enum
import random
from functools import wraps
import threading
import hashlib
import string
from http.cookiejar import CookieJar
import traceback

# 忽略pandas版本可能出现的FutureWarning
warnings.filterwarnings('ignore', category=FutureWarning)

# 获取日志记录器实例，该模块不应配置全局日志记录器，应由应用程序配置
logger = logging.getLogger(__name__)


class MarketType(Enum):
    """
    市场类型枚举
    """
    ALL = "all"  # 全市场
    MAIN_BOARD = "main"  # 主板
    GEM = "gem"  # 创业板
    STAR = "star"  # 科创板
    BSE = "bse"  # 北交所


class EnhancedAntiSpiderConfig:
    """
    增强的反爬虫配置
    """
    # 请求延迟配置 - 大幅增加以避免被ban
    MIN_DELAY = 3.0  # 最小延迟（秒）- 从1.0增加到3.0
    MAX_DELAY = 8.0  # 最大延迟（秒）- 从3.0增加到8.0
    
    # 重试配置
    MAX_RETRIES = 3  # 最大重试次数
    RETRY_DELAY_BASE = 5.0  # 重试基础延迟（秒）- 从2.0增加到5.0
    RETRY_DELAY_MULTIPLIER = 2.0  # 重试延迟倍数
    
    # 并发控制 - 完全禁用并发
    DEFAULT_MAX_WORKERS = 1  # 默认最大并发数 - 从3改为1
    RATE_LIMIT_WINDOW = 5.0  # 速率限制窗口（秒）- 从2.0增加到5.0
    MAX_REQUESTS_PER_WINDOW = 1  # 每个窗口最大请求数 - 从2改为1
    
    # User-Agent池
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]
    
    # Referer池
    REFERERS = [
        'https://data.eastmoney.com/',
        'https://quote.eastmoney.com/',
        'https://quote.eastmoney.com/center/gridlist.html',
        'https://data.eastmoney.com/zjlx/',
        'https://data.eastmoney.com/hsgtcg/',
    ]
    
    # 浏览器指纹配置
    BROWSER_CONFIGS = [
        {
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
        },
        {
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'sec-ch-ua': '"Chromium";v="119", "Google Chrome";v="119"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
        },
        {
            'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
            'Accept-Encoding': 'gzip, deflate',
            'sec-ch-ua': '"Microsoft Edge";v="120", "Chromium";v="120"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
        }
    ]


class UtTokenGenerator:
    """
    动态生成ut令牌
    """
    # 已知的ut令牌池
    UT_TOKENS = [
        '<redacted-token>',
        '<redacted-token>',
        '<redacted-token>',
        '<redacted-token>',
        '<redacted-token>',
    ]
    
    @staticmethod
    def generate_random_ut() -> str:
        """生成随机的32位十六进制ut令牌"""
        # 方法1：从已知池中随机选择
        if random.random() < 0.7:  # 70%概率使用已知的
            return random.choice(UtTokenGenerator.UT_TOKENS)
        
        # 方法2：生成类似格式的随机令牌
        parts = []
        patterns = ['b2884a39', '3a59ad64', '002292a3', 'e90d46a5']
        
        # 随机组合4个部分
        for _ in range(4):
            if random.random() < 0.5:
                # 使用已知模式
                parts.append(random.choice(patterns))
            else:
                # 生成随机8位hex
                parts.append(''.join(random.choices('0123456789abcdef', k=8)))
        
        return ''.join(parts)
    
    @staticmethod
    def generate_time_based_ut() -> str:
        """基于时间戳生成ut令牌"""
        timestamp = str(int(time.time() * 1000))
        # 添加随机盐
        salt = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        # 生成MD5哈希
        hash_input = f"{timestamp}{salt}eastmoney"
        return hashlib.md5(hash_input.encode()).hexdigest()


class SessionManager:
    """
    增强的会话管理器，维护cookie和会话状态
    支持会话老化、自动刷新和更好的隔离
    """
    def __init__(self, max_sessions: int = 5, session_lifetime: int = 300):
        self.sessions = []
        self.session_timestamps = []  # 记录每个会话的创建时间
        self.session_request_counts = []  # 记录每个会话的请求次数
        self.current_session_index = 0
        self.lock = threading.Lock()
        self.max_sessions = max_sessions
        self.session_lifetime = session_lifetime  # 会话生命周期(秒)
        self.max_requests_per_session = 50  # 每个会话最大请求数
        
        # 创建初始会话池
        for _ in range(max_sessions):
            self._create_new_session()
    
    def _create_new_session(self) -> requests.Session:
        """创建新会话并添加到池中"""
        session = requests.Session()
        
        # 禁用代理
        session.proxies = {
            'http': None,
            'https': None,
        }
        session.trust_env = False
        
        # 清空cookies确保全新开始
        session.cookies.clear()
        
        # 设置连接池参数
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=0  # 我们自己处理重试
        )
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        # 添加随机的初始cookie(模拟浏览器行为)
        if random.random() < 0.3:  # 30%概率添加
            session.cookies.set(
                'qgqp_b_id',
                hashlib.md5(str(time.time()).encode()).hexdigest()[:16],
                domain='.eastmoney.com'
            )
        
        self.sessions.append(session)
        self.session_timestamps.append(time.time())
        self.session_request_counts.append(0)
        
        return session
    
    def get_session(self) -> requests.Session:
        """获取可用会话，自动处理老化和刷新"""
        with self.lock:
            now = time.time()
            
            # 检查当前会话是否需要刷新
            if self.session_timestamps:
                session_age = now - self.session_timestamps[self.current_session_index]
                request_count = self.session_request_counts[self.current_session_index]
                
                # 会话过期或请求过多时刷新
                if (session_age > self.session_lifetime or
                    request_count >= self.max_requests_per_session):
                    logger.debug(
                        f"会话 {self.current_session_index} 需要刷新 "
                        f"(年龄: {session_age:.1f}s, 请求数: {request_count})"
                    )
                    # 关闭旧会话
                    self.sessions[self.current_session_index].close()
                    # 创建新会话替换
                    new_session = requests.Session()
                    new_session.proxies = {'http': None, 'https': None}
                    new_session.trust_env = False
                    new_session.cookies.clear()
                    
                    adapter = requests.adapters.HTTPAdapter(
                        pool_connections=10, pool_maxsize=20, max_retries=0
                    )
                    new_session.mount('http://', adapter)
                    new_session.mount('https://', adapter)
                    
                    if random.random() < 0.3:
                        new_session.cookies.set(
                            'qgqp_b_id',
                            hashlib.md5(str(time.time()).encode()).hexdigest()[:16],
                            domain='.eastmoney.com'
                        )
                    
                    self.sessions[self.current_session_index] = new_session
                    self.session_timestamps[self.current_session_index] = now
                    self.session_request_counts[self.current_session_index] = 0
            
            # 获取会话并更新计数
            session = self.sessions[self.current_session_index]
            self.session_request_counts[self.current_session_index] += 1
            
            # 轮换到下一个会话
            self.current_session_index = (self.current_session_index + 1) % len(self.sessions)
            
            return session
    
    def refresh_session(self, session: requests.Session):
        """刷新指定会话（清除cookies等）"""
        session.cookies.clear()
        
        # 可选：添加新的随机cookie
        if random.random() < 0.5:
            session.cookies.set(
                'em_hq_fls',
                f'js{random.randint(100000, 999999)}',
                domain='.eastmoney.com'
            )
    
    def refresh_all_sessions(self):
        """刷新所有会话"""
        with self.lock:
            logger.info("刷新所有会话...")
            for i, session in enumerate(self.sessions):
                session.close()
                
                new_session = requests.Session()
                new_session.proxies = {'http': None, 'https': None}
                new_session.trust_env = False
                new_session.cookies.clear()
                
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=10, pool_maxsize=20, max_retries=0
                )
                new_session.mount('http://', adapter)
                new_session.mount('https://', adapter)
                
                self.sessions[i] = new_session
                self.session_timestamps[i] = time.time()
                self.session_request_counts[i] = 0


class RateLimiter:
    """
    速率限制器
    """
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = []
        self.lock = threading.Lock()
    
    def wait_if_needed(self):
        """等待直到可以发送下一个请求"""
        with self.lock:
            now = time.time()
            # 清理过期的请求记录
            self.requests = [req_time for req_time in self.requests
                           if now - req_time < self.window_seconds]
            
            # 如果达到限制，等待
            if len(self.requests) >= self.max_requests:
                sleep_time = self.window_seconds - (now - self.requests[0]) + 0.1
                if sleep_time > 0:
                    logger.debug(f"速率限制：等待 {sleep_time:.2f} 秒")
                    time.sleep(sleep_time)
                    # 递归调用以重新检查
                    self.wait_if_needed()
            else:
                # 记录这次请求
                self.requests.append(now)


def retry_with_backoff(max_retries: int = EnhancedAntiSpiderConfig.MAX_RETRIES):
    """
    带指数退避的重试装饰器
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        delay = EnhancedAntiSpiderConfig.RETRY_DELAY_BASE * (
                            EnhancedAntiSpiderConfig.RETRY_DELAY_MULTIPLIER ** attempt
                        )
                        # 添加随机抖动
                        delay *= (0.5 + random.random())
                        logger.warning(
                            f"{func.__name__} 失败 (尝试 {attempt + 1}/{max_retries}): {e}. "
                            f"等待 {delay:.2f} 秒后重试..."
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"{func.__name__} 在 {max_retries} 次尝试后失败: {e}"
                        )
            raise last_exception
        return wrapper
    return decorator


class StockCapitalFlowConfig:
    """
    个股资金流向爬虫配置类
    存储API端点、请求头、字段映射等常量信息
    """

    # API接口地址
    CAPITAL_FLOW_URL = "https://push2.eastmoney.com/api/qt/clist/get"

    # 市场类型对应的筛选参数
    MARKET_FILTER_PARAMS = {
        MarketType.ALL: 'm:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2',
        MarketType.MAIN_BOARD: 'm:1+t:2,m:1+t:23',  # 主板
        MarketType.GEM: 'm:0+t:80',  # 创业板
        MarketType.STAR: 'm:1+t:23',  # 科创板
        MarketType.BSE: 'm:0+t:81'  # 北交所
    }

    # 资金流向数据字段原始键名到中文名称的映射
    FIELD_MAPPING = {
        'f12': '股票代码',
        'f14': '股票名称',
        'f2': '最新价',
        'f3': '涨跌幅',
        'f5': '成交量',
        'f6': '成交额',
        'f62': '主力净流入',
        'f184': '主力净流入占比',
        'f66': '超大单净流入',
        'f69': '超大单净流入占比',
        'f72': '大单净流入',
        'f75': '大单净流入占比',
        'f78': '中单净流入',
        'f81': '中单净流入占比',
        'f84': '小单净流入',
        'f87': '小单净流入占比',
        'f124': '更新时间戳',
    }


class StockCapitalFlowFetcher:
    """
    个股资金流向数据获取模块
    负责从东方财富API获取原始的个股资金流向数据
    包含增强的反爬措施
    """

    def __init__(self, config: StockCapitalFlowConfig, market_type: MarketType = MarketType.ALL,
                 use_anti_spider: bool = True):
        """
        初始化数据获取器

        Args:
            config (StockCapitalFlowConfig): 配置对象
            market_type (MarketType): 市场类型，默认为全市场
            use_anti_spider (bool): 是否使用反爬虫措施
        """
        self.config = config
        self.market_type = market_type
        self.use_anti_spider = use_anti_spider
        
        # 会话管理器
        self.session_manager = SessionManager()
        
        # 速率限制器
        self.rate_limiter = RateLimiter(
            EnhancedAntiSpiderConfig.MAX_REQUESTS_PER_WINDOW,
            EnhancedAntiSpiderConfig.RATE_LIMIT_WINDOW
        )
        
        # UT令牌生成器
        self.ut_generator = UtTokenGenerator()
        
        # 请求计数器
        self.request_count = 0
        self.request_lock = threading.Lock()
    
    def _get_random_headers(self) -> Dict[str, str]:
        """获取随机请求头"""
        # 基础头
        headers = {
            'User-Agent': random.choice(EnhancedAntiSpiderConfig.USER_AGENTS),
            'Referer': random.choice(EnhancedAntiSpiderConfig.REFERERS),
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Host': 'push2.eastmoney.com',
        }
        
        # 添加随机浏览器指纹
        browser_config = random.choice(EnhancedAntiSpiderConfig.BROWSER_CONFIGS)
        headers.update(browser_config)
        
        # 随机添加一些可选头
        if random.random() < 0.7:
            headers['DNT'] = '1'
        
        if random.random() < 0.5:
            headers['Upgrade-Insecure-Requests'] = '1'
        
        return headers
    
    def _random_delay(self):
        """添加随机延迟"""
        if self.use_anti_spider:
            # 增加一些变化
            base_delay = random.uniform(
                EnhancedAntiSpiderConfig.MIN_DELAY,
                EnhancedAntiSpiderConfig.MAX_DELAY
            )
            
            # 偶尔添加更长的延迟
            if random.random() < 0.1:  # 10%概率
                base_delay *= random.uniform(1.5, 2.5)
            
            time.sleep(base_delay)
    
    def _get_dynamic_ut(self) -> str:
        """获取动态ut令牌"""
        with self.request_lock:
            self.request_count += 1
            
            # 每隔一定请求数更换策略
            if self.request_count % 10 == 0:
                # 使用时间基础的令牌
                return self.ut_generator.generate_time_based_ut()
            else:
                # 使用随机令牌
                return self.ut_generator.generate_random_ut()

    @retry_with_backoff()
    def fetch_capital_flow_page(self,
                               page_num: int = 1,
                               page_size: int = 100,
                               sort_field: str = 'f3') -> Optional[Dict]:
        """
        获取单页的个股资金流向数据

        Args:
            page_num (int): 页码，从1开始，默认为1
            page_size (int): 每页返回的数据条数，默认为100
            sort_field (str): 排序字段，默认为f3（涨跌幅）

        Returns:
            Optional[Dict]: 包含API返回的JSON数据的字典，如果请求失败则为None
        """
        # 速率限制
        if self.use_anti_spider:
            self.rate_limiter.wait_if_needed()
        
        # 获取会话
        session = self.session_manager.get_session()
        
        try:
            # 动态生成参数
            params = {
                'cb': f'jQuery{random.randint(1000000000000000, 9999999999999999)}_{int(time.time() * 1000)}',
                'fid': sort_field,  # 排序字段
                'po': '1',  # 排序方式，1为降序
                'pz': str(page_size),
                'pn': str(page_num),
                'np': '1',
                'fltt': '2',
                'invt': '2',
                'ut': self._get_dynamic_ut(),  # 使用动态ut
                'fs': self.config.MARKET_FILTER_PARAMS[self.market_type],
                'fields': ','.join(self.config.FIELD_MAPPING.keys()),
                '_': str(int(time.time() * 1000))  # 时间戳防缓存
            }
            
            # 随机化一些参数顺序
            if random.random() < 0.3:
                params['dect'] = '1'  # 添加一些额外参数

            response = session.get(
                self.config.CAPITAL_FLOW_URL,
                params=params,
                headers=self._get_random_headers(),
                timeout=30  # 固定30秒超时
            )
            response.raise_for_status()
            
            # 随机延迟
            self._random_delay()

            # 处理JSONP响应格式
            content = response.text
            json_start_index = content.find('(')
            json_end_index = content.rfind(')')

            if json_start_index != -1 and json_end_index != -1 and json_start_index < json_end_index:
                json_str = content[json_start_index + 1:json_end_index]
                json_data = json.loads(json_str)
                return json_data
            else:
                logger.error(
                    f"解析{self.market_type.value}市场资金流向JSONP响应失败 (页 {page_num}): 无法找到有效的JSON数据。响应内容: {content[:200]}..."
                )
                return None

        except Exception as e:
            logger.error(f"获取{self.market_type.value}市场资金流向失败 (页 {page_num}): {e}, {traceback.print_exc()}")
            
            # 错误时可能需要刷新会话
            if "403" in str(e) or "forbidden" in str(e).lower():
                self.session_manager.refresh_session(session)
            
            raise

    def fetch_all_capital_flow(self, max_pages: int = 10) -> List[Dict]:
        """
        获取所有个股资金流向数据，自动处理分页并行获取

        Args:
            max_pages (int): 最大获取页数，默认为10

        Returns:
            List[Dict]: 包含所有个股原始资金流向数据的列表
        """
        all_raw_data = []

        # 首先获取第一页数据，以确定总记录数和总页数
        page_size_for_total_count = 100
        first_page_response = self.fetch_capital_flow_page(
            page_num=1, page_size=page_size_for_total_count)

        if not first_page_response or 'data' not in first_page_response or not first_page_response['data']:
            logger.warning(f"获取{self.market_type.value}市场资金流向第一页数据失败或数据为空，无法继续获取")
            return all_raw_data

        total_records = first_page_response['data'].get('total', 0)
        if total_records == 0:
            logger.info(f"{self.market_type.value}市场资金流向总数为0，无需进一步获取")
            if 'diff' in first_page_response['data'] and first_page_response['data']['diff']:
                all_raw_data.extend(first_page_response['data']['diff'])
            return all_raw_data

        # 计算实际需要获取的页数
        total_pages = min(max_pages, (total_records + page_size_for_total_count - 1) // page_size_for_total_count)

        logger.info(f"{self.market_type.value}市场资金流向总数: {total_records}, 每页大小: {page_size_for_total_count}, 将获取: {total_pages}页")

        # 添加第一页的数据到结果列表
        if 'diff' in first_page_response['data'] and first_page_response['data']['diff']:
            all_raw_data.extend(first_page_response['data']['diff'])

        # 串行获取剩余页面，减少并发以降低被封风险
        for page_num in range(2, total_pages + 1):
            try:
                # 偶尔添加更长的页面间延迟
                if page_num % 5 == 0:
                    extra_delay = random.uniform(2, 5)
                    logger.info(f"页面 {page_num}: 额外延迟 {extra_delay:.1f} 秒")
                    time.sleep(extra_delay)
                
                page_data = self.fetch_capital_flow_page(page_num, page_size_for_total_count)
                if page_data and 'data' in page_data and 'diff' in page_data['data'] and page_data['data']['diff']:
                    all_raw_data.extend(page_data['data']['diff'])
                    logger.info(f"获取第 {page_num}/{total_pages} 页完成")
                else:
                    logger.warning(f"获取{self.market_type.value}市场资金流向第 {page_num} 页数据失败或数据不完整")
            except Exception as e:
                logger.error(f"获取第 {page_num} 页失败: {e}")

        logger.info(f"成功获取 {len(all_raw_data)} 条原始{self.market_type.value}市场资金流向数据")
        return all_raw_data


class StockCapitalFlowParser:
    """
    个股资金流向数据解析模块
    负责将从API获取的原始JSON数据转换为结构化的Pandas DataFrame
    """

    def __init__(self, config: StockCapitalFlowConfig):
        """
        初始化数据解析器

        Args:
            config (StockCapitalFlowConfig): 配置对象
        """
        self.config = config

    def parse_capital_flow_data(self, raw_data_list: List[Dict]) -> pd.DataFrame:
        """
        解析个股资金流向原始数据列表

        Args:
            raw_data_list (List[Dict]): 从API获取的原始资金流向数据字典列表

        Returns:
            pd.DataFrame: 包含解析后资金流向数据的Pandas DataFrame，列名为中文
        """
        if not raw_data_list:
            logger.info("输入的原始资金流向数据列表为空，返回空DataFrame")
            return pd.DataFrame()

        parsed_data_list = []
        for raw_item in raw_data_list:
            parsed_item = {}
            for field_key, chinese_name in self.config.FIELD_MAPPING.items():
                raw_value = raw_item.get(field_key)

                # 对特定字段进行单位转换或格式化
                if raw_value is None or raw_value == '-':
                    parsed_value = None
                elif '占比' in chinese_name or '涨跌幅' in chinese_name:
                    # 百分比字段，保留两位小数
                    try:
                        parsed_value = round(float(raw_value), 2)
                    except (ValueError, TypeError):
                        parsed_value = None
                elif ('流入' in chinese_name and '占比' not in chinese_name) or chinese_name == '成交额' or chinese_name == '涨跌额':
                    # 金额字段，API单位通常是元，转换为万元，保留两位小数
                    try:
                        parsed_value = round(float(raw_value) / 10000, 2)
                    except (ValueError, TypeError):
                        parsed_value = None
                elif chinese_name == '成交量':
                    # 成交量单位是"手"
                    try:
                        parsed_value = int(raw_value)
                    except (ValueError, TypeError):
                        parsed_value = None
                elif chinese_name == '最新价':
                    # 股价保留两位小数
                    try:
                        parsed_value = round(float(raw_value), 2)
                    except (ValueError, TypeError):
                        parsed_value = None
                elif chinese_name == '更新时间戳':
                    # 时间戳转换为可读时间
                    try:
                        if raw_value:
                            parsed_value = datetime.fromtimestamp(int(raw_value)).strftime('%Y-%m-%d %H:%M:%S')
                        else:
                            parsed_value = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    except (ValueError, TypeError):
                        parsed_value = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                else:
                    # 其他字段直接使用
                    parsed_value = raw_value

                parsed_item[chinese_name] = parsed_value

            # 添加数据获取时间
            parsed_item['数据获取时间'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            parsed_data_list.append(parsed_item)

        df = pd.DataFrame(parsed_data_list)
        
        # 按主力净流入排序
        if '主力净流入' in df.columns:
            df = df.sort_values('主力净流入', ascending=False)
        
        logger.info(f"成功解析 {len(df)} 条个股资金流向数据")
        return df


class StockCapitalFlowScraper:
    """
    个股资金流向爬虫主类
    集成数据获取、数据解析和数据存储功能，提供一次性爬取、定时爬取等功能
    """

    def __init__(self, market_type: MarketType = MarketType.ALL, output_dir: str = None,
                 use_anti_spider: bool = True):
        """
        初始化个股资金流向爬虫

        Args:
            market_type (MarketType): 市场类型，默认为全市场
            output_dir (str): 数据输出目录，如果为None则自动设置
            use_anti_spider (bool): 是否使用反爬虫措施，默认为True
        """
        self.market_type = market_type
        self.config = StockCapitalFlowConfig()
        self.fetcher = StockCapitalFlowFetcher(self.config, market_type, use_anti_spider)
        self.parser = StockCapitalFlowParser(self.config)
        self.is_running = False

        # 设置输出目录
        if output_dir is None:
            self.output_dir = f"output/stock_capital_flow_data_{market_type.value}"
        else:
            self.output_dir = output_dir
        
        os.makedirs(self.output_dir, exist_ok=True)

    def scrape_all_data(self, max_pages: int = 10) -> pd.DataFrame:
        """
        执行完整的数据爬取流程

        Args:
            max_pages (int): 最大获取页数

        Returns:
            pd.DataFrame: 爬取并解析后的数据
        """
        try:
            logger.info(f"开始爬取{self.market_type.value}市场个股资金流向数据...")

            # 获取原始数据
            raw_data = self.fetcher.fetch_all_capital_flow(max_pages=max_pages)

            if not raw_data:
                logger.warning("未获取到任何原始数据")
                return pd.DataFrame()

            # 解析数据
            df = self.parser.parse_capital_flow_data(raw_data)

            if df.empty:
                logger.warning("解析后数据为空")
                return df

            logger.info(f"成功完成{self.market_type.value}市场个股资金流向数据爬取，共 {len(df)} 条记录")
            return df

        except Exception as e:
            logger.error(f"爬取{self.market_type.value}市场个股资金流向数据时发生错误: {e}")
            return pd.DataFrame()

    def save_data(self, df: pd.DataFrame, filename_prefix: str = "stock_capital_flow") -> str:
        """
        保存数据到CSV文件

        Args:
            df (pd.DataFrame): 要保存的数据
            filename_prefix (str): 文件名前缀

        Returns:
            str: 保存的文件路径
        """
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{filename_prefix}_{self.market_type.value}_{timestamp}.csv"
            filepath = os.path.join(self.output_dir, filename)

            # 保存为CSV文件，使用UTF-8编码和BOM以确保中文正确显示
            df.to_csv(filepath, index=False, encoding='utf-8-sig')
            
            logger.info(f"数据已保存到: {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"保存数据时发生错误: {e}")
            return ""

    def save_to_json(self, df: pd.DataFrame, filename_prefix: str = "stock_capital_flow") -> str:
        """
        保存数据到JSON文件

        Args:
            df (pd.DataFrame): 要保存的数据
            filename_prefix (str): 文件名前缀

        Returns:
            str: 保存的文件路径
        """
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{filename_prefix}_{self.market_type.value}_{timestamp}.json"
            filepath = os.path.join(self.output_dir, filename)

            # 转换为JSON格式
            data_dict = df.to_dict('records')
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data_dict, f, ensure_ascii=False, indent=2)
            
            logger.info(f"JSON数据已保存到: {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"保存JSON数据时发生错误: {e}")
            return ""

    def run_once(self, max_pages: int = 10, save_format: str = 'csv') -> Tuple[pd.DataFrame, str]:
        """
        执行一次完整的爬取和保存流程

        Args:
            max_pages (int): 最大获取页数
            save_format (str): 保存格式，'csv'或'json'或'both'

        Returns:
            Tuple[pd.DataFrame, str]: 数据和保存路径
        """
        df = self.scrape_all_data(max_pages=max_pages)
        
        if df.empty:
            return df, ""

        filepath = ""
        if save_format in ['csv', 'both']:
            filepath = self.save_data(df)
        
        if save_format in ['json', 'both']:
            json_path = self.save_to_json(df)
            if save_format == 'json':
                filepath = json_path

        return df, filepath

    def start_scheduled_scraping(self, interval_seconds: int = 60, max_pages: int = 10, save_format: str = 'csv'):
        """
        开始定时爬取

        Args:
            interval_seconds (int): 爬取间隔秒数
            max_pages (int): 每次最大获取页数
            save_format (str): 保存格式
        """
        self.is_running = True
        logger.info(f"开始定时爬取{self.market_type.value}市场个股资金流向数据，间隔: {interval_seconds}秒")

        while self.is_running:
            try:
                df, filepath = self.run_once(max_pages=max_pages, save_format=save_format)
                if not df.empty:
                    logger.info(f"定时爬取完成，数据条数: {len(df)}, 保存路径: {filepath}")
                else:
                    logger.warning("定时爬取未获取到数据")
                
                time.sleep(interval_seconds)
                
            except KeyboardInterrupt:
                logger.info("接收到中断信号，停止定时爬取")
                self.stop()
                break
            except Exception as e:
                logger.error(f"定时爬取过程中发生错误: {e}")
                time.sleep(interval_seconds)

    def stop(self):
        """
        停止爬虫
        """
        self.is_running = False
        logger.info("个股资金流向爬虫已停止")

    def get_top_inflow_stocks(self, df: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
        """
        获取主力净流入最多的股票

        Args:
            df (pd.DataFrame): 股票数据
            top_n (int): 返回前N只股票

        Returns:
            pd.DataFrame: 主力净流入最多的股票
        """
        if df.empty or '主力净流入' not in df.columns:
            return pd.DataFrame()
        
        return df.nlargest(top_n, '主力净流入')[
            ['股票代码', '股票名称', '最新价', '涨跌幅', '主力净流入', '主力净流入占比', '数据获取时间']
        ]

    def get_top_outflow_stocks(self, df: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
        """
        获取主力净流出最多的股票

        Args:
            df (pd.DataFrame): 股票数据
            top_n (int): 返回前N只股票

        Returns:
            pd.DataFrame: 主力净流出最多的股票
        """
        if df.empty or '主力净流入' not in df.columns:
            return pd.DataFrame()
        
        return df.nsmallest(top_n, '主力净流入')[
            ['股票代码', '股票名称', '最新价', '涨跌幅', '主力净流入', '主力净流入占比', '数据获取时间']
        ]

    def analyze_market_summary(self, df: pd.DataFrame) -> Dict:
        """
        分析市场资金流向概况

        Args:
            df (pd.DataFrame): 股票数据

        Returns:
            Dict: 市场概况统计
        """
        if df.empty:
            return {}

        summary = {
            '总股票数': len(df),
            '主力净流入股票数': len(df[df['主力净流入'] > 0]) if '主力净流入' in df.columns else 0,
            '主力净流出股票数': len(df[df['主力净流入'] < 0]) if '主力净流入' in df.columns else 0,
            '市场主力净流入总额(万元)': round(df['主力净流入'].sum(), 2) if '主力净流入' in df.columns else 0,
            '平均主力净流入(万元)': round(df['主力净流入'].mean(), 2) if '主力净流入' in df.columns else 0,
            '上涨股票数': len(df[df['涨跌幅'] > 0]) if '涨跌幅' in df.columns else 0,
            '下跌股票数': len(df[df['涨跌幅'] < 0]) if '涨跌幅' in df.columns else 0,
            '平涨股票数': len(df[df['涨跌幅'] == 0]) if '涨跌幅' in df.columns else 0,
            '数据获取时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        return summary