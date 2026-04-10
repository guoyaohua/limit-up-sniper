"""
东方财富数据爬虫API接口模块

本模块提供简洁易用的API接口，供外部程序调用爬虫功能。
包含概念板块数据获取、个股资金流向分析、实时监控、数据筛选等核心功能。

主要功能包括:
- 概念板块行情与资金流向数据获取
- 个股资金流向排行数据获取
- 实时数据监控器
- 数据分析与筛选工具
- 股票到概念板块映射关系
"""
import pandas as pd
from typing import Optional, Dict, List, Callable, Union, Tuple
import threading
import time
from datetime import datetime, timedelta
import os
from enum import Enum
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
from functools import wraps
import hashlib
import string
from http.cookiejar import CookieJar
import traceback
from loguru import logger
from infra.utils import send_email


class SectorType(Enum):
    """
    板块类型枚举
    """
    CONCEPT = "concept"  # 概念板块
    INDUSTRY = "industry"  # 行业板块


class MarketType(Enum):
    """
    市场类型枚举
    """
    ALL = "all"  # 全市场
    MAIN_BOARD = "main"  # 主板
    GEM = "gem"  # 创业板
    STAR = "star"  # 科创板
    BSE = "bse"  # 北交所


class SectorConfig:
    """
    板块爬虫配置类
    """
    # API接口地址
    SECTOR_QUOTE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
    CAPITAL_FLOW_URL = "https://push2.eastmoney.com/api/qt/clist/get"

    # 板块类型对应的筛选参数
    SECTOR_FILTER_PARAMS = {
        SectorType.CONCEPT.name: 'm:90+t:3',  # 概念板块
        SectorType.INDUSTRY.name: 'm:90+t:2'  # 行业板块
    }

    # 板块行情数据字段原始键名到中文名称的映射
    QUOTE_FIELD_MAPPING = {
        'f12': '板块代码',
        'f14': '板块名称',
        'f2': '最新价',
        'f3': '涨跌幅',
        'f4': '涨跌额',
        'f5': '成交量',
        'f6': '成交额',
        'f22': '涨速',
        'f11': '5分钟涨跌',
        'f104': '上涨家数',
        'f105': '下跌家数',
        'f62': '主力净流入',
        'f184': '主力净流入占比',
        'f66': '超大单净流入',
        'f69': '超大单净流入占比',
        'f164': '5日主力净流入',
        'f165': '5日主力净流入占比',
        'f166': '5日超大单净流入',
        'f167': '5日超大单净流入占比',
        'f174': '10日主力净流入',
        'f175': '10日主力净流入占比',
        'f176': '10日超大单净流入',
        'f177': '10日超大单净流入占比',
        'f128': '领涨股票名称',
        'f140': '领涨股票代码',
        'f136': '领涨股票涨跌幅',
    }

    # 板块成分股数据字段原始键名到中文名称的映射
    CONSTITUENT_FIELD_MAPPING = {
        'f12': '股票代码',
        'f14': '股票名称',
    }


