"""
东方财富网涨停板行情爬虫
抓取涨停股池、炸板股池、跌停股池数据
支持选择特定日期的历史数据

支持两种方式：
1. API 方式（推荐）：直接调用东方财富 API 获取数据，更稳定
2. 网页方式：通过浏览器抓取页面数据
"""

import asyncio
import os
import re
import json
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from playwright.async_api import async_playwright, Page, Browser
from dataclasses import dataclass, field

# 尝试导入项目的 logger，如果失败则使用基本的 logging
try:
    from logger_config import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

@dataclass
class StockInfo:
    """股票信息数据类"""
    code: str = ""              # 股票代码
    name: str = ""              # 股票名称
    price: str = ""             # 最新价（保留原始字符串）
    change_pct: str = ""        # 涨跌幅（保留原始字符串）
    turnover_rate: str = ""     # 换手率（保留原始字符串）
    change_speed: str = ""      # 涨速（保留原始字符串）
    amplitude: str = ""         # 振幅（保留原始字符串）
    amount: str = ""            # 成交额（保留原始字符串）
    circulating_value: str = "" # 流通市值（保留原始字符串）
    total_value: str = ""       # 总市值（保留原始字符串）
    pe_ratio: str = ""          # 动态市盈率（保留原始字符串）
    seal_amount: str = ""       # 封板资金/封单资金（保留原始字符串）
    limit_up_price: str = ""    # 涨停价（炸板股池）
    limit_up_time: str = ""     # 首次封板时间
    last_limit_time: str = ""   # 最后封板时间
    limit_up_stats: str = ""    # 涨停统计
    industry: str = ""          # 所属行业
    continuous_days: str = ""   # 连板数
    open_count: str = ""        # 开板次数/炸板次数
    continuous_limit_down: str = ""  # 连续跌停（跌停股池）
    board_amount: str = ""      # 板上成交额（跌停股池）
    pool_type: str = ""         # 股池类型: ztgc(涨停), zbgc(炸板), dtgc(跌停)

