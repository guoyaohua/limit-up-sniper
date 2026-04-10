"""
同花顺热点数据抓取器
"""

import requests
import json
import time
import random
import asyncio
from datetime import datetime
from typing import Optional
from logger_config import logger
from .models import HotStock, HotSector, HotSpotData, TopicInfo, ETFInfo, MarketAnalysis

class THSHotSpotScraper:
    """同花顺热点股票/板块抓取器"""
    
    # API 基础地址
    BASE_URL = "https://dq.10jqka.com.cn"
    
    # API 接口
    API_ENDPOINTS = {
        # 热股榜单 API
        'hot_stocks': 'https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock',
        # 热门板块 API
        'hot_sectors': 'https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/plate',
    }
    
    # 默认请求头
    DEFAULT_HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Origin': 'https://eq.10jqka.com.cn',
        'Referer': 'https://eq.10jqka.com.cn/frontend/thsTopRank/index.html',
        'Connection': 'keep-alive',
    }
    
    def __init__(self, timeout: int = 30, retry_count: int = 3, retry_delay: float = 1.0):
        """
        初始化抓取器
        
        Args:
            timeout: 请求超时时间(秒)
            retry_count: 重试次数
            retry_delay: 重试延迟(秒)
        """
        self.timeout = timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)
    
    def _make_request(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        """
        发送HTTP请求并处理重试逻辑
        
        Args:
            url: 请求URL
            params: 请求参数
            
        Returns:
            JSON响应数据，失败返回None
        """
        last_error = None
        
        for attempt in range(self.retry_count):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                
                # 解析JSON响应
                data = response.json()
                
                # 检查响应状态
                if data.get('status_code') == 0:
                    return data
                else:
                    error_msg = data.get('status_msg') or 'Unknown error'
                    logger.warning(f"API返回错误: {error_msg}")
                    return data  # 仍然返回数据，让调用者决定如何处理
                    
            except requests.exceptions.RequestException as e:
                last_error = e
                logger.warning(f"请求失败 (尝试 {attempt + 1}/{self.retry_count}): {e}")
                
                if attempt < self.retry_count - 1:
                    delay = self.retry_delay * (1 + random.random())
                    time.sleep(delay)
        
        logger.error(f"请求最终失败: {last_error}")
        return None
    
    def get_hot_stocks_1h(self, limit: int = 100) -> list[HotStock]:
        """
        获取1小时热股榜
        
        Args:
            limit: 返回数量限制
            
        Returns:
            热股列表
        """
        params = {
            'stock_type': 'a',
            'type': 'hour',
            'list_type': 'normal',
        }
        
        data = self._make_request(self.API_ENDPOINTS['hot_stocks'], params)
        return self._parse_hot_stocks(data, limit)
    
    def get_hot_stocks_24h(self, limit: int = 100) -> list[HotStock]:
        """
        获取24小时热股榜
        
        Args:
            limit: 返回数量限制
            
        Returns:
            热股列表
        """
        params = {
            'stock_type': 'a',
            'type': 'day',
            'list_type': 'normal',
        }
        
        data = self._make_request(self.API_ENDPOINTS['hot_stocks'], params)
        return self._parse_hot_stocks(data, limit)
    
    def _parse_hot_stocks(self, data: Optional[dict], limit: int) -> list[HotStock]:
        """
        解析热股数据
        
        Args:
            data: API响应数据
            limit: 返回数量限制
            
        Returns:
            热股列表
        """
        stocks = []
        
        if not data:
            return stocks
        
        try:
            # 从data字段中提取股票列表
            stock_list = None
            
            if 'data' in data:
                if isinstance(data['data'], dict) and 'stock_list' in data['data']:
                    stock_list = data['data']['stock_list']
                elif isinstance(data['data'], list):
                    stock_list = data['data']
            
            if not stock_list:
                logger.warning(f"无法解析热股数据: {json.dumps(data, ensure_ascii=False)[:200]}")
                return stocks
            
            fetch_time = datetime.now()
            
            for idx, item in enumerate(stock_list[:limit], 1):
                try:
                    # 解析标签
                    tag_info = item.get('tag', {})
                    concept_tags = []
                    popularity_tag = ""
                    if isinstance(tag_info, dict):
                        concept_tags = tag_info.get('concept_tag', []) or []
                        popularity_tag = tag_info.get('popularity_tag', '') or ''
                    
                    # 解析话题信息
                    topic = None
                    topic_data = item.get('topic')
                    if topic_data and isinstance(topic_data, dict):
                        topic = TopicInfo(
                            topic_code=topic_data.get('topic_code', ''),
                            title=topic_data.get('title', ''),
                            ios_jump_url=topic_data.get('ios_jump_url', ''),
                            android_jump_url=topic_data.get('android_jump_url', '')
                        )
                    
                    # 解析热度值
                    hot_value = item.get('rate', 0)
                    if isinstance(hot_value, str):
                        hot_value = int(float(hot_value))
                    else:
                        hot_value = int(hot_value)
                    
                    # 处理 rise_and_fall 可能为 None 的情况
                    rise_and_fall = item.get('rise_and_fall')
                    if rise_and_fall is None:
                        change_percent = 0.0
                    else:
                        change_percent = float(rise_and_fall)
                    
                    stock = HotStock(
                        rank=item.get('order', idx),
                        code=str(item.get('code', '')),
                        name=item.get('name', ''),
                        market=item.get('market', 0),
                        hot_value=hot_value,
                        change_percent=change_percent,
                        hot_rank_change=item.get('hot_rank_chg'),
                        concept_tags=concept_tags,
                        popularity_tag=popularity_tag,
                        analyse=item.get('analyse', ''),
                        analyse_title=item.get('analyse_title', ''),
                        topic=topic,
                        fetch_time=fetch_time
                    )
                    stocks.append(stock)
                except (ValueError, TypeError) as e:
                    logger.warning(f"解析股票数据失败: {e}, 数据: {item}")
                    continue
                    
        except Exception as e:
            logger.exception(f"解析热股数据异常: {e}")
        
        return stocks
    
    def get_hot_industry_sectors(self, limit: int = 50) -> list[HotSector]:
        """
        获取热门行业板块
        
        Args:
            limit: 返回数量限制
            
        Returns:
            行业板块列表
        """
        params = {
            'type': 'industry',
        }
        
        data = self._make_request(self.API_ENDPOINTS['hot_sectors'], params)
        return self._parse_hot_sectors(data, 'industry', limit)
    
    def get_hot_concept_sectors(self, limit: int = 50) -> list[HotSector]:
        """
        获取热门概念板块
        
        Args:
            limit: 返回数量限制
            
        Returns:
            概念板块列表
        """
        params = {
            'type': 'concept',
        }
        
        data = self._make_request(self.API_ENDPOINTS['hot_sectors'], params)
        return self._parse_hot_sectors(data, 'concept', limit)
    
    def _parse_hot_sectors(self, data: Optional[dict], sector_type: str, limit: int) -> list[HotSector]:
        """
        解析热门板块数据
        
        Args:
            data: API响应数据
            sector_type: 板块类型 ('industry' 或 'concept')
            limit: 返回数量限制
            
        Returns:
            板块列表
        """
        sectors = []
        
        if not data:
            return sectors
        
        try:
            # 从data字段中提取板块列表
            sector_list = None
            
            if 'data' in data:
                if isinstance(data['data'], dict) and 'plate_list' in data['data']:
                    sector_list = data['data']['plate_list']
                elif isinstance(data['data'], list):
                    sector_list = data['data']
            
            if not sector_list:
                logger.warning(f"无法解析板块数据: {json.dumps(data, ensure_ascii=False)[:200]}")
                return sectors
            
            fetch_time = datetime.now()
            
            for idx, item in enumerate(sector_list[:limit], 1):
                try:
                    # 解析热度值
                    hot_value = item.get('rate', 0)
                    if isinstance(hot_value, str):
                        hot_value = int(float(hot_value))
                    else:
                        hot_value = int(hot_value)
                    
                    # 解析ETF信息
                    etf_info = None
                    if item.get('etf_product_id'):
                        # 处理 etf_rise_and_fall 可能为 None 的情况
                        etf_rise_and_fall = item.get('etf_rise_and_fall')
                        etf_change = float(etf_rise_and_fall) if etf_rise_and_fall is not None else 0.0
                        etf_info = ETFInfo(
                            product_id=str(item.get('etf_product_id', '')),
                            name=item.get('etf_name', ''),
                            rise_and_fall=etf_change,
                            market_id=item.get('etf_market_id', 0)
                        )
                    
                    # 处理 rise_and_fall 可能为 None 的情况
                    rise_and_fall = item.get('rise_and_fall')
                    change_percent = float(rise_and_fall) if rise_and_fall is not None else 0.0
                    
                    sector = HotSector(
                        rank=item.get('order', idx),
                        code=str(item.get('code', '')),
                        name=item.get('name', ''),
                        market_id=item.get('market_id', 0),
                        hot_value=hot_value,
                        change_percent=change_percent,
                        sector_type=sector_type,
                        hot_rank_change=item.get('hot_rank_chg'),
                        hot_tag=item.get('hot_tag', ''),
                        rise_stop_info=item.get('tag', ''),  # 涨停信息，如"18家涨停"
                        etf_info=etf_info,
                        fetch_time=fetch_time
                    )
                    sectors.append(sector)
                except (ValueError, TypeError) as e:
                    logger.warning(f"解析板块数据失败: {e}, 数据: {item}")
                    continue
                    
        except Exception as e:
            logger.exception(f"解析板块数据异常: {e}")
        
        return sectors
    
    def get_all_hot_data(self, include_market_analysis: bool = False) -> HotSpotData:
        """
        获取所有热点数据
        
        Args:
            include_market_analysis: 是否包含盘面分析数据(需要 Playwright)
        
        Returns:
            热点数据汇总对象
        """
        logger.info("开始抓取同花顺热点数据...")
        
        hot_data = HotSpotData()
        
        # 获取1小时热股
        logger.info("  - 获取1小时热股...")
        hot_data.hot_stocks_1h = self.get_hot_stocks_1h()
        time.sleep(0.5)  # 避免请求过快
        
        # 获取24小时热股
        logger.info("  - 获取24小时热股...")
        hot_data.hot_stocks_24h = self.get_hot_stocks_24h()
        time.sleep(0.5)
        
        # 获取热门行业板块
        logger.info("  - 获取热门行业板块...")
        hot_data.hot_industry_sectors = self.get_hot_industry_sectors()
        time.sleep(0.5)
        
        # 获取热门概念板块
        logger.info("  - 获取热门概念板块...")
        hot_data.hot_concept_sectors = self.get_hot_concept_sectors()
        
        # 获取盘面分析数据（如果需要）
        if include_market_analysis:
            logger.info("  - 获取盘面分析数据...")
            try:
                hot_data.market_analysis = self.get_market_analysis()
            except Exception as e:
                logger.exception(f"获取盘面分析数据失败: {e}")
        
        hot_data.fetch_time = datetime.now()
        
        logger.info("抓取完成!")
        logger.info(hot_data.summary())
        
        return hot_data
    
    def get_market_analysis(self, headless: bool = True) -> Optional[MarketAnalysis]:
        """
        获取盘面分析数据
        
        Args:
            headless: 是否使用无头模式运行浏览器
            
        Returns:
            盘面分析数据对象
        """
        try:
            from .market_analysis_scraper import get_market_analysis
            return get_market_analysis(headless=headless)
        except ImportError as e:
            logger.error(f"导入盘面分析模块失败，请确保已安装 playwright: {e}")
            logger.info("可以通过 'pip install playwright && playwright install chromium' 安装")
            return None
        except Exception as e:
            logger.exception(f"获取盘面分析数据失败: {e}")
            return None
    
    def close(self):
        """关闭会话"""
        self.session.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