class EnhancedAntiSpiderConfig:
    """
    增强的反爬虫配置
    """
    # 请求延迟配置
    MIN_DELAY = 1.0  # 最小延迟（秒）- 增加
    MAX_DELAY = 3.0  # 最大延迟（秒）- 增加

    # 重试配置
    MAX_RETRIES = 3  # 最大重试次数
    RETRY_DELAY_BASE = 2.0  # 重试基础延迟（秒）- 增加
    RETRY_DELAY_MULTIPLIER = 2.0  # 重试延迟倍数

    # 并发控制
    DEFAULT_MAX_WORKERS = 1  # 默认最大并发数
    RATE_LIMIT_WINDOW = 2.0  # 速率限制窗口（秒）- 增加
    MAX_REQUESTS_PER_WINDOW = 2  # 每个窗口最大请求数 - 减少

    # User-Agent池 - 扩展
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
        'https://quote.eastmoney.com/',
        'https://quote.eastmoney.com/center/gridlist.html',
        'https://quote.eastmoney.com/concept/',
        'https://quote.eastmoney.com/center/',
        'https://data.eastmoney.com/',
    ]

    # 浏览器指纹配置
    BROWSER_CONFIGS = [{
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'sec-ch-ua':
        '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    }, {
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'sec-ch-ua': '"Chromium";v="119", "Google Chrome";v="119"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    }, {
        'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
        'Accept-Encoding': 'gzip, deflate',
        'sec-ch-ua': '"Microsoft Edge";v="120", "Chromium";v="120"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    }]


class UtTokenGenerator:
    """
    动态生成ut令牌
    """
    # 已知的ut令牌池（可以通过浏览器抓取更多）
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
        # 分析已知令牌的模式，生成相似的
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
        salt = ''.join(
            random.choices(string.ascii_letters + string.digits, k=8))
        # 生成MD5哈希
        hash_input = f"{timestamp}{salt}eastmoney"
        return hashlib.md5(hash_input.encode()).hexdigest()


class SessionManager:
    """
    会话管理器，维护cookie和会话状态
    """
    def __init__(self):
        self.sessions = []
        self.current_session_index = 0
        self.lock = threading.Lock()

        # 创建多个会话
        for _ in range(3):
            session = requests.Session()
            # 设置cookie jar
            session.cookies = CookieJar()
            # 设置适配器
            adapter = requests.adapters.HTTPAdapter(pool_connections=1,
                                                    pool_maxsize=1,
                                                    max_retries=0)
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            self.sessions.append(session)

    def get_session(self) -> requests.Session:
        """轮流获取会话"""
        with self.lock:
            session = self.sessions[self.current_session_index]
            self.current_session_index = (self.current_session_index +
                                          1) % len(self.sessions)
            return session

    def refresh_session(self, session: requests.Session):
        """刷新会话（清除cookies等）"""
        session.cookies.clear()


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
            self.requests = [
                req_time for req_time in self.requests
                if now - req_time < self.window_seconds
            ]

            # 如果达到限制，等待
            if len(self.requests) >= self.max_requests:
                sleep_time = self.window_seconds - (now -
                                                    self.requests[0]) + 0.1
                if sleep_time > 0:
                    logger.debug(f"速率限制：等待 {sleep_time:.2f} 秒")
                    time.sleep(sleep_time)
                    # 递归调用以重新检查
                    self.wait_if_needed()
            else:
                # 记录这次请求
                self.requests.append(now)


def retry_with_backoff(
        max_retries: int = EnhancedAntiSpiderConfig.MAX_RETRIES):
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
                    msg = f'{func.__name__} 失败: {e}, 请打开网站人工验证：https://www.eastmoney.com/default.html \n\n{traceback.format_exc()}'
                    if not last_exception:
                        send_email("【数据抓取失败】请人工验证", msg)
                    # logger.exception(msg)
                    os.system(f'start https://www.eastmoney.com/default.html')

                    # 等待用户验证后继续
                    input("请验证后按回车继续...")

                    last_exception = e
                    if attempt < max_retries - 1:
                        delay = EnhancedAntiSpiderConfig.RETRY_DELAY_BASE * (
                            EnhancedAntiSpiderConfig.RETRY_DELAY_MULTIPLIER**
                            attempt)
                        # 添加随机抖动
                        delay *= (0.5 + random.random())
                        logger.warning(
                            f"{func.__name__} 失败 (尝试 {attempt + 1}/{max_retries}): {e}. "
                            f"等待 {delay:.2f} 秒后重试...")
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"{func.__name__} 在 {max_retries} 次尝试后失败: {e}")
            raise last_exception

        return wrapper

    return decorator


class EnhancedSectorDataFetcher:
    """
    增强的板块数据获取模块
    包含更强的反爬措施
    """
    def __init__(self,
                 config: SectorConfig,
                 sector_type: SectorType,
                 use_anti_spider: bool = True):
        self.config = config
        self.sector_type = sector_type
        self.use_anti_spider = use_anti_spider

        # 会话管理器
        self.session_manager = SessionManager()

        # 速率限制器
        self.rate_limiter = RateLimiter(
            EnhancedAntiSpiderConfig.MAX_REQUESTS_PER_WINDOW,
            EnhancedAntiSpiderConfig.RATE_LIMIT_WINDOW)

        # UT令牌生成器
        self.ut_generator = UtTokenGenerator()

        # 请求计数器（用于轮换策略）
        self.request_count = 0
        self.request_lock = threading.Lock()

    def _get_random_headers(self) -> Dict[str, str]:
        """获取随机请求头"""
        # 基础头
        headers = {
            'User-Agent': random.choice(EnhancedAntiSpiderConfig.USER_AGENTS),
            'Referer': random.choice(EnhancedAntiSpiderConfig.REFERERS),
            'Accept': 'application/json, text/plain, */*',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        }

        # 添加随机浏览器指纹
        browser_config = random.choice(
            EnhancedAntiSpiderConfig.BROWSER_CONFIGS)
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
            base_delay = random.uniform(EnhancedAntiSpiderConfig.MIN_DELAY,
                                        EnhancedAntiSpiderConfig.MAX_DELAY)

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
    def fetch_quotes_page(self,
                          page_num: int = 1,
                          page_size: int = 100) -> Optional[Dict]:
        """获取单页的板块实时行情数据"""
        # 速率限制
        if self.use_anti_spider:
            self.rate_limiter.wait_if_needed()

        # 获取会话
        session = self.session_manager.get_session()

        try:
            # 动态生成参数
            params = {
                'cb':
                f'jQuery{random.randint(1000000000000000, 9999999999999999)}_{int(time.time() * 1000)}',
                'fid': 'f3',
                'po': '1',
                'pz': str(page_size),
                'pn': str(page_num),
                'np': '1',
                'fltt': '2',
                'invt': '2',
                'ut': self._get_dynamic_ut(),  # 使用动态ut
                'fs': self.config.SECTOR_FILTER_PARAMS[self.sector_type.name],
                'fields': ','.join(self.config.QUOTE_FIELD_MAPPING.keys()),
                '_': str(int(time.time() * 1000))
            }

            # 随机化一些参数顺序
            if random.random() < 0.3:
                params['dect'] = '1'  # 添加一些额外参数

            response = session.get(
                self.config.SECTOR_QUOTE_URL,
                params=params,
                headers=self._get_random_headers(),
                timeout=random.uniform(15, 25)  # 随机超时时间
            )
            response.raise_for_status()

            # 随机延迟
            self._random_delay()

            # 解析JSONP响应
            content = response.text
            json_start_index = content.find('(')
            json_end_index = content.rfind(')')

            if json_start_index != -1 and json_end_index != -1:
                json_str = content[json_start_index + 1:json_end_index]
                return json.loads(json_str)
            else:
                raise ValueError("无法解析JSONP响应")

        except Exception as e:
            logger.error(
                f"获取{self.sector_type.value}板块行情失败 (页 {page_num}): {e}, {traceback.print_exc()}"
            )

            # 错误时可能需要刷新会话
            if "403" in str(e) or "forbidden" in str(e).lower():
                self.session_manager.refresh_session(session)

            raise

    def fetch_all_quotes(self, max_pages: Optional[int] = 1) -> List[Dict]:
        """获取所有板块的实时行情数据"""
        all_raw_quotes_data = []

        # 获取第一页确定总数
        first_page_response = self.fetch_quotes_page(1, 100)
        if not first_page_response or 'data' not in first_page_response:
            return all_raw_quotes_data

        total_records = first_page_response['data'].get('total', 0)
        if total_records == 0:
            return all_raw_quotes_data

        total_pages = (total_records + 99) // 100
        logger.info(
            f"{self.sector_type.value}板块总数: {total_records}, 总页数: {total_pages}"
        )

        # 添加第一页数据
        if 'diff' in first_page_response['data']:
            all_raw_quotes_data.extend(first_page_response['data']['diff'])

        # 如果只获取第一页数据
        if max_pages == 1:
            logger.info(
                f"仅获取第一页数据，共 {len(all_raw_quotes_data)} 条{self.sector_type.value}板块数据"
            )
            return all_raw_quotes_data
        elif max_pages is not None and total_pages > max_pages:
            total_pages = min(total_pages, max_pages)
            logger.info(f"限制获取到第 {total_pages} 页数据")

        # 串行获取剩余页面
        for page_num in range(2, total_pages + 1):
            try:
                # 偶尔添加更长的页面间延迟
                if page_num % 5 == 0:
                    extra_delay = random.uniform(2, 5)
                    logger.info(f"页面 {page_num}: 额外延迟 {extra_delay:.1f} 秒")
                    time.sleep(extra_delay)

                page_data = self.fetch_quotes_page(page_num, 100)
                if page_data and 'data' in page_data and 'diff' in page_data[
                        'data']:
                    all_raw_quotes_data.extend(page_data['data']['diff'])
                    logger.info(f"获取第 {page_num}/{total_pages} 页完成")
            except Exception as e:
                logger.error(f"获取第 {page_num} 页失败: {e}")

        logger.info(
            f"成功获取 {len(all_raw_quotes_data)} 条{self.sector_type.value}板块数据")
        return all_raw_quotes_data

    @retry_with_backoff()
    def fetch_constituents_page(self,
                                sector_code: str,
                                page_num: int = 1,
                                page_size: int = 200) -> Optional[Dict]:
        """获取指定板块的单页成分股数据"""
        # 速率限制
        if self.use_anti_spider:
            self.rate_limiter.wait_if_needed()

        # 获取会话
        session = self.session_manager.get_session()

        try:
            params = {
                'cb':
                f'jQuery{random.randint(1000000000000000, 9999999999999999)}_{int(time.time() * 1000)}',
                'fid': 'f3',
                'po': '1',
                'pz': str(page_size),
                'pn': str(page_num),
                'np': '1',
                'fltt': '2',
                'invt': '2',
                'ut': self._get_dynamic_ut(),  # 使用动态ut
                'fs': f'b:{sector_code}+f:!50',
                'fields':
                ','.join(self.config.CONSTITUENT_FIELD_MAPPING.keys()),
                '_': str(int(time.time() * 1000))
            }

            response = session.get(self.config.SECTOR_QUOTE_URL,
                                   params=params,
                                   headers=self._get_random_headers(),
                                   timeout=random.uniform(15, 30))
            response.raise_for_status()

            # 随机延迟
            self._random_delay()

            # 解析响应
            content = response.text
            json_start_index = content.find('(')
            json_end_index = content.rfind(')')

            if json_start_index != -1 and json_end_index != -1:
                json_str = content[json_start_index + 1:json_end_index]
                return json.loads(json_str)
            else:
                raise ValueError("无法解析JSONP响应")

        except Exception as e:
            logger.error(f"获取板块 {sector_code} 成分股失败 (页 {page_num}): {e}")

            # 错误时可能需要刷新会话
            if "403" in str(e) or "forbidden" in str(e).lower():
                self.session_manager.refresh_session(session)

            raise

    def fetch_all_constituents(self, sector_code: str) -> List[Dict]:
        """获取指定板块的所有成分股数据"""
        all_raw_constituents = []
        API_EFFECTIVE_PAGE_SIZE = 100

        first_page_response = self.fetch_constituents_page(
            sector_code, 1, API_EFFECTIVE_PAGE_SIZE)

        if not first_page_response or 'data' not in first_page_response:
            return all_raw_constituents

        total_records = first_page_response['data'].get('total', 0)

        if 'diff' in first_page_response['data']:
            all_raw_constituents.extend(first_page_response['data']['diff'])

        if total_records == 0:
            return all_raw_constituents

        total_pages = (total_records + API_EFFECTIVE_PAGE_SIZE -
                       1) // API_EFFECTIVE_PAGE_SIZE

        # 串行获取剩余页面
        for page_num in range(2, total_pages + 1):
            try:
                page_data = self.fetch_constituents_page(
                    sector_code, page_num, API_EFFECTIVE_PAGE_SIZE)
                if page_data and 'data' in page_data and 'diff' in page_data[
                        'data']:
                    all_raw_constituents.extend(page_data['data']['diff'])
            except Exception as e:
                logger.error(f"获取板块 {sector_code} 第 {page_num} 页失败: {e}")

        return all_raw_constituents


class SectorDataParser:
    """数据解析器（保持原有实现）"""
    def __init__(self, config: SectorConfig):
        self.config = config

    def parse_quotes_data(self, raw_quotes_list: List[Dict]) -> pd.DataFrame:
        """解析板块行情数据为DataFrame"""
        parsed_quotes = []

        if isinstance(raw_quotes_list, dict):
            raw_quotes_list = [raw_quotes_list]

        for raw_data in raw_quotes_list:
            parsed_record = {}
            for raw_key, chinese_name in self.config.QUOTE_FIELD_MAPPING.items(
            ):
                value = raw_data.get(raw_key)

                # 数据类型转换
                if raw_key in [
                        'f2', 'f3', 'f4', 'f22', 'f11', 'f184', 'f69', 'f165',
                        'f167', 'f175', 'f177', 'f136'
                ]:
                    try:
                        parsed_record[chinese_name] = float(
                            value) if value is not None else 0.0
                    except (ValueError, TypeError):
                        parsed_record[chinese_name] = 0.0
                elif raw_key in [
                        'f5', 'f6', 'f104', 'f105', 'f62', 'f66', 'f164',
                        'f166', 'f174', 'f176'
                ]:
                    try:
                        parsed_record[chinese_name] = int(
                            value) if value is not None else 0
                    except (ValueError, TypeError):
                        parsed_record[chinese_name] = 0
                else:
                    parsed_record[
                        chinese_name] = value if value is not None else ''

            parsed_quotes.append(parsed_record)

        return pd.DataFrame(parsed_quotes)


class SectorScraper:
    """
    增强的板块爬虫主类
    包含更强的反爬措施
    """
    def __init__(self,
                 sector_type: SectorType,
                 output_dir: str = None,
                 use_anti_spider: bool = True):
        self.sector_type = sector_type
        self.config = SectorConfig()
        self.fetcher = EnhancedSectorDataFetcher(
            self.config, sector_type, use_anti_spider=use_anti_spider)
        self.parser = SectorDataParser(self.config)
        self.use_anti_spider = use_anti_spider

        # 设置输出目录
        if output_dir is None:
            output_dir = f"output/{sector_type.value}_sectors"
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def scrape_stock_to_sector_mapping(
            self,
            max_workers: int = None,
            batch_size: int = 5,
            progress_callback=None) -> Dict[str, List[str]]:
        """
        增强的爬取所有板块及其成分股方法
        
        Args:
            max_workers: 最大并发数，None表示使用默认值
            batch_size: 批处理大小，每批处理的板块数（减少到5）
            progress_callback: 进度回调函数
            
        Returns:
            Dict[str, List[str]]: 股票代码到板块列表的映射
        """
        if max_workers is None:
            max_workers = EnhancedAntiSpiderConfig.DEFAULT_MAX_WORKERS

        logger.info(f"开始构建股票-{self.sector_type.value}板块映射（增强版）...")
        stock_to_sector_map: Dict[str, List[str]] = {}

        # 获取所有板块
        raw_sectors = self.fetcher.fetch_all_quotes(
            max_pages=100)  # 限制获取100页数据
        if not raw_sectors:
            logger.error("未能获取板块列表")
            return stock_to_sector_map

        sectors_to_process = [
            {
                'code': sector_data.get('f12'),
                'name': sector_data.get('f14')
            } for sector_data in raw_sectors
            if sector_data.get('f12') and sector_data.get('f14')
        ]

        total_sectors = len(sectors_to_process)
        logger.info(f"获取到 {total_sectors} 个板块待处理")

        # 分批处理
        processed_count = 0
        failed_sectors = []

        for batch_start in range(0, total_sectors, batch_size):
            batch_end = min(batch_start + batch_size, total_sectors)
            batch_sectors = sectors_to_process[batch_start:batch_end]

            logger.info(f"处理批次 {batch_start//batch_size + 1}: "
                        f"板块 {batch_start + 1}-{batch_end}/{total_sectors}")

            # 使用线程池处理当前批次
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures_map = {
                    executor.submit(self._fetch_and_parse_constituents_safe,
                                    sector_info): sector_info
                    for sector_info in batch_sectors
                }

                for future in as_completed(futures_map):
                    sector_info = futures_map[future]
                    sector_code = sector_info['code']
                    sector_name = sector_info['name']
                    processed_count += 1

                    try:
                        stock_codes = future.result()
                        if stock_codes:
                            for stock_code in stock_codes:
                                if stock_code not in stock_to_sector_map:
                                    stock_to_sector_map[stock_code] = []
                                if sector_code not in stock_to_sector_map[
                                        stock_code]:
                                    stock_to_sector_map[stock_code].append(
                                        sector_code)

                            logger.info(f"[{processed_count}/{total_sectors}] "
                                        f"板块 {sector_name} ({sector_code}): "
                                        f"{len(stock_codes)} 个成分股")
                        else:
                            logger.warning(
                                f"[{processed_count}/{total_sectors}] "
                                f"板块 {sector_name} ({sector_code}): 无成分股")

                        # 进度回调
                        if progress_callback:
                            progress_callback(processed_count, total_sectors)

                    except Exception as e:
                        logger.error(
                            f"处理板块 {sector_name} ({sector_code}) 失败: {e}")
                        failed_sectors.append(sector_info)

            # 批次间延迟（增强）
            if batch_end < total_sectors and self.use_anti_spider:
                # 动态调整延迟
                base_delay = random.uniform(5, 10)
                # 根据失败率调整
                failure_rate = len(
                    failed_sectors
                ) / processed_count if processed_count > 0 else 0
                if failure_rate > 0.1:  # 失败率超过10%
                    base_delay *= 2
                    logger.warning(
                        f"失败率较高 ({failure_rate:.1%})，增加延迟到 {base_delay:.1f} 秒")

                logger.info(f"批次间延迟 {base_delay:.1f} 秒...")
                time.sleep(base_delay)

        # 统计结果
        success_rate = (processed_count - len(failed_sectors)
                        ) / processed_count if processed_count > 0 else 0
        logger.info(f"处理完成！成功率: {success_rate:.1%}")
        logger.info(f"成功处理: {processed_count - len(failed_sectors)} 个板块")
        logger.info(f"失败: {len(failed_sectors)} 个板块")
        logger.info(f"股票总数: {len(stock_to_sector_map)}")

        # 如果失败率过高，发出警告
        if len(failed_sectors) > total_sectors * 0.2:
            logger.warning("警告：失败率超过20%，可能被反爬系统限制")

        return stock_to_sector_map

    def _fetch_and_parse_constituents_safe(self,
                                           sector_info: Dict) -> List[str]:
        """安全地获取和解析成分股"""
        try:
            raw_constituents = self.fetcher.fetch_all_constituents(
                sector_info['code'])
            if raw_constituents:
                return [
                    stock.get('f12') for stock in raw_constituents
                    if stock.get('f12')
                ]
            return []
        except Exception as e:
            logger.error(
                f"获取板块 {sector_info['name']} ({sector_info['code']}) 成分股失败: {e}"
            )
            raise

    def save_mapping_data(self,
                          mapping_data: Dict[str, List[str]],
                          filename_suffix: str = "") -> str:
        """保存映射数据"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"stock_to_{self.sector_type.value}_mapping{filename_suffix}_{timestamp}.json"
        filepath = os.path.join(self.output_dir, filename)

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(mapping_data, f, ensure_ascii=False, indent=2)
            logger.info(f"映射数据已保存到: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"保存映射数据失败: {e}")
            raise

    def save_quotes_data(self,
                         quotes_data_df: pd.DataFrame,
                         filename_suffix: str = "") -> str:
        """保存板块行情数据"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.sector_type.value}_quotes_data{filename_suffix}_{timestamp}.csv"
        filepath = os.path.join(self.output_dir, filename)

        try:
            quotes_data_df.to_csv(filepath, index=False, encoding='utf-8-sig')
            logger.info(f"行情数据已保存到: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"保存行情数据失败: {e}")
            raise


class StockCapitalFlowConfig:
    """
    个股资金流向爬虫配置类
    存储API端点、请求头、字段映射等常量信息
    """

    # API接口地址
    CAPITAL_FLOW_URL = "https://push2.eastmoney.com/api/qt/clist/get"

    # 市场类型对应的筛选参数
    MARKET_FILTER_PARAMS = {
        MarketType.ALL:
        'm:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2',
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
    def __init__(self,
                 config: StockCapitalFlowConfig,
                 market_type: MarketType = MarketType.ALL,
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
            EnhancedAntiSpiderConfig.RATE_LIMIT_WINDOW)

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
            'Accept': 'application/json, text/plain, */*',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        }

        # 添加随机浏览器指纹
        browser_config = random.choice(
            EnhancedAntiSpiderConfig.BROWSER_CONFIGS)
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
            base_delay = random.uniform(EnhancedAntiSpiderConfig.MIN_DELAY,
                                        EnhancedAntiSpiderConfig.MAX_DELAY)

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
                'cb':
                f'jQuery{random.randint(1000000000000000, 9999999999999999)}_{int(time.time() * 1000)}',
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
                timeout=random.uniform(15, 25)  # 随机超时时间
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
            logger.error(
                f"获取{self.market_type.value}市场资金流向失败 (页 {page_num}): {e}, {traceback.print_exc()}"
            )

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

        if not first_page_response or 'data' not in first_page_response or not first_page_response[
                'data']:
            logger.warning(
                f"获取{self.market_type.value}市场资金流向第一页数据失败或数据为空，无法继续获取")
            return all_raw_data

        total_records = first_page_response['data'].get('total', 0)
        if total_records == 0:
            logger.info(f"{self.market_type.value}市场资金流向总数为0，无需进一步获取")
            if 'diff' in first_page_response['data'] and first_page_response[
                    'data']['diff']:
                all_raw_data.extend(first_page_response['data']['diff'])
            return all_raw_data

        # 计算实际需要获取的页数
        total_pages = min(max_pages,
                          (total_records + page_size_for_total_count - 1) //
                          page_size_for_total_count)

        logger.info(
            f"{self.market_type.value}市场资金流向总数: {total_records}, 每页大小: {page_size_for_total_count}, 将获取: {total_pages}页"
        )

        # 添加第一页的数据到结果列表
        if 'diff' in first_page_response['data'] and first_page_response[
                'data']['diff']:
            all_raw_data.extend(first_page_response['data']['diff'])

        # 串行获取剩余页面，减少并发以降低被封风险
        for page_num in range(2, total_pages + 1):
            try:
                # 偶尔添加更长的页面间延迟
                if page_num % 5 == 0:
                    extra_delay = random.uniform(2, 5)
                    logger.info(f"页面 {page_num}: 额外延迟 {extra_delay:.1f} 秒")
                    time.sleep(extra_delay)

                page_data = self.fetch_capital_flow_page(
                    page_num, page_size_for_total_count)
                if page_data and 'data' in page_data and 'diff' in page_data[
                        'data'] and page_data['data']['diff']:
                    all_raw_data.extend(page_data['data']['diff'])
                    logger.info(f"获取第 {page_num}/{total_pages} 页完成")
                else:
                    logger.warning(
                        f"获取{self.market_type.value}市场资金流向第 {page_num} 页数据失败或数据不完整"
                    )
            except Exception as e:
                logger.error(f"获取第 {page_num} 页失败: {e}")

        logger.info(
            f"成功获取 {len(all_raw_data)} 条原始{self.market_type.value}市场资金流向数据")
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

    def parse_capital_flow_data(self,
                                raw_data_list: List[Dict]) -> pd.DataFrame:
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
                elif ('流入' in chinese_name and '占比' not in chinese_name
                      ) or chinese_name == '成交额' or chinese_name == '涨跌额':
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
                            parsed_value = datetime.fromtimestamp(
                                int(raw_value)).strftime('%Y-%m-%d %H:%M:%S')
                        else:
                            parsed_value = datetime.now().strftime(
                                '%Y-%m-%d %H:%M:%S')
                    except (ValueError, TypeError):
                        parsed_value = datetime.now().strftime(
                            '%Y-%m-%d %H:%M:%S')
                else:
                    # 其他字段直接使用
                    parsed_value = raw_value

                parsed_item[chinese_name] = parsed_value

            # 添加数据获取时间
            parsed_item['数据获取时间'] = datetime.now().strftime(
                '%Y-%m-%d %H:%M:%S')
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
    def __init__(self,
                 market_type: MarketType = MarketType.ALL,
                 output_dir: str = None,
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
        self.fetcher = StockCapitalFlowFetcher(self.config, market_type,
                                               use_anti_spider)
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

            logger.info(
                f"成功完成{self.market_type.value}市场个股资金流向数据爬取，共 {len(df)} 条记录")
            return df

        except Exception as e:
            logger.error(f"爬取{self.market_type.value}市场个股资金流向数据时发生错误: {e}")
            return pd.DataFrame()

    def save_data(self,
                  df: pd.DataFrame,
                  filename_prefix: str = "stock_capital_flow") -> str:
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

    def save_to_json(self,
                     df: pd.DataFrame,
                     filename_prefix: str = "stock_capital_flow") -> str:
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

    def run_once(self,
                 max_pages: int = 10,
                 save_format: str = 'csv') -> Tuple[pd.DataFrame, str]:
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

    def start_scheduled_scraping(self,
                                 interval_seconds: int = 60,
                                 max_pages: int = 10,
                                 save_format: str = 'csv'):
        """
        开始定时爬取

        Args:
            interval_seconds (int): 爬取间隔秒数
            max_pages (int): 每次最大获取页数
            save_format (str): 保存格式
        """
        self.is_running = True
        logger.info(
            f"开始定时爬取{self.market_type.value}市场个股资金流向数据，间隔: {interval_seconds}秒"
        )

        while self.is_running:
            try:
                df, filepath = self.run_once(max_pages=max_pages,
                                             save_format=save_format)
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

    def get_top_inflow_stocks(self,
                              df: pd.DataFrame,
                              top_n: int = 20) -> pd.DataFrame:
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

        return df.nlargest(top_n, '主力净流入')[[
            '股票代码', '股票名称', '最新价', '涨跌幅', '主力净流入', '主力净流入占比', '数据获取时间'
        ]]

    def get_top_outflow_stocks(self,
                               df: pd.DataFrame,
                               top_n: int = 20) -> pd.DataFrame:
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

        return df.nsmallest(top_n, '主力净流入')[[
            '股票代码', '股票名称', '最新价', '涨跌幅', '主力净流入', '主力净流入占比', '数据获取时间'
        ]]

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
            '总股票数':
            len(df),
            '主力净流入股票数':
            len(df[df['主力净流入'] > 0]) if '主力净流入' in df.columns else 0,
            '主力净流出股票数':
            len(df[df['主力净流入'] < 0]) if '主力净流入' in df.columns else 0,
            '市场主力净流入总额(万元)':
            round(df['主力净流入'].sum(), 2) if '主力净流入' in df.columns else 0,
            '平均主力净流入(万元)':
            round(df['主力净流入'].mean(), 2) if '主力净流入' in df.columns else 0,
            '上涨股票数':
            len(df[df['涨跌幅'] > 0]) if '涨跌幅' in df.columns else 0,
            '下跌股票数':
            len(df[df['涨跌幅'] < 0]) if '涨跌幅' in df.columns else 0,
            '平涨股票数':
            len(df[df['涨跌幅'] == 0]) if '涨跌幅' in df.columns else 0,
            '数据获取时间':
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        return summary


class StockCapitalFlowAnalyzer:
    """个股资金流向分析器"""
    def __init__(self, data_dir: str = "output/stock_capital_flow_data_all"):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

    def load_latest_data(
            self,
            filename_pattern: str = "stock_capital_flow") -> pd.DataFrame:
        """
        加载最新的资金流向数据
        
        Args:
            filename_pattern (str): 文件名模式
            
        Returns:
            pd.DataFrame: 最新的数据
        """
        try:
            # 查找最新的CSV文件
            csv_files = [
                f for f in os.listdir(self.data_dir)
                if f.startswith(filename_pattern) and f.endswith('.csv')
            ]

            if not csv_files:
                logger.warning(f"在目录 {self.data_dir} 中未找到资金流向数据文件")
                return pd.DataFrame()

            # 按文件名排序，获取最新文件
            latest_file = sorted(csv_files)[-1]
            filepath = os.path.join(self.data_dir, latest_file)

            df = pd.read_csv(filepath, encoding='utf-8-sig')
            logger.info(f"成功加载数据文件: {latest_file}, 数据条数: {len(df)}")

            return df

        except Exception as e:
            logger.error(f"加载数据时发生错误: {e}")
            return pd.DataFrame()

    def load_historical_data(self,
                             days: int = 7,
                             filename_pattern: str = "stock_capital_flow"
                             ) -> List[pd.DataFrame]:
        """
        加载历史数据
        
        Args:
            days (int): 加载最近几天的数据
            filename_pattern (str): 文件名模式
            
        Returns:
            List[pd.DataFrame]: 历史数据列表
        """
        try:
            csv_files = [
                f for f in os.listdir(self.data_dir)
                if f.startswith(filename_pattern) and f.endswith('.csv')
            ]

            if not csv_files:
                return []

            # 按时间排序，获取最近的文件
            csv_files.sort()
            cutoff_date = datetime.now() - timedelta(days=days)

            historical_data = []
            for filename in csv_files[-days * 10:]:  # 取更多文件以防有些时间段没有数据
                try:
                    # 从文件名提取时间信息
                    time_part = filename.split('_')[-2] + '_' + filename.split(
                        '_')[-1].replace('.csv', '')
                    file_time = datetime.strptime(time_part, '%Y%m%d_%H%M%S')

                    if file_time >= cutoff_date:
                        filepath = os.path.join(self.data_dir, filename)
                        df = pd.read_csv(filepath, encoding='utf-8-sig')
                        if not df.empty:
                            df['文件时间'] = file_time
                            historical_data.append(df)

                except Exception as e:
                    logger.warning(f"解析文件 {filename} 时出错: {e}")
                    continue

            logger.info(f"成功加载 {len(historical_data)} 个历史数据文件")
            return historical_data

        except Exception as e:
            logger.error(f"加载历史数据时发生错误: {e}")
            return []

    def get_top_inflow_stocks(self,
                              df: pd.DataFrame,
                              top_n: int = 20) -> pd.DataFrame:
        """获取主力净流入最多的股票"""
        if df.empty or '主力净流入' not in df.columns:
            return pd.DataFrame()

        return df.nlargest(top_n, '主力净流入')[[
            '股票代码', '股票名称', '最新价', '涨跌幅', '主力净流入', '主力净流入占比', '数据获取时间'
        ]]

    def get_top_outflow_stocks(self,
                               df: pd.DataFrame,
                               top_n: int = 20) -> pd.DataFrame:
        """获取主力净流出最多的股票"""
        if df.empty or '主力净流入' not in df.columns:
            return pd.DataFrame()

        return df.nsmallest(top_n, '主力净流入')[[
            '股票代码', '股票名称', '最新价', '涨跌幅', '主力净流入', '主力净流入占比', '数据获取时间'
        ]]

    def analyze_continuous_inflow_stocks(self,
                                         historical_data: List[pd.DataFrame],
                                         days: int = 3) -> pd.DataFrame:
        """
        分析连续多日主力净流入的股票
        
        Args:
            historical_data (List[pd.DataFrame]): 历史数据列表
            days (int): 连续天数
            
        Returns:
            pd.DataFrame: 连续流入的股票
        """
        if not historical_data or len(historical_data) < days:
            return pd.DataFrame()

        try:
            # 合并历史数据
            all_data = pd.concat(historical_data, ignore_index=True)

            # 按股票代码分组分析
            continuous_stocks = []

            for stock_code, group in all_data.groupby('股票代码'):
                # 按时间排序
                group_sorted = group.sort_values('文件时间')

                # 获取最近几天的数据
                recent_data = group_sorted.tail(days)

                if len(recent_data) >= days:
                    # 检查是否连续流入
                    inflow_values = recent_data['主力净流入'].values
                    if all(val > 0 for val in inflow_values):
                        latest_data = recent_data.iloc[-1]
                        continuous_stocks.append({
                            '股票代码': stock_code,
                            '股票名称': latest_data['股票名称'],
                            '最新价': latest_data['最新价'],
                            '涨跌幅': latest_data['涨跌幅'],
                            '连续流入天数': days,
                            '累计流入': inflow_values.sum(),
                            '平均每日流入': inflow_values.mean(),
                            '最新流入': latest_data['主力净流入']
                        })

            result_df = pd.DataFrame(continuous_stocks)
            if not result_df.empty:
                result_df = result_df.sort_values('累计流入', ascending=False)

            return result_df

        except Exception as e:
            logger.error(f"分析连续流入股票时发生错误: {e}")
            return pd.DataFrame()

    def calculate_market_sentiment(self, df: pd.DataFrame) -> Dict:
        """
        计算市场情绪指标
        
        Args:
            df (pd.DataFrame): 股票数据
            
        Returns:
            Dict: 市场情绪指标
        """
        if df.empty:
            return {}

        try:
            total_stocks = len(df)
            inflow_stocks = len(
                df[df['主力净流入'] > 0]) if '主力净流入' in df.columns else 0
            outflow_stocks = len(
                df[df['主力净流入'] < 0]) if '主力净流入' in df.columns else 0

            up_stocks = len(df[df['涨跌幅'] > 0]) if '涨跌幅' in df.columns else 0
            down_stocks = len(df[df['涨跌幅'] < 0]) if '涨跌幅' in df.columns else 0

            total_inflow = df['主力净流入'].sum() if '主力净流入' in df.columns else 0

            sentiment = {
                '总股票数':
                total_stocks,
                '主力净流入股票数':
                inflow_stocks,
                '主力净流出股票数':
                outflow_stocks,
                '上涨股票数':
                up_stocks,
                '下跌股票数':
                down_stocks,
                '资金流入比例':
                round(inflow_stocks / total_stocks *
                      100, 2) if total_stocks > 0 else 0,
                '上涨股票比例':
                round(up_stocks / total_stocks *
                      100, 2) if total_stocks > 0 else 0,
                '市场总流入(万元)':
                round(total_inflow, 2),
                '平均流入(万元)':
                round(total_inflow /
                      total_stocks, 2) if total_stocks > 0 else 0,
                '更新时间':
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            return sentiment

        except Exception as e:
            logger.error(f"计算市场情绪时发生错误: {e}")
            return {}


class StockCapitalFlowMonitor:
    """个股资金流向监控器"""
    def __init__(self,
                 market_type: MarketType = MarketType.ALL,
                 output_dir: str = None,
                 max_pages: Optional[int] = 10):
        self.market_type = market_type
        self.max_pages = max_pages  # Store max_pages
        self.scraper = StockCapitalFlowScraper(market_type=market_type,
                                               output_dir=output_dir)
        self.analyzer = StockCapitalFlowAnalyzer(self.scraper.output_dir)

        # 监控状态控制 for start/stop/_run (legacy)
        self.is_running = False
        self.thread: Optional[threading.Thread] = None

        # 回调函数和数据存储
        self.callback: Optional[Callable[[pd.DataFrame], None]] = None
        self.interval = 10  # Default interval for legacy _run
        self.last_data: Optional[pd.DataFrame] = None
        logger.info(
            f"StockCapitalFlowMonitor initialized (market: {market_type.value}, max_pages: {self.max_pages})."
        )

    def set_callback(self, callback: Callable[[pd.DataFrame], None]) -> None:
        """设置数据更新回调函数"""
        self.callback = callback
        logger.debug("数据更新回调函数已设置")

    def get_latest_data(self) -> Optional[pd.DataFrame]:
        """获取最新的个股资金流向数据"""
        return self.last_data

    def start(self, interval: int = 10) -> None:
        """启动个股资金流向监控器"""
        if self.is_running:
            logger.warning("个股资金流向监控器已在运行中，无法重复启动")
            return

        self.interval = interval
        self.is_running = True

        # 创建并启动监控线程
        self.thread = threading.Thread(target=self._run,
                                       name="StockCapitalFlowMonitorThread",
                                       daemon=True)
        self.thread.start()

        logger.info(f"个股资金流向监控器已启动，数据更新间隔: {interval}秒")

    def stop(self) -> None:
        """停止个股资金流向监控器"""
        if not self.is_running:
            logger.info("个股资金流向监控器未在运行")
            return

        self.is_running = False

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("监控线程在5秒内未能正常结束")
            else:
                logger.info("监控线程已正常结束")

        self.thread = None
        logger.info("个股资金流向监控器已停止")

    def _run(self) -> None:
        """监控器主循环（内部方法） - Used by legacy start()/stop()"""
        logger.info(
            f"个股资金流向监控循环已开始 (market: {self.market_type.value}, max_pages: {self.max_pages})"
        )

        while self.is_running:
            try:
                # 获取个股资金流向数据
                # scraper.run_once itself calls scrape_all_data which now has max_pages
                df, filepath = self.scraper.run_once(max_pages=self.max_pages,
                                                     save_format=None)

                if df is not None and not df.empty:  # df can be None if scrape_all_data returns None
                    # 更新最新数据
                    self.last_data = df

                    # 调用回调函数
                    if self.callback:
                        try:
                            self.callback(df.copy())  # Pass a copy
                        except Exception as e:
                            logger.exception(f"回调函数执行出错: {e}", exc_info=True)

                    if filepath:  # Filepath might be None if save_format is None and not saved
                        logger.debug(
                            f"成功获取 {len(df)} 只股票的资金流向数据，保存到: {filepath}")
                    else:
                        logger.debug(f"成功获取 {len(df)} 只股票的资金流向数据 (未保存到文件)")
                else:
                    logger.warning("获取到的个股资金流向数据为空或获取失败")

                # 等待下次更新
                if self.is_running:  # Check again before sleep
                    time.sleep(self.interval)

            except KeyboardInterrupt:
                logger.info("监控器收到键盘中断信号，正在退出...")
                break  # Exit loop
            except Exception as e:
                logger.error(f"监控过程发生异常: {e}", exc_info=True)
                if self.is_running:  # Check again before sleep
                    time.sleep(
                        min(self.interval,
                            30))  # Wait a bit before retrying or stopping

        logger.info(f"个股资金流向监控循环已结束 (market: {self.market_type.value})")


class SectorMonitor:
    """
    板块实时数据监控器基类（支持概念板块和行业板块）。
    (Base class for real-time sector data monitor - supports both concept and industry sectors.)
    
    此监控器能够定时获取板块数据，支持自定义回调函数来处理更新的数据。
    适用于需要实时跟踪板块表现、资金流向变化的应用场景。
    
    (This monitor can periodically fetch sector data and supports custom
    callback functions to process updated data. Suitable for applications that need
    real-time tracking of sector performance and capital flow changes.)
    
    主要功能 (Main Features):
    - 支持概念板块和行业板块监控 (Support for both concept and industry sector monitoring)
    - 定时自动获取板块数据 (Automatic periodic fetching of sector data)
    - 支持自定义数据更新回调 (Support for custom data update callbacks)
    - 线程安全的启停控制 (Thread-safe start/stop control)
    - 异常处理和错误恢复 (Exception handling and error recovery)
    - 数据自动保存功能 (Automatic data saving functionality)
    """
    def __init__(self,
                 sector_type: Union[str, SectorType],
                 output_dir: str = None,
                 max_pages: Optional[int] = 1):
        """
        初始化板块监控器。
        (Initialize sector monitor.)
        
        Args:
            sector_type (Union[str, SectorType]): 板块类型，可选值：
                - "concept" 或 SectorType.CONCEPT: 概念板块
                - "industry" 或 SectorType.INDUSTRY: 行业板块
            output_dir (str): 数据文件输出目录。如果为None，将根据板块类型自动设置。
                (Output directory for data files. If None, will be set automatically based on sector type.)
        """
        # 处理板块类型参数
        if isinstance(sector_type, str):
            sector_type_map = {
                "concept": SectorType.CONCEPT,
                "industry": SectorType.INDUSTRY
            }
            if sector_type not in sector_type_map:
                raise ValueError(
                    f"无效的板块类型: {sector_type}。有效值为: {list(sector_type_map.keys())}"
                )
            sector_type = sector_type_map[sector_type]

        self.sector_type = sector_type
        self.max_pages = max_pages  # Store max_pages

        # 如果未指定输出目录，根据板块类型自动设置
        if output_dir is None:
            output_dir = f"output/{sector_type.name.lower()}_sector_data"

        # 创建板块爬虫实例
        # (Create sector scraper instance)
        self.scraper = SectorScraper(sector_type=sector_type,
                                     output_dir=output_dir)

        # 监控状态控制
        # (Monitor status control)
        self.is_running = False
        self.thread: Optional[threading.Thread] = None

        # 回调函数和数据存储
        # (Callback function and data storage)
        self.callback: Optional[Callable[[pd.DataFrame], None]] = None
        self.interval = 10
        self.last_data: Optional[pd.DataFrame] = None

        logger.debug(f"{sector_type.value}板块监控器已初始化，输出目录: {output_dir}")

    def set_callback(self, callback: Callable[[pd.DataFrame], None]) -> None:
        """
        设置数据更新回调函数。
        (Set data update callback function.)
        
        回调函数将在每次获取到新数据时被调用，接收DataFrame作为参数。
        (Callback function will be called every time new data is fetched,
        receiving DataFrame as parameter.)
        
        Args:
            callback (Callable[[pd.DataFrame], None]): 数据更新回调函数。
                (Data update callback function.)
        
        Example:
            >>> def my_callback(df):
            >>>     print(f"获取到 {len(df)} 个板块数据")
            >>>     top_gainer = df.iloc[0]
            >>>     print(f"领涨板块: {top_gainer['板块名称']}")
            >>>
            >>> # 监控概念板块
            >>> monitor = SectorMonitor(sector_type="concept")
            >>> monitor.set_callback(my_callback)
            >>>
            >>> # 监控行业板块
            >>> monitor = SectorMonitor(sector_type="industry")
            >>> monitor.set_callback(my_callback)
        """
        self.callback = callback
        logger.debug("数据更新回调函数已设置")

    def get_latest_data(self) -> Optional[pd.DataFrame]:
        """
        获取最新的板块数据。
        (Get the latest sector data.)
        
        Returns:
            Optional[pd.DataFrame]: 最新的板块数据，如果还没有数据则返回None。
                (Latest sector data, returns None if no data available yet.)
        """
        return self.last_data

    def start(self, interval: int = 10) -> None:
        """
        启动板块监控器。
        (Start the sector monitor.)
        
        Args:
            interval (int): 数据更新间隔（秒）。默认为 10秒。
                (Data update interval in seconds. Default is 10 seconds.)
        
        Note:
            如果监控器已经在运行，此方法会发出警告并直接返回。
            (If monitor is already running, this method will issue a warning and return.)
        """
        if self.is_running:
            logger.warning(f"{self.sector_type.value}板块监控器已在运行中，无法重复启动")
            return

        self.interval = interval
        self.is_running = True

        # 创建并启动监控线程
        # (Create and start monitoring thread)
        self.thread = threading.Thread(
            target=self._run,
            name=f"{self.sector_type.name}SectorMonitorThread",
            daemon=True  # 设置为守护线程，主程序退出时自动结束
        )
        self.thread.start()

        logger.info(f"{self.sector_type.value}板块监控器已启动，数据更新间隔: {interval}秒")

    def stop(self) -> None:
        """
        停止板块监控器。
        (Stop the sector monitor.)
        
        此方法会安全地停止监控线程，等待当前操作完成后再退出。
        (This method safely stops the monitoring thread, waiting for current
        operations to complete before exiting.)
        """
        if not self.is_running:
            logger.info(f"{self.sector_type.value}板块监控器未在运行")
            return

        # 设置停止标志
        # (Set stop flag)
        self.is_running = False

        # 等待线程结束
        # (Wait for thread to finish)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("监控线程在5秒内未能正常结束")
            else:
                logger.info("监控线程已正常结束")

        self.thread = None
        logger.info(f"{self.sector_type.value}板块监控器已停止")

    def _run(self) -> None:
        """
        监控器主循环（内部方法）。
        (Monitor main loop - internal method.)
        
        此方法在独立线程中运行，负责定时获取数据、调用回调函数和处理异常。
        (This method runs in a separate thread, responsible for periodic data fetching,
        callback invocation, and exception handling.)
        """
        logger.info(f"{self.sector_type.value}板块监控循环已开始")

        while self.is_running:
            try:
                # 获取板块数据
                # (Fetch sector data)
                data = self.scraper.fetcher.fetch_all_quotes(
                    max_pages=self.max_pages)
                df = self.scraper.parser.parse_quotes_data(data)

                if not df.empty:
                    # 更新最新数据
                    # (Update latest data)
                    self.last_data = df

                    # 调用回调函数
                    # (Call callback function)
                    if self.callback:
                        try:
                            self.callback(df)
                        except Exception as e:
                            logger.exception(f"回调函数执行出错: {e}")
                else:
                    logger.warning(f"获取到的{self.sector_type.value}板块数据为空")

                # 等待下次更新
                # (Wait for next update)
                time.sleep(self.interval)

            except KeyboardInterrupt:
                logger.info("监控器收到键盘中断信号，正在退出...")
                break
            except Exception as e:
                logger.error(f"监控过程发生异常: {e}", exc_info=True)
                # 发生异常后等待一段时间再继续，避免频繁出错
                # (Wait after exception before continuing to avoid frequent errors)
                time.sleep(min(self.interval, 30))

        logger.info(f"{self.sector_type.value}板块监控循环已结束")


class ConceptSectorMonitor(SectorMonitor):
    """
    概念板块实时数据监控器。
    (Real-time concept sector data monitor.)
    
    这是SectorMonitor的便捷子类，默认监控概念板块。
    (This is a convenience subclass of SectorMonitor that defaults to monitoring concept sectors.)
    """
    def __init__(self, output_dir: str = None, max_pages: Optional[int] = 1):
        """
        初始化概念板块监控器。
        
        Args:
            output_dir (str): 数据文件输出目录。如果为None，默认为"output/concept_sector_data"。
        """
        super().__init__(sector_type=SectorType.CONCEPT,
                         output_dir=output_dir,
                         max_pages=max_pages)


class IndustrySectorMonitor(SectorMonitor):
    """
    行业板块实时数据监控器。
    (Real-time industry sector data monitor.)
    
    这是SectorMonitor的便捷子类，默认监控行业板块。
    (This is a convenience subclass of SectorMonitor that defaults to monitoring industry sectors.)
    """
    def __init__(self, output_dir: str = None, max_pages: Optional[int] = 1):
        """
        初始化行业板块监控器。
        
        Args:
            output_dir (str): 数据文件输出目录。如果为None，默认为"output/industry_sector_data"。
        """
        super().__init__(sector_type=SectorType.INDUSTRY,
                         output_dir=output_dir,
                         max_pages=max_pages)


def get_stock_to_sector_mapping_enhanced(
        sector_type: Union[str, SectorType],
        save_to_file: bool = False,
        output_dir: str = None,
        max_workers: int = 1,
        batch_size: int = 5,
        use_anti_spider: bool = True,
        progress_callback=None) -> Dict[str, List[str]]:
    """
    获取个股到板块的映射关系（增强版 - v1.9.0新增）
    
    使用增强版爬虫，具有更强的反爬能力，包括动态UT令牌、多会话管理、深度请求随机化等。
    适用于在标准版仍然被封禁的情况下使用。
    
    Args:
        sector_type (Union[str, SectorType]): 板块类型，可选值：
            - "concept" 或 SectorType.CONCEPT: 概念板块
            - "industry" 或 SectorType.INDUSTRY: 行业板块
        save_to_file (bool): 是否将映射关系保存到JSON文件，默认为False
        output_dir (str): 数据文件的输出目录，如果为None则根据板块类型自动设置
        max_workers (int): 并行处理的最大线程数，默认为1（增强版建议保持为1）
        batch_size (int): 批处理大小，每批处理的板块数量，默认为5（增强版更保守）
        progress_callback: 进度回调函数，接收(current, total)两个参数
    
    Returns:
        Dict[str, List[str]]: 股票代码到板块列表的映射字典
    
    Example:
        >>> # 使用增强版获取概念板块映射
        >>> from eastmoney_scraper import get_stock_to_sector_mapping_enhanced
        >>>
        >>> def show_progress(cur, total):
        ...     print(f"\\r进度: {cur}/{total} ({cur/total*100:.1f}%)", end='')
        >>>
        >>> mapping = get_stock_to_sector_mapping_enhanced(
        ...     "concept",
        ...     save_to_file=True,
        ...     progress_callback=show_progress
        ... )
        >>> print(f"\\n成功获取 {len(mapping)} 只股票的映射关系")
        
        >>> # 获取行业板块映射
        >>> industry_mapping = get_stock_to_sector_mapping_enhanced(
        ...     SectorType.INDUSTRY,
        ...     batch_size=3  # 行业板块较少，可以更小的批次
        ... )
    
    Note:
        - 增强版爬取速度明显慢于标准版，但稳定性更高
        - 使用动态UT令牌，每个请求都有不同的令牌
        - 使用多会话轮换，分散请求特征
        - 请求延迟增加到1-3秒，并有10%概率触发长延迟
        - 如果增强版仍然被封，建议等待24-48小时后再试
    """

    # 处理板块类型参数
    if isinstance(sector_type, str):
        sector_type_map = {
            "concept": SectorType.CONCEPT,
            "industry": SectorType.INDUSTRY
        }
        if sector_type not in sector_type_map:
            raise ValueError(
                f"无效的板块类型: {sector_type}。有效值为: {list(sector_type_map.keys())}")
        sector_type = sector_type_map[sector_type]

    # 创建增强版爬虫实例
    scraper = SectorScraper(
        sector_type=sector_type,
        output_dir=output_dir,
        use_anti_spider=use_anti_spider  # 增强版始终启用反爬
    )

    # 爬取股票到板块的映射关系
    logger.info(f"开始使用增强版爬虫获取股票到{sector_type.value}板块映射关系...")
    logger.info("增强版特性：动态UT令牌、多会话管理、深度请求随机化")
    logger.info(f"批处理大小: {batch_size}, 预计耗时较长，请耐心等待...")

    mapping = scraper.scrape_stock_to_sector_mapping(
        max_workers=max_workers,
        batch_size=batch_size,
        progress_callback=progress_callback)

    # 如果需要保存文件
    if save_to_file:
        scraper.save_mapping_data(mapping, "_enhanced")
        logger.info(f"股票到{sector_type.value}板块映射（增强版）已保存到文件")

    logger.info(f"成功获取 {len(mapping)} 只股票的{sector_type.value}板块映射关系")
    return mapping