class EastMoneyZTBScraper:
    """东方财富网涨停板行情抓取器"""
    
    # 股池类型配置
    POOL_CONFIGS = {
        'ztgc': {
            'name': '涨停股池',
            'url': 'https://quote.eastmoney.com/ztb/detail#type=ztgc',
            'filename': 'limit_up_stocks'
        },
        'zbgc': {
            'name': '炸板股池',
            'url': 'https://quote.eastmoney.com/ztb/detail#type=zbgc',
            'filename': 'broken_limit_stocks'
        },
        'dtgc': {
            'name': '跌停股池',
            'url': 'https://quote.eastmoney.com/ztb/detail#type=dtgc',
            'filename': 'limit_down_stocks'
        }
    }
    
    def __init__(self, headless: bool = True, timeout: int = 60000, data_dir: str = None, target_date: str = None):
        """
        初始化抓取器
        
        Args:
            headless: 是否使用无头模式
            timeout: 页面加载超时时间(毫秒)
            data_dir: 数据保存目录，默认为 output/eastmoney_ztb/{日期}
            target_date: 目标日期，格式为 YYYY-MM-DD 或 YYYYMMDD，默认为当天
        """
        self.headless = headless
        self.timeout = timeout
        self.target_date = self._parse_date(target_date) if target_date else None
        self.page_date = None  # 页面实际显示的日期，会在抓取时更新
        
        # 数据目录会在抓取后根据页面日期确定
        self._base_data_dir = data_dir
        self._data_dir = None
            
        self.browser: Optional[Browser] = None
        self._playwright = None
    
    def _parse_date(self, date_str: str) -> str:
        """
        解析日期字符串，统一转换为 YYYY-MM-DD 格式
        
        Args:
            date_str: 日期字符串，支持 YYYY-MM-DD 或 YYYYMMDD 格式
            
        Returns:
            YYYY-MM-DD 格式的日期字符串
        """
        if not date_str:
            return None
        
        # 移除可能的空格
        date_str = date_str.strip()
        
        # 尝试解析 YYYYMMDD 格式
        if len(date_str) == 8 and date_str.isdigit():
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        
        # 尝试解析 YYYY-MM-DD 格式
        if len(date_str) == 10 and date_str[4] == '-' and date_str[7] == '-':
            return date_str
        
        # 尝试使用 datetime 解析其他格式
        for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d']:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime('%Y-%m-%d')
            except ValueError:
                continue
        
        logger.warning(f"无法解析日期格式: {date_str}，将使用当天日期")
        return None
    
    @property
    def data_dir(self) -> str:
        """获取数据保存目录"""
        if self._data_dir:
            return self._data_dir
        
        # 如果已经获取到页面日期，使用页面日期
        if self.page_date:
            date_str = self.page_date.replace('-', '')
        elif self.target_date:
            date_str = self.target_date.replace('-', '')
        else:
            date_str = datetime.now().strftime('%Y%m%d')
        
        if self._base_data_dir:
            return self._base_data_dir
        else:
            return os.path.join("output", "eastmoney_ztb", date_str)
    
    def _ensure_data_dir(self):
        """确保数据目录存在"""
        os.makedirs(self.data_dir, exist_ok=True)
    
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
    
    async def scrape_pool(self, pool_type: str) -> List[StockInfo]:
        """
        抓取指定股池的数据
        
        Args:
            pool_type: 股池类型 ('ztgc', 'zbgc', 'dtgc')
            
        Returns:
            股票信息列表
        """
        if pool_type not in self.POOL_CONFIGS:
            raise ValueError(f"未知的股池类型: {pool_type}")
        
        config = self.POOL_CONFIGS[pool_type]
        logger.info(f"开始抓取 {config['name']}...")
        
        if not self.browser:
            raise RuntimeError("Browser not initialized. Use 'async with' context manager.")
        
        context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        page = await context.new_page()
        stocks = []
        
        try:
            # 加载页面
            logger.info(f"  访问页面: {config['url']}")
            await page.goto(config['url'], timeout=self.timeout, wait_until='networkidle')
            
            # 等待页面内容加载
            await page.wait_for_timeout(3000)
            
            # 如果指定了目标日期，选择该日期
            if self.target_date:
                await self._select_date(page, self.target_date)
            
            # 获取页面当前显示的日期
            self.page_date = await self._get_page_date(page)
            if self.page_date:
                logger.info(f"  页面日期: {self.page_date}")
            
            # 等待表格出现
            try:
                await page.wait_for_selector('table', timeout=10000)
                logger.info("  表格已加载")
            except Exception as e:
                logger.warning(f"  等待表格超时: {e}")
            
            # 点击"加载更多"直到没有更多数据
            await self._load_all_data(page)
            
            # 解析表格数据
            stocks = await self._parse_table_data(page, pool_type)
            
            logger.info(f"  {config['name']} 抓取完成，共 {len(stocks)} 条数据")
            
        except Exception as e:
            logger.exception(f"抓取 {config['name']} 时发生错误: {e}")
        finally:
            await context.close()
        
        return stocks
    
    async def _get_page_date(self, page: Page) -> Optional[str]:
        """
        获取页面当前显示的日期
        
        Args:
            page: Playwright 页面对象
            
        Returns:
            日期字符串（YYYY-MM-DD 格式），如果获取失败返回 None
        """
        try:
            # 使用 JavaScript 获取日期输入框的值（readonly 输入框的值需要通过 JS 获取）
            date_value = await page.evaluate('''
                () => {
                    const input = document.querySelector('#beginDate');
                    return input ? input.value : null;
                }
            ''')
            
            if date_value:
                logger.info(f"  从页面获取到日期: {date_value}")
                return self._parse_date(date_value)
            
            # 备选方案：尝试从 time-box 内的 input 获取
            date_value = await page.evaluate('''
                () => {
                    const input = document.querySelector('.time-box input');
                    return input ? input.value : null;
                }
            ''')
            
            if date_value:
                logger.info(f"  从 time-box 获取到日期: {date_value}")
                return self._parse_date(date_value)
            
            logger.warning("  无法从页面获取日期")
            return None
            
        except Exception as e:
            logger.warning(f"  获取页面日期失败: {e}")
            return None
    
    async def _select_date(self, page: Page, target_date: str):
        """
        在页面上选择指定日期
        
        Args:
            page: Playwright 页面对象
            target_date: 目标日期（YYYY-MM-DD 格式）
        """
        try:
            logger.info(f"  选择日期: {target_date}")
            
            # 先关闭可能存在的广告
            await self._close_ads(page)
            
            # 点击日期输入框打开日期选择器
            date_input = await page.query_selector('#beginDate')
            if not date_input:
                date_input = await page.query_selector('.time-box input')
            
            if not date_input:
                logger.warning("  未找到日期输入框")
                return
            
            # 点击打开日期选择器
            await date_input.click()
            await page.wait_for_timeout(1000)
            
            # 解析目标日期
            target_dt = datetime.strptime(target_date, '%Y-%m-%d')
            target_year = target_dt.year
            target_month = target_dt.month
            target_day = target_dt.day
            
            # 等待日期选择器出现（layui-laydate 是东方财富使用的日期组件）
            try:
                await page.wait_for_selector('.layui-laydate', timeout=3000)
                logger.info("  日期选择器已打开")
            except:
                logger.warning("  未检测到日期选择器")
            
            # 导航到目标年月
            await self._navigate_to_month(page, target_year, target_month)
            
            # 点击目标日期
            try:
                # laydate 组件中日期单元格的选择器
                day_clicked = await page.evaluate(f'''
                    (params) => {{
                        const {{ year, month, day }} = params;
                        // 查找 laydate 日期表格中的日期单元格
                        const dateCells = document.querySelectorAll('.layui-laydate-content td');
                        for (const cell of dateCells) {{
                            const layYmd = cell.getAttribute('lay-ymd');
                            if (layYmd) {{
                                const [y, m, d] = layYmd.split('-').map(Number);
                                if (y === year && m === month && d === day) {{
                                    cell.click();
                                    return true;
                                }}
                            }}
                        }}
                        return false;
                    }}
                ''', {'year': target_year, 'month': target_month, 'day': target_day})
                
                if day_clicked:
                    logger.info(f"  已点击日期: {target_year}-{target_month}-{target_day}")
                else:
                    logger.warning(f"  未找到日期单元格: {target_year}-{target_month}-{target_day}")
                    
            except Exception as e:
                logger.warning(f"  点击日期失败: {e}")
            
            # 等待页面数据刷新
            await page.wait_for_timeout(3000)
            
            # 确保日期选择器已关闭
            try:
                # 检查日期选择器是否还存在
                laydate = await page.query_selector('.layui-laydate')
                if laydate:
                    # 点击页面空白处关闭日期选择器
                    await page.click('.ztb-wrap', position={'x': 10, 'y': 10}, force=True)
                    await page.wait_for_timeout(500)
                    
                    # 如果还没关闭，按 Escape
                    laydate = await page.query_selector('.layui-laydate')
                    if laydate:
                        await page.keyboard.press('Escape')
                        await page.wait_for_timeout(500)
            except:
                pass
            
            # 等待表格数据更新
            await page.wait_for_timeout(2000)
            
        except Exception as e:
            logger.warning(f"  选择日期失败: {e}")
    
    async def _navigate_to_month(self, page: Page, target_year: int, target_month: int):
        """
        在日期选择器中导航到目标年月
        
        Args:
            page: Playwright 页面对象
            target_year: 目标年份
            target_month: 目标月份
        """
        max_clicks = 24  # 最多点击24次（2年）
        clicks = 0
        
        while clicks < max_clicks:
            try:
                # 获取当前显示的年月（laydate 组件格式）
                current_ym = await page.evaluate('''
                    () => {
                        // laydate 组件的年月显示
                        const layHeader = document.querySelector('.layui-laydate-header');
                        if (layHeader) {
                            const setYm = layHeader.querySelector('.laydate-set-ym');
                            if (setYm) {
                                const spans = setYm.querySelectorAll('span');
                                if (spans.length >= 2) {
                                    // 第一个是年份，第二个是月份
                                    const yearText = spans[0].textContent.replace(/[^0-9]/g, '');
                                    const monthText = spans[1].textContent.replace(/[^0-9]/g, '');
                                    const year = parseInt(yearText);
                                    const month = parseInt(monthText);
                                    if (year && month) {
                                        return { year, month };
                                    }
                                }
                            }
                        }
                        return null;
                    }
                ''')
                
                if not current_ym:
                    logger.warning("  无法获取当前显示的年月")
                    break
                
                current_year = current_ym.get('year')
                current_month = current_ym.get('month')
                
                logger.info(f"  当前显示: {current_year}年{current_month}月, 目标: {target_year}年{target_month}月")
                
                if current_year == target_year and current_month == target_month:
                    logger.info("  已到达目标月份")
                    break
                
                # 计算需要前进还是后退
                current_total = current_year * 12 + current_month
                target_total = target_year * 12 + target_month
                
                if target_total < current_total:
                    # 需要往前（点击上一月）
                    prev_clicked = await page.evaluate('''
                        () => {
                            const prevBtn = document.querySelector('.layui-laydate-header .laydate-icon.laydate-prev-m');
                            if (prevBtn) {
                                prevBtn.click();
                                return true;
                            }
                            return false;
                        }
                    ''')
                    if prev_clicked:
                        await page.wait_for_timeout(300)
                    else:
                        logger.warning("  未找到上一月按钮")
                        break
                else:
                    # 需要往后（点击下一月）
                    next_clicked = await page.evaluate('''
                        () => {
                            const nextBtn = document.querySelector('.layui-laydate-header .laydate-icon.laydate-next-m');
                            if (nextBtn) {
                                nextBtn.click();
                                return true;
                            }
                            return false;
                        }
                    ''')
                    if next_clicked:
                        await page.wait_for_timeout(300)
                    else:
                        logger.warning("  未找到下一月按钮")
                        break
                
                clicks += 1
                
            except Exception as e:
                logger.warning(f"  导航到目标月份失败: {e}")
                break
        
        if clicks >= max_clicks:
            logger.warning(f"  导航月份达到最大点击次数 ({max_clicks})")
    
    async def _close_ads(self, page: Page):
        """
        关闭页面上的悬浮广告
        
        Args:
            page: Playwright 页面对象
        """
        try:
            # 使用 JavaScript 直接移除广告元素
            await page.evaluate('''
                () => {
                    // 移除图片广告 (wztctg 类)
                    document.querySelectorAll('.wztctg, img.wztctg').forEach(el => el.remove());
                    
                    // 移除常见的广告容器
                    const adSelectors = [
                        '[class*="ad-"]',
                        '[class*="banner"]',
                        '[id*="ad"]',
                        '.popup',
                        '.modal',
                        '.overlay',
                        '[style*="position: fixed"]',
                        '[style*="position:fixed"]',
                    ];
                    
                    adSelectors.forEach(selector => {
                        try {
                            document.querySelectorAll(selector).forEach(el => {
                                // 检查是否是悬浮广告（不移除表格等重要元素）
                                if (el.tagName !== 'TABLE' && el.tagName !== 'TBODY' && el.tagName !== 'TR' && el.tagName !== 'TD') {
                                    const style = window.getComputedStyle(el);
                                    if (style.position === 'fixed' || style.position === 'absolute') {
                                        el.style.display = 'none';
                                    }
                                }
                            });
                        } catch (e) {}
                    });
                    
                    // 移除可能遮挡的div
                    document.querySelectorAll('div').forEach(el => {
                        const style = window.getComputedStyle(el);
                        if (style.position === 'fixed' && style.zIndex > 100) {
                            el.style.display = 'none';
                        }
                    });
                }
            ''')
            logger.info("  已尝试移除页面广告")
            
            # 尝试按 Escape 键关闭弹窗
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(500)
            
        except Exception as e:
            logger.warning(f"  关闭广告时出错: {e}")
    
    async def _load_all_data(self, page: Page):
        """
        点击"加载更多"按钮直到没有更多数据
        
        Args:
            page: Playwright 页面对象
        """
        # 先尝试关闭可能存在的广告
        await self._close_ads(page)
        
        load_count = 0
        max_attempts = 50  # 最大尝试次数，防止无限循环
        retry_count = 0
        max_retries = 3  # 每次点击失败后的最大重试次数
        
        while load_count < max_attempts:
            try:
                # 每次循环都尝试移除广告
                await self._close_ads(page)
                
                # 查找"点击加载更多"按钮
                load_more_btn = await page.query_selector('text=点击加载更多')
                
                if load_more_btn:
                    # 检查按钮是否可见
                    is_visible = await load_more_btn.is_visible()
                    if is_visible:
                        logger.info(f"  点击加载更多... (第 {load_count + 1} 次)")
                        try:
                            # 使用 force=True 强制点击，忽略覆盖元素
                            await load_more_btn.click(force=True, timeout=5000)
                            await page.wait_for_timeout(1500)  # 等待数据加载
                            load_count += 1
                            retry_count = 0  # 重置重试计数
                            continue
                        except Exception as click_error:
                            logger.warning(f"  点击失败，尝试使用 JavaScript 点击: {click_error}")
                            # 尝试使用 JavaScript 点击
                            try:
                                await page.evaluate('''
                                    () => {
                                        const btn = document.evaluate(
                                            "//div[contains(text(), '点击加载更多')]",
                                            document,
                                            null,
                                            XPathResult.FIRST_ORDERED_NODE_TYPE,
                                            null
                                        ).singleNodeValue;
                                        if (btn) btn.click();
                                    }
                                ''')
                                await page.wait_for_timeout(1500)
                                load_count += 1
                                retry_count = 0
                                continue
                            except Exception as js_error:
                                logger.warning(f"  JavaScript 点击也失败: {js_error}")
                                retry_count += 1
                                if retry_count >= max_retries:
                                    logger.warning("  重试次数过多，停止加载")
                                    break
                                continue
                
                # 检查是否显示"无更多数据"
                no_more_data = await page.query_selector('text=无更多数据')
                if no_more_data:
                    is_visible = await no_more_data.is_visible()
                    if is_visible:
                        logger.info("  已加载全部数据（无更多数据）")
                        break
                
                # 如果既没有"加载更多"按钮也没有"无更多数据"提示，可能已经加载完毕
                # 再等待一下看是否有新内容
                await page.wait_for_timeout(1000)
                
                # 再次检查
                load_more_btn = await page.query_selector('text=点击加载更多')
                no_more_data = await page.query_selector('text=无更多数据')
                
                if not load_more_btn and not no_more_data:
                    logger.info("  数据加载完成")
                    break
                    
            except Exception as e:
                logger.warning(f"  加载更多数据时出错: {e}")
                retry_count += 1
                if retry_count >= max_retries:
                    break
                continue
        
        if load_count >= max_attempts:
            logger.warning(f"  达到最大加载次数 ({max_attempts})，停止加载")
    
    async def _parse_table_data(self, page: Page, pool_type: str) -> List[StockInfo]:
        """
        解析表格数据
        
        Args:
            page: Playwright 页面对象
            pool_type: 股池类型
            
        Returns:
            股票信息列表
        """
        stocks = []
        
        try:
            # 只获取数据表格中的行，排除日期选择器等其他表格
            # 使用更精确的选择器：.ztb-table 是涨停板数据表格的容器
            rows = await page.query_selector_all('.ztb-table table tbody tr, .dataview table tbody tr')
            
            # 如果上面的选择器没找到，尝试排除日期选择器的表格
            if not rows:
                rows = await page.query_selector_all('table:not(.layui-laydate-content) tbody tr')
            
            logger.info(f"  找到 {len(rows)} 行数据")
            
            for row in rows:
                try:
                    cells = await row.query_selector_all('td')
                    if len(cells) < 5:
                        continue
                    
                    stock = StockInfo()
                    stock.pool_type = pool_type
                    
                    # 根据不同股池类型解析不同的列
                    cell_texts = []
                    for cell in cells:
                        text = await cell.inner_text()
                        cell_texts.append(text.strip())
                    
                    # 解析通用字段
                    if len(cell_texts) >= 3:
                        # 序号通常在第一列，跳过
                        # 代码通常在第二列
                        stock.code = cell_texts[1] if len(cell_texts) > 1 else ""
                        # 名称通常在第三列
                        stock.name = cell_texts[2] if len(cell_texts) > 2 else ""
                    
                    # 验证股票代码格式（应该是6位数字）
                    if not stock.code or not self._is_valid_stock_code(stock.code):
                        continue
                    
                    # 根据股池类型解析特定字段
                    if pool_type == 'ztgc':
                        stock = self._parse_ztgc_row(cell_texts, stock)
                    elif pool_type == 'zbgc':
                        stock = self._parse_zbgc_row(cell_texts, stock)
                    elif pool_type == 'dtgc':
                        stock = self._parse_dtgc_row(cell_texts, stock)
                    
                    stocks.append(stock)
                        
                except Exception as e:
                    logger.warning(f"  解析行数据失败: {e}")
                    continue
                    
        except Exception as e:
            logger.exception(f"解析表格数据时发生错误: {e}")
        
        return stocks
    
    def _is_valid_stock_code(self, code: str) -> bool:
        """
        验证股票代码是否有效
        
        Args:
            code: 股票代码
            
        Returns:
            是否是有效的股票代码
        """
        if not code:
            return False
        
        # 移除可能的空格
        code = code.strip()
        
        # 股票代码应该是6位数字
        if len(code) == 6 and code.isdigit():
            return True
        
        # 有些可能带有市场后缀，如 000001.SZ
        if '.' in code:
            main_code = code.split('.')[0]
            if len(main_code) == 6 and main_code.isdigit():
                return True
        
        return False
    
    def _parse_ztgc_row(self, cells: List[str], stock: StockInfo) -> StockInfo:
        """
        解析涨停股池行数据
        用户提供的列顺序: 代码、名称、涨跌幅、最新价、成交额、流通市值、总市值、换手率、封板资金、首次封板时间、最后封板时间、炸板次数、涨停统计、连板数、所属行业
        实际表格索引:      1     2     3       4       5       6         7       8       9        10            11          12       13       14      15
        """
        try:
            # 直接保存原始字符串，避免解析错误
            if len(cells) > 3:
                stock.change_pct = cells[3]
            if len(cells) > 4:
                stock.price = cells[4]
            if len(cells) > 5:
                stock.amount = cells[5]
            if len(cells) > 6:
                stock.circulating_value = cells[6]
            if len(cells) > 7:
                stock.total_value = cells[7]
            if len(cells) > 8:
                stock.turnover_rate = cells[8]
            if len(cells) > 9:
                stock.seal_amount = cells[9]
            if len(cells) > 10:
                stock.limit_up_time = cells[10]
            if len(cells) > 11:
                stock.last_limit_time = cells[11]
            if len(cells) > 12:
                stock.open_count = cells[12]
            if len(cells) > 13:
                stock.limit_up_stats = cells[13]
            if len(cells) > 14:
                stock.continuous_days = cells[14]
            if len(cells) > 15:
                stock.industry = cells[15]
        except Exception as e:
            logger.warning(f"解析涨停股池行数据失败: {e}")
        return stock
    
    def _parse_zbgc_row(self, cells: List[str], stock: StockInfo) -> StockInfo:
        """
        解析炸板股池行数据
        用户提供的列顺序: 代码、名称、涨跌幅、最新价、涨停价、成交额、流通市值、总市值、换手率、涨速、首次封板时间、炸板次数、涨停统计、振幅、所属行业
        实际表格索引:      1     2     3       4       5       6       7         8       9      10      11          12       13       14    15
        """
        try:
            if len(cells) > 3:
                stock.change_pct = cells[3]
            if len(cells) > 4:
                stock.price = cells[4]
            if len(cells) > 5:
                stock.limit_up_price = cells[5]
            if len(cells) > 6:
                stock.amount = cells[6]
            if len(cells) > 7:
                stock.circulating_value = cells[7]
            if len(cells) > 8:
                stock.total_value = cells[8]
            if len(cells) > 9:
                stock.turnover_rate = cells[9]
            if len(cells) > 10:
                stock.change_speed = cells[10]
            if len(cells) > 11:
                stock.limit_up_time = cells[11]
            if len(cells) > 12:
                stock.open_count = cells[12]
            if len(cells) > 13:
                stock.limit_up_stats = cells[13]
            if len(cells) > 14:
                stock.amplitude = cells[14]
            if len(cells) > 15:
                stock.industry = cells[15]
        except Exception as e:
            logger.warning(f"解析炸板股池行数据失败: {e}")
        return stock
    
    def _parse_dtgc_row(self, cells: List[str], stock: StockInfo) -> StockInfo:
        """
        解析跌停股池行数据
        用户提供的列顺序: 代码、名称、涨跌幅、最新价、成交额、流通市值、总市值、动态市盈率、换手率、封单资金、最后封板时间、板上成交额、连续跌停、开板次数、所属行业
        实际表格索引:      1     2     3       4       5       6         7       8          9       10        11          12         13       14      15
        """
        try:
            if len(cells) > 3:
                stock.change_pct = cells[3]
            if len(cells) > 4:
                stock.price = cells[4]
            if len(cells) > 5:
                stock.amount = cells[5]
            if len(cells) > 6:
                stock.circulating_value = cells[6]
            if len(cells) > 7:
                stock.total_value = cells[7]
            if len(cells) > 8:
                stock.pe_ratio = cells[8]
            if len(cells) > 9:
                stock.turnover_rate = cells[9]
            if len(cells) > 10:
                stock.seal_amount = cells[10]
            if len(cells) > 11:
                stock.last_limit_time = cells[11]
            if len(cells) > 12:
                stock.board_amount = cells[12]
            if len(cells) > 13:
                stock.continuous_limit_down = cells[13]
            if len(cells) > 14:
                stock.open_count = cells[14]
            if len(cells) > 15:
                stock.industry = cells[15]
        except Exception as e:
            logger.warning(f"解析跌停股池行数据失败: {e}")
        return stock
    
    def save_to_tsv(self, stocks: List[StockInfo], pool_type: str) -> str:
        """
        保存股票数据到 TSV 文件（保存原始数据，不做格式转换）
        
        Args:
            stocks: 股票信息列表
            pool_type: 股池类型
            
        Returns:
            保存的文件路径
        """
        config = self.POOL_CONFIGS.get(pool_type)
        if not config:
            raise ValueError(f"未知的股池类型: {pool_type}")
        
        # 使用页面日期作为文件名，如果没有则使用当天日期
        if self.page_date:
            date_str = self.page_date.replace('-', '')
        elif self.target_date:
            date_str = self.target_date.replace('-', '')
        else:
            date_str = datetime.now().strftime('%Y%m%d')
        
        # 确保数据目录存在
        self._ensure_data_dir()
        
        filename = f"{date_str}_{config['filename']}.tsv"
        filepath = os.path.join(self.data_dir, filename)
        
        # 定义 TSV 列头和行数据获取函数（保存原始数据）
        if pool_type == 'ztgc':
            # 涨停股池完整列
            headers = ['代码', '名称', '涨跌幅', '最新价', '成交额', '流通市值', '总市值', '换手率', '封板资金', '首次封板时间', '最后封板时间', '炸板次数', '涨停统计', '连板数', '所属行业']
            get_row = lambda s: [
                s.code, s.name, s.change_pct, s.price, s.amount, s.circulating_value,
                s.total_value, s.turnover_rate, s.seal_amount, s.limit_up_time,
                s.last_limit_time, s.open_count, s.limit_up_stats, s.continuous_days, s.industry
            ]
        elif pool_type == 'zbgc':
            # 炸板股池完整列
            headers = ['代码', '名称', '涨跌幅', '最新价', '涨停价', '成交额', '流通市值', '总市值', '换手率', '涨速', '首次封板时间', '炸板次数', '涨停统计', '振幅', '所属行业']
            get_row = lambda s: [
                s.code, s.name, s.change_pct, s.price, s.limit_up_price, s.amount,
                s.circulating_value, s.total_value, s.turnover_rate, s.change_speed,
                s.limit_up_time, s.open_count, s.limit_up_stats, s.amplitude, s.industry
            ]
        else:  # dtgc
            # 跌停股池完整列
            headers = ['代码', '名称', '涨跌幅', '最新价', '成交额', '流通市值', '总市值', '动态市盈率', '换手率', '封单资金', '最后封板时间', '板上成交额', '连续跌停', '开板次数', '所属行业']
            get_row = lambda s: [
                s.code, s.name, s.change_pct, s.price, s.amount, s.circulating_value,
                s.total_value, s.pe_ratio, s.turnover_rate, s.seal_amount,
                s.last_limit_time, s.board_amount, s.continuous_limit_down, s.open_count, s.industry
            ]
        
        # 写入文件
        with open(filepath, 'w', encoding='utf-8') as f:
            # 写入表头
            f.write('\t'.join(headers) + '\n')
            # 写入数据
            for stock in stocks:
                row = get_row(stock)
                f.write('\t'.join(row) + '\n')
        
        logger.info(f"  数据已保存到: {filepath}")
        return filepath
    
    async def scrape_all_pools(self) -> Dict[str, List[StockInfo]]:
        """
        抓取所有股池的数据并保存
        
        Returns:
            包含所有股池数据的字典
        """
        all_data = {}
        
        for pool_type in self.POOL_CONFIGS.keys():
            try:
                stocks = await self.scrape_pool(pool_type)
                all_data[pool_type] = stocks
                
                if stocks:
                    self.save_to_tsv(stocks, pool_type)
                    
                # 在抓取不同股池之间稍作等待
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.exception(f"抓取 {pool_type} 时发生错误: {e}")
                all_data[pool_type] = []
        
        return all_data

