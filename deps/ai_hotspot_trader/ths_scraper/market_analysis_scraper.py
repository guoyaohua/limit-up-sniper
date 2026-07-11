"""
同花顺盘面分析数据抓取器
使用 Playwright 抓取动态渲染的页面数据
结合 API 获取准确数据
涨跌家数、涨跌停、涨跌分布等数据通过 xtquant tick 数据计算
"""

import asyncio
import os
import re
import json
import aiohttp
from datetime import datetime
from typing import Optional, Dict, List
from playwright.async_api import async_playwright, Page, Browser
from .models import MarketAnalysis
from .market_stats_calculator import calculate_market_stats, XTQUANT_AVAILABLE

# 使用 loguru 进行日志管理
from logger_config import logger

class MarketAnalysisScraper:
    """盘面分析数据抓取器"""
    
    # 盘面分析页面 URL
    MARKET_ANALYSIS_URL = os.getenv(
        'THS_MARKET_ANALYSIS_URL',
        'https://eq.10jqka.com.cn/webpage/kamis-renderer/index.html',
    )
    token = os.getenv('THS_MARKET_ANALYSIS_TOKEN', '')
    if token:
        MARKET_ANALYSIS_URL = f'{MARKET_ANALYSIS_URL}?token={token}'
    
    # API URLs
    TURNOVER_API_URL = "https://dq.10jqka.com.cn/fuyao/market_analysis_api/chart/v1/get_chart_data?chart_key=turnover_minute"
    LIMIT_UP_DAY_API_URL = "https://dq.10jqka.com.cn/fuyao/market_analysis_api/chart/v1/get_chart_data?chart_key=limit_up_day"
    MARKET_SCORE_API_URL = "https://dq.10jqka.com.cn/fuyao/market_analysis_api/score/v1/get_market_score"
    
    def __init__(self, headless: bool = True, timeout: int = 30000):
        """
        初始化抓取器
        
        Args:
            headless: 是否使用无头模式
            timeout: 页面加载超时时间(毫秒)
        """
        self.headless = headless
        self.timeout = timeout
        self.browser: Optional[Browser] = None
        self._playwright = None
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(headless=self.headless)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
    
    async def get_market_analysis(self) -> MarketAnalysis:
        """
        获取盘面分析数据
        
        Returns:
            MarketAnalysis 对象
        """
        if not self.browser:
            raise RuntimeError("Browser not initialized. Use 'async with' context manager.")
        
        # 使用移动端设置，因为这是一个移动端页面
        context = await self.browser.new_context(
            viewport={'width': 375, 'height': 812},
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
            is_mobile=True,
            has_touch=True,
        )
        
        page = await context.new_page()
        
        try:
            # 存储拦截到的 API 响应数据
            api_responses = {}
            
            # 监听网络请求，捕获 API 数据
            async def handle_response(response):
                url = response.url
                if 'api' in url or 'data' in url:
                    try:
                        content_type = response.headers.get('content-type', '')
                        if 'json' in content_type or 'javascript' in content_type:
                            text = await response.text()
                            api_responses[url] = text
                    except:
                        pass
            
            page.on('response', handle_response)
            
            # 加载页面
            await page.goto(self.MARKET_ANALYSIS_URL, timeout=self.timeout, wait_until='networkidle')
            
            # 等待页面内容加载
            await page.wait_for_timeout(5000)
            
            # 尝试等待特定元素出现
            try:
                await page.wait_for_selector('text=上涨', timeout=10000)
            except:
                pass
            
            # 滚动页面以加载更多内容
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight / 3)')
            await page.wait_for_timeout(1000)
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight * 2 / 3)')
            await page.wait_for_timeout(1000)
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await page.wait_for_timeout(2000)
            
            # 滚动回顶部
            await page.evaluate('window.scrollTo(0, 0)')
            await page.wait_for_timeout(1000)
            
            # 解析页面数据
            analysis = await self._parse_page_data(page)
            
            # 尝试从 API 响应中补充数据
            analysis = self._parse_api_responses(analysis, api_responses)
            
            # 使用API获取准确的成交额数据（昨日同期）
            try:
                turnover_data = await self.fetch_turnover_data()
                if turnover_data['today_volume'] > 0:
                    analysis.today_volume = turnover_data['today_volume']
                if turnover_data['yesterday_volume'] > 0:
                    analysis.yesterday_volume = turnover_data['yesterday_volume']
                    # logger.debug(f"yesterday_volume from API: {analysis.yesterday_volume}")
            except Exception as e:
                logger.exception(f"获取成交额API数据失败: {e}")
            
            # 使用API获取准确的涨跌数据
            try:
                limit_data = await self.fetch_limit_up_data()
                if limit_data['rise_count'] > 0:
                    analysis.rise_count = limit_data['rise_count']
                if limit_data['fall_count'] > 0:
                    analysis.fall_count = limit_data['fall_count']
                if limit_data['limit_up_count'] > 0:
                    analysis.limit_up_count = limit_data['limit_up_count']
                if limit_data['limit_down_count'] >= 0:
                    analysis.limit_down_count = limit_data['limit_down_count']
            except Exception as e:
                logger.exception(f"获取涨跌API数据失败: {e}")
            
            # 使用API获取准确的市场评分
            try:
                score_data = await self.fetch_market_score()
                if score_data['market_score'] > 0:
                    analysis.market_score = score_data['market_score']
                if score_data['market_comment']:
                    analysis.market_comment = score_data['market_comment']
            except Exception as e:
                logger.exception(f"获取市场评分API数据失败: {e}")
            
            # 使用 xtquant tick 数据计算涨跌家数、涨跌停、涨跌分布
            # 这是最准确的数据来源，会覆盖之前从页面或API获取的数据
            if XTQUANT_AVAILABLE:
                try:
                    logger.info("使用 xtquant tick 数据计算市场统计...")
                    market_stats = calculate_market_stats()
                    
                    if market_stats.valid_stocks > 0:
                        # 覆盖涨跌家数
                        analysis.rise_count = market_stats.rise_count
                        analysis.fall_count = market_stats.fall_count
                        analysis.flat_count = market_stats.flat_count
                        
                        # 覆盖涨跌停数量
                        analysis.limit_up_count = market_stats.limit_up_count
                        analysis.limit_down_count = market_stats.limit_down_count
                        
                        # 覆盖涨跌分布
                        analysis.rise_distribution = market_stats.rise_distribution
                        analysis.fall_distribution = market_stats.fall_distribution
                        
                        logger.info(f"xtquant 统计完成: 涨{analysis.rise_count} 跌{analysis.fall_count} "
                                   f"涨停{analysis.limit_up_count} 跌停{analysis.limit_down_count}")
                    else:
                        logger.warning("xtquant 统计数据无效，使用API/页面数据")
                except Exception as e:
                    logger.exception(f"使用 xtquant 计算市场统计失败: {e}")
            else:
                logger.info("xtquant 不可用，使用API/页面数据获取涨跌分布")
            
            return analysis
            
        finally:
            await context.close()
    
    async def _parse_page_data(self, page: Page) -> MarketAnalysis:
        """
        从页面 DOM 中解析数据
        
        Args:
            page: Playwright 页面对象
            
        Returns:
            MarketAnalysis 对象
        """
        analysis = MarketAnalysis()
        analysis.fetch_time = datetime.now()
        
        try:
            # 获取整个页面的文本内容用于解析
            page_text = await page.evaluate('document.body.innerText')
            
            # 解析当日综合评分 - "9.0\n\n当日综合评分"
            score_match = re.search(r'(\d+(?:\.\d+)?)\s*\n+\s*当日综合评分', page_text)
            if score_match:
                analysis.market_score = int(float(score_match.group(1)))
            
            # 解析当日点评
            comment_match = re.search(r'当日点评\s*\n+\s*([^\n]+)', page_text)
            if comment_match:
                analysis.market_comment = comment_match.group(1).strip()
            
            # 解析涨跌家数 - "涨 3920 家" "跌 1349 家"
            rise_match = re.search(r'涨\s*(\d+)\s*家', page_text)
            if rise_match:
                analysis.rise_count = int(rise_match.group(1))
            
            fall_match = re.search(r'跌\s*(\d+)\s*家', page_text)
            if fall_match:
                analysis.fall_count = int(fall_match.group(1))
            
            # 解析涨跌停 - "涨停 109 家" "跌停 1 家"
            limit_up_match = re.search(r'涨停\s*(\d+)\s*家', page_text)
            if limit_up_match:
                analysis.limit_up_count = int(limit_up_match.group(1))
            
            limit_down_match = re.search(r'跌停\s*(\d+)\s*家', page_text)
            if limit_down_match:
                analysis.limit_down_count = int(limit_down_match.group(1))
            
            # 解析暗盘资金 - "净流入 3807 家" "净流出 1189 家"
            inflow_match = re.search(r'净流入\s*(\d+)\s*家', page_text)
            outflow_match = re.search(r'净流出\s*(\d+)\s*家', page_text)
            if inflow_match and outflow_match:
                analysis.inflow_sectors = [{'name': '暗盘资金净流入', 'count': int(inflow_match.group(1))}]
                analysis.outflow_sectors = [{'name': '暗盘资金净流出', 'count': int(outflow_match.group(1))}]
            
            # 解析市场成交额
            # 优先提取"今日: xxx亿 昨日: xxx亿"格式（图表区域显示的当前时刻数据）
            # 格式可能是: "今日：9870亿 昨日：10000亿" 或 "今日:9870亿 昨日:10000亿"
            current_volume_match = re.search(r'今日[：:]\s*(\d+(?:\.\d+)?)\s*亿\s*昨日[：:]\s*(\d+(?:\.\d+)?)\s*亿', page_text)
            if current_volume_match:
                analysis.today_volume = float(current_volume_match.group(1))
                analysis.yesterday_volume = float(current_volume_match.group(2))
            else:
                # 备用：提取总成交额和昨日总成交额
                today_volume_match = re.search(r'总成交额\s*\n?\s*(\d+(?:\.\d+)?)\s*亿', page_text)
                if today_volume_match:
                    analysis.today_volume = float(today_volume_match.group(1))
                
                yesterday_volume_match = re.search(r'昨日总成交额\s*\n?\s*(\d+(?:\.\d+)?)\s*亿', page_text)
                if yesterday_volume_match:
                    analysis.yesterday_volume = float(yesterday_volume_match.group(1))
            
            # 注意：涨跌分布数据通过 xtquant tick 数据计算获取（更准确）
            # 页面正则解析不可靠，已移除相关代码
            # 如果 xtquant 不可用，涨跌分布将保持为空字典
            
        except Exception as e:
            logger.exception(f"解析页面数据时发生错误: {e}")
        
        return analysis
    
    async def _parse_distribution(self, page: Page, dist_type: str) -> Dict[str, int]:
        """
        解析涨跌分布数据
        
        Args:
            page: Playwright 页面对象
            dist_type: 'rise' 或 'fall'
            
        Returns:
            涨跌分布字典
        """
        distribution = {}
        
        try:
            if dist_type == 'rise':
                ranges = ['>9%', '7-9%', '5-7%', '3-5%', '0-3%']
            else:
                ranges = ['<-9%', '-9--7%', '-7--5%', '-5--3%', '-3-0%']
            
            page_text = await page.evaluate('document.body.innerText')
            
            for range_str in ranges:
                pattern = re.escape(range_str.replace('%', '')) + r'[^\d]*(\d+)'
                match = re.search(pattern, page_text)
                if match:
                    distribution[range_str] = int(match.group(1))
                    
        except Exception:
            pass
        
        return distribution
    
    async def _parse_fund_flow(self, page: Page, flow_type: str) -> List[Dict]:
        """
        解析资金流向数据
        
        Args:
            page: Playwright 页面对象
            flow_type: 'inflow' 或 'outflow'
            
        Returns:
            资金流向列表
        """
        sectors = []
        
        try:
            page_text = await page.evaluate('document.body.innerText')
            
            if flow_type == 'inflow':
                section_match = re.search(r'流入前三(.+?)(?:流出|$)', page_text, re.DOTALL)
            else:
                section_match = re.search(r'流出前三(.+?)(?:连板|$)', page_text, re.DOTALL)
            
            if section_match:
                section_text = section_match.group(1)
                items = re.findall(r'([^\d\n]+?)\s*([+-]?\d+(?:\.\d+)?)\s*亿?\s*([+-]?\d+(?:\.\d+)?)\s*%?', section_text)
                for item in items[:3]:
                    sectors.append({
                        'name': item[0].strip(),
                        'amount': float(item[1]),
                        'change': float(item[2])
                    })
                    
        except Exception:
            pass
        
        return sectors
    
    def _parse_api_responses(self, analysis: MarketAnalysis, api_responses: Dict[str, str]) -> MarketAnalysis:
        """
        从拦截到的 API 响应中补充数据
        
        Args:
            analysis: 当前的 MarketAnalysis 对象
            api_responses: API 响应字典
            
        Returns:
            更新后的 MarketAnalysis 对象
        """
        for url, response_text in api_responses.items():
            try:
                if response_text.startswith('{') or response_text.startswith('['):
                    data = json.loads(response_text)
                elif '(' in response_text and response_text.endswith(')'):
                    json_str = response_text[response_text.find('(')+1:response_text.rfind(')')]
                    data = json.loads(json_str)
                else:
                    continue
                
                if isinstance(data, dict):
                    # 检查成交额数据
                    if 'volume' in data or 'amount' in data:
                        if 'today' in data:
                            analysis.today_volume = float(data.get('today', 0))
                        if 'yesterday' in data:
                            analysis.yesterday_volume = float(data.get('yesterday', 0))
                            
            except (json.JSONDecodeError, ValueError):
                continue
        
        return analysis
    
    @staticmethod
    async def _fetch_api_data(url: str) -> Optional[Dict]:
        """
        通过HTTP请求获取API数据
        
        Args:
            url: API URL
            
        Returns:
            API响应数据字典，失败返回None
        """
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15'
                }
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        return await response.json()
        except Exception as e:
            logger.exception(f"获取API数据失败 {url}: {e}")
        return None
    
    @classmethod
    async def fetch_turnover_data(cls) -> Dict:
        """
        获取市场成交额分时数据
        
        Returns:
            包含今日成交额、昨日同期成交额的字典
        """
        result = {
            'today_volume': 0.0,
            'yesterday_volume': 0.0,
            'volume_change': 0.0
        }
        
        data = await cls._fetch_api_data(cls.TURNOVER_API_URL)
        if data and data.get('status_code') == 0:
            charts = data.get('data', {}).get('charts', {})
            point_list = charts.get('point_list', [])
            
            # 从 point_list 最后一个数据点获取当前时刻的同期数据
            # 格式: [timestamp, turnover, turnover_pre, turnover_change]
            if point_list:
                latest_point = []
                for i in range(len(point_list), 0, -1):
                    if len(point_list[i-1]) >= 4 and point_list[i-1][1] is not None and point_list[i-1][2] is not None and point_list[i-1][3] is not None:
                        latest_point = point_list[i-1]
                        break
                # logger.debug(f"Latest turnover point data: {latest_point}")
                if len(latest_point) >= 4:
                    # 转换为亿元
                    result['today_volume'] = round(latest_point[1] / 100000000, 2)
                    result['yesterday_volume'] = round(latest_point[2] / 100000000, 2)  # 昨日同期
                    # logger.debug(f"yesterday_volume from API: {result['yesterday_volume']}")
                    result['volume_change'] = round(latest_point[3] / 100000000, 2)
        
        return result
    
    @classmethod
    async def fetch_limit_up_data(cls) -> Dict:
        """
        获取涨跌趋势数据（涨跌家数、涨跌停）
        
        Returns:
            包含涨跌家数、涨跌停数据的字典
        """
        result = {
            'rise_count': 0,
            'fall_count': 0,
            'limit_up_count': 0,
            'limit_down_count': 0
        }
        
        data = await cls._fetch_api_data(cls.LIMIT_UP_DAY_API_URL)
        if data and data.get('status_code') == 0:
            charts = data.get('data', {}).get('charts', {})
            header = charts.get('header', [])
            
            for item in header:
                key = item.get('key')
                val = item.get('val', 0)
                
                if key == 'rise':
                    result['rise_count'] = int(val)
                elif key == 'fall':
                    result['fall_count'] = int(val)
                elif key == 'limit_up':
                    result['limit_up_count'] = int(val)
                elif key == 'limit_down':
                    result['limit_down_count'] = int(val)
        
        return result
    
    @classmethod
    async def fetch_market_score(cls) -> Dict:
        """
        获取市场评分数据
        
        Returns:
            包含市场评分、点评的字典
        """
        result = {
            'market_score': 0,
            'market_comment': ''
        }
        
        data = await cls._fetch_api_data(cls.MARKET_SCORE_API_URL)
        if data and data.get('status_code') == 0:
            score_data = data.get('data', {})
            result['market_score'] = float(score_data.get('sum_socre', 0))
            result['market_comment'] = score_data.get('score_content', '')
        
        return result

async def get_market_analysis_async(headless: bool = True) -> MarketAnalysis:
    """
    异步获取盘面分析数据的便捷函数
    
    Args:
        headless: 是否使用无头模式
        
    Returns:
        MarketAnalysis 对象
    """
    async with MarketAnalysisScraper(headless=headless) as scraper:
        return await scraper.get_market_analysis()

def get_market_analysis(headless: bool = True) -> MarketAnalysis:
    """
    同步获取盘面分析数据的便捷函数
    
    Args:
        headless: 是否使用无头模式
        
    Returns:
        MarketAnalysis 对象
    """
    return asyncio.run(get_market_analysis_async(headless=headless))

if __name__ == '__main__':
    # 测试代码
    async def main():
        async with MarketAnalysisScraper(headless=False) as scraper:
            analysis = await scraper.get_market_analysis()
            
            logger.info("="*50)
            logger.info(analysis.summary())
            logger.info("="*50)
            
            logger.info("\n详细数据:")
            logger.info(json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2))
    
    asyncio.run(main())