class EastMoneyAPIFetcher:
    """
    东方财富网涨停板 API 数据获取器
    直接通过 API 获取数据，无需浏览器，更稳定
    """
    
    # API 端点配置
    API_ENDPOINTS = {
        'ztgc': {
            'name': '涨停股池',
            'url': 'https://push2ex.eastmoney.com/getTopicZTPool',
            'filename': 'limit_up_stocks',
            'sort': 'fbt:asc'
        },
        'zbgc': {
            'name': '炸板股池',
            'url': 'https://push2ex.eastmoney.com/getTopicZBPool',
            'filename': 'broken_limit_stocks',
            'sort': 'fbt:asc'
        },
        'dtgc': {
            'name': '跌停股池',
            'url': 'https://push2ex.eastmoney.com/getTopicDTPool',
            'filename': 'limit_down_stocks',
            'sort': 'fund:asc'
        }
    }
    
    def __init__(self, target_date: str = None, data_dir: str = None):
        """
        初始化 API 数据获取器
        
        Args:
            target_date: 目标日期，格式为 YYYY-MM-DD 或 YYYYMMDD，默认为当天
            data_dir: 数据保存目录
        """
        self.target_date = self._parse_date(target_date) if target_date else datetime.now().strftime('%Y-%m-%d')
        self._base_data_dir = data_dir
    
    def _parse_date(self, date_str: str) -> str:
        """解析日期字符串"""
        if not date_str:
            return datetime.now().strftime('%Y-%m-%d')
        
        date_str = date_str.strip()
        
        if len(date_str) == 8 and date_str.isdigit():
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        
        if len(date_str) == 10 and date_str[4] == '-' and date_str[7] == '-':
            return date_str
        
        for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d']:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime('%Y-%m-%d')
            except ValueError:
                continue
        
        return datetime.now().strftime('%Y-%m-%d')
    
    @property
    def data_dir(self) -> str:
        """获取数据保存目录"""
        date_str = self.target_date.replace('-', '')
        if self._base_data_dir:
            return self._base_data_dir
        else:
            return os.path.join("output", "eastmoney_ztb", date_str)
    
    def _ensure_data_dir(self):
        """确保数据目录存在"""
        os.makedirs(self.data_dir, exist_ok=True)
    
    async def fetch_pool_data(self, pool_type: str) -> List[StockInfo]:
        """
        通过 API 获取指定股池的数据
        
        Args:
            pool_type: 股池类型 ('ztgc', 'zbgc', 'dtgc')
            
        Returns:
            股票信息列表
        """
        if pool_type not in self.API_ENDPOINTS:
            raise ValueError(f"未知的股池类型: {pool_type}")
        
        config = self.API_ENDPOINTS[pool_type]
        logger.info(f"通过 API 获取 {config['name']} 数据...")
        
        date_str = self.target_date.replace('-', '')
        stocks = []
        page_index = 0
        page_size = 100  # 每页获取100条
        
        async with aiohttp.ClientSession() as session:
            while True:
                # 构建 API 请求参数
                params = {
                    'ut': '7eea3edcaed734bea9cbfc24409ed989',
                    'dpt': 'wz.ztzt',
                    'Pageindex': str(page_index),
                    'pagesize': str(page_size),
                    'sort': config.get('sort', 'fbt:asc'),
                    'date': date_str,
                    '_': str(int(datetime.now().timestamp() * 1000))
                }
                
                try:
                    async with session.get(config['url'], params=params, timeout=30) as response:
                        if response.status != 200:
                            logger.warning(f"  API 请求失败: {response.status}")
                            break
                        
                        text = await response.text()
                        
                        # 解析 JSONP 响应
                        json_data = self._parse_jsonp(text)
                        if not json_data:
                            logger.warning("  无法解析 API 响应")
                            break
                        
                        data = json_data.get('data', {})
                        pool_data = data.get('pool', [])
                        
                        if not pool_data:
                            logger.info(f"  第 {page_index + 1} 页无数据，停止获取")
                            break
                        
                        # 解析股票数据
                        for item in pool_data:
                            stock = self._parse_api_item(item, pool_type)
                            if stock:
                                stocks.append(stock)
                        
                        logger.info(f"  第 {page_index + 1} 页获取 {len(pool_data)} 条数据")
                        
                        # 如果返回数据少于 page_size，说明已经是最后一页
                        if len(pool_data) < page_size:
                            break
                        
                        page_index += 1
                        await asyncio.sleep(0.5)  # 请求间隔
                        
                except asyncio.TimeoutError:
                    logger.warning(f"  API 请求超时")
                    break
                except Exception as e:
                    logger.warning(f"  API 请求错误: {e}")
                    break
        
        logger.info(f"  {config['name']} 获取完成，共 {len(stocks)} 条数据")
        return stocks
    
    def _parse_jsonp(self, text: str) -> Optional[dict]:
        """解析 JSONP 响应"""
        try:
            # 移除 JSONP 回调包装
            match = re.search(r'callback\w*\((.*)\)', text, re.DOTALL)
            if match:
                json_str = match.group(1)
                return json.loads(json_str)
            
            # 尝试直接解析 JSON
            return json.loads(text)
        except Exception as e:
            logger.warning(f"  解析 JSONP 失败: {e}")
            return None
    
    def _parse_api_item(self, item: dict, pool_type: str) -> Optional[StockInfo]:
        """
        解析 API 返回的单条数据
        
        Args:
            item: API 返回的数据项
            pool_type: 股池类型
            
        Returns:
            StockInfo 对象
        """
        try:
            stock = StockInfo()
            stock.pool_type = pool_type
            
            # 通用字段
            stock.code = item.get('c', '')  # 股票代码
            stock.name = item.get('n', '')  # 股票名称
            
            # 数值字段需要格式化
            price = item.get('p', 0)  # 最新价（单位：厘，即0.001元）
            if price:
                stock.price = f"{price / 1000:.2f}"
            
            change_pct = item.get('zdp', 0)  # 涨跌幅
            if change_pct:
                stock.change_pct = f"{change_pct:.2f}%"
            
            amount = item.get('amount', 0)  # 成交额
            if amount:
                if amount >= 100000000:
                    stock.amount = f"{amount / 100000000:.2f}亿"
                elif amount >= 10000:
                    stock.amount = f"{amount / 10000:.2f}万"
                else:
                    stock.amount = str(amount)
            
            # 流通市值
            ltsz = item.get('ltsz', 0)
            if ltsz:
                if ltsz >= 100000000:
                    stock.circulating_value = f"{ltsz / 100000000:.2f}亿"
                else:
                    stock.circulating_value = f"{ltsz / 10000:.2f}万"
            
            # 总市值
            tshare = item.get('tshare', 0)
            if tshare:
                if tshare >= 100000000:
                    stock.total_value = f"{tshare / 100000000:.2f}亿"
                else:
                    stock.total_value = f"{tshare / 10000:.2f}万"
            
            # 换手率
            hs = item.get('hs', 0)
            if hs:
                stock.turnover_rate = f"{hs:.2f}%"
            
            # 封板资金
            fund = item.get('fund', 0)
            if fund:
                if fund >= 100000000:
                    stock.seal_amount = f"{fund / 100000000:.2f}亿"
                elif fund >= 10000:
                    stock.seal_amount = f"{fund / 10000:.2f}万"
                else:
                    stock.seal_amount = str(fund)
            
            # 首次封板时间
            fbt = item.get('fbt', '')
            if fbt:
                stock.limit_up_time = str(fbt)
            
            # 最后封板时间
            lbt = item.get('lbt', '')
            if lbt:
                stock.last_limit_time = str(lbt)
            
            # 开板次数/炸板次数
            oc = item.get('oc', 0)
            stock.open_count = f"{oc}次" if oc else "0次"
            
            # 涨停统计
            zttj = item.get('zttj', {})
            if zttj:
                days = zttj.get('days', 0)
                ct = zttj.get('ct', 0)
                stock.limit_up_stats = f"{days}/{ct}"
            
            # 连板数
            lb = item.get('lb', 0)
            if lb > 1:
                stock.continuous_days = f"{lb}连板"
            else:
                stock.continuous_days = "首板"
            
            # 所属行业
            hybk = item.get('hybk', '')
            if hybk:
                stock.industry = hybk
            
            return stock
            
        except Exception as e:
            logger.warning(f"  解析 API 数据项失败: {e}")
            return None
    
    def save_to_tsv(self, stocks: List[StockInfo], pool_type: str) -> str:
        """保存数据到 TSV 文件"""
        config = self.API_ENDPOINTS.get(pool_type)
        if not config:
            raise ValueError(f"未知的股池类型: {pool_type}")
        
        date_str = self.target_date.replace('-', '')
        self._ensure_data_dir()
        
        filename = f"{date_str}_{config['filename']}.tsv"
        filepath = os.path.join(self.data_dir, filename)
        
        # 定义 TSV 列头
        headers = ['代码', '名称', '涨跌幅', '最新价', '成交额', '流通市值', '总市值', '换手率', '封板资金', '首次封板时间', '最后封板时间', '炸板次数', '涨停统计', '连板数', '所属行业']
        
        get_row = lambda s: [
            str(s.code or ''), str(s.name or ''), str(s.change_pct or ''), str(s.price or ''), 
            str(s.amount or ''), str(s.circulating_value or ''), str(s.total_value or ''), 
            str(s.turnover_rate or ''), str(s.seal_amount or ''), str(s.limit_up_time or ''),
            str(s.last_limit_time or ''), str(s.open_count or ''), str(s.limit_up_stats or ''), 
            str(s.continuous_days or ''), str(s.industry or '')
        ]
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\t'.join(headers) + '\n')
            for stock in stocks:
                row = get_row(stock)
                f.write('\t'.join(row) + '\n')
        
        logger.info(f"  数据已保存到: {filepath}")
        return filepath
    
    async def fetch_all_pools(self) -> Dict[str, List[StockInfo]]:
        """获取所有股池数据"""
        all_data = {}
        
        for pool_type in self.API_ENDPOINTS.keys():
            try:
                stocks = await self.fetch_pool_data(pool_type)
                all_data[pool_type] = stocks
                
                if stocks:
                    self.save_to_tsv(stocks, pool_type)
                
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.exception(f"获取 {pool_type} 数据时发生错误: {e}")
                all_data[pool_type] = []
        
        return all_data


async def fetch_via_api(target_date: str = None, data_dir: str = None) -> Dict[str, List[StockInfo]]:
    """
    通过 API 获取涨停板数据（推荐方式）
    
    Args:
        target_date: 目标日期，格式为 YYYY-MM-DD 或 YYYYMMDD
        data_dir: 数据保存目录
        
    Returns:
        包含所有股池数据的字典
    """
    fetcher = EastMoneyAPIFetcher(target_date=target_date, data_dir=data_dir)
    return await fetcher.fetch_all_pools()


async def scrape_all_pools_async(headless: bool = True, data_dir: str = None, target_date: str = None) -> Dict[str, List[StockInfo]]:
    """
    异步抓取所有股池数据的便捷函数
    
    Args:
        headless: 是否使用无头模式
        data_dir: 数据保存目录
        target_date: 目标日期，格式为 YYYY-MM-DD 或 YYYYMMDD
        
    Returns:
        包含所有股池数据的字典
    """
    async with EastMoneyZTBScraper(headless=headless, data_dir=data_dir, target_date=target_date) as scraper:
        return await scraper.scrape_all_pools()

def scrape_all_pools(headless: bool = True, data_dir: str = None, target_date: str = None) -> Dict[str, List[StockInfo]]:
    """
    同步抓取所有股池数据的便捷函数
    
    Args:
        headless: 是否使用无头模式
        data_dir: 数据保存目录
        target_date: 目标日期，格式为 YYYY-MM-DD 或 YYYYMMDD
        
    Returns:
        包含所有股池数据的字典
    """
    return asyncio.run(scrape_all_pools_async(headless=headless, data_dir=data_dir, target_date=target_date))

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='东方财富网涨停板行情爬虫')
    parser.add_argument('--mode', type=str, choices=['api', 'web'], default='api',
                       help='抓取模式: api(通过API获取,推荐), web(通过网页抓取)')
    parser.add_argument('--headless', action='store_true', default=False, help='使用无头模式运行浏览器（仅web模式）')
    parser.add_argument('--pool', type=str, choices=['ztgc', 'zbgc', 'dtgc', 'all'], default='all',
                       help='要抓取的股池类型: ztgc(涨停股池), zbgc(炸板股池), dtgc(跌停股池), all(全部)')
    parser.add_argument('--output', type=str, default=None, help='数据保存目录，默认为 output/eastmoney_ztb/{日期}')
    parser.add_argument('--date', type=str, default=None, 
                       help='目标日期，支持格式: YYYY-MM-DD 或 YYYYMMDD，例如 2024-01-15 或 20240115，默认为当天')
    
    args = parser.parse_args()
    
    async def main():
        data_dir = args.output if args.output else None
        target_date = args.date if args.date else None
        
        if args.mode == 'api':
            # 使用 API 方式获取数据（推荐）
            fetcher = EastMoneyAPIFetcher(target_date=target_date, data_dir=data_dir)
            
            if args.pool == 'all':
                all_data = await fetcher.fetch_all_pools()
                print("\n========== 抓取结果汇总 (API模式) ==========")
                print(f"数据日期: {fetcher.target_date}")
                print(f"保存目录: {fetcher.data_dir}")
                for pool_type, stocks in all_data.items():
                    config = fetcher.API_ENDPOINTS[pool_type]
                    print(f"{config['name']}: {len(stocks)} 条数据")
            else:
                stocks = await fetcher.fetch_pool_data(args.pool)
                if stocks:
                    fetcher.save_to_tsv(stocks, args.pool)
                    config = fetcher.API_ENDPOINTS[args.pool]
                    print(f"\n数据日期: {fetcher.target_date}")
                    print(f"保存目录: {fetcher.data_dir}")
                    print(f"{config['name']}: {len(stocks)} 条数据")
        else:
            # 使用网页方式抓取数据
            async with EastMoneyZTBScraper(headless=args.headless, data_dir=data_dir, target_date=target_date) as scraper:
                if args.pool == 'all':
                    all_data = await scraper.scrape_all_pools()
                    print("\n========== 抓取结果汇总 (Web模式) ==========")
                    if scraper.page_date:
                        print(f"数据日期: {scraper.page_date}")
                    print(f"保存目录: {scraper.data_dir}")
                    for pool_type, stocks in all_data.items():
                        config = scraper.POOL_CONFIGS[pool_type]
                        print(f"{config['name']}: {len(stocks)} 条数据")
                else:
                    stocks = await scraper.scrape_pool(args.pool)
                    if stocks:
                        scraper.save_to_tsv(stocks, args.pool)
                        config = scraper.POOL_CONFIGS[args.pool]
                        print(f"\n数据日期: {scraper.page_date or '未知'}")
                        print(f"保存目录: {scraper.data_dir}")
                        print(f"{config['name']}: {len(stocks)} 条数据")
    
    asyncio.run(main())
