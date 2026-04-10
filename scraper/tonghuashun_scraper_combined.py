"""
同花顺板块行情爬虫 - 组合版
使用requests获取板块基本信息和实时行情
使用Selenium获取第二页及以后的板块成分股（通过AJAX URL）
"""

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service
import pandas as pd
import json
import re
from typing import Dict, List, Optional
from datetime import datetime
import time
import logging
import random
from fake_useragent import UserAgent  # 需要安装: pip install fake-useragent
import os

# 设置日志格式，包含行号
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)
logger = logging.getLogger(__name__)


class TonghuashunScraperCombined:
    """同花顺板块数据爬虫 - 组合版"""
    
    def __init__(self, headless: bool = True, edge_driver_path: Optional[str] = None):
        """
        初始化爬虫
        :param headless: 是否使用无头模式
        :param edge_driver_path: EdgeDriver路径
        """
        # Requests相关
        self.session = requests.Session()
        
        # 使用随机User-Agent
        try:
            ua = UserAgent()
            user_agent = ua.random
        except:
            # 如果fake-useragent失败，使用备用列表
            user_agents = [
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
            ]
            user_agent = random.choice(user_agents)
        
        self.base_headers = {
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        }
        self.session.headers.update(self.base_headers)
        self.hexin_v = None
        
        # 添加请求延迟范围
        self.min_delay = 0.5
        self.max_delay = 2.0
        
        # 添加重试机制
        self.max_retries = 3
        self.retry_delay = 5
        
        # Selenium相关
        self.driver = None
        self.headless = headless
        self.edge_driver_path = edge_driver_path or 'msedgedriver.exe'
        self._driver_closed = False  # 标记驱动是否已关闭
        
        # 初始化会话
        self._init_session()
    
    def _init_session(self):
        """初始化会话，获取必要的cookies和hexin-v"""
        try:
            # 添加随机延迟
            time.sleep(random.uniform(0.5, 1.5))
            
            main_url = "https://q.10jqka.com.cn/"
            
            # 设置超时和重试
            for attempt in range(self.max_retries):
                try:
                    response = self.session.get(main_url, timeout=15)
                    response.raise_for_status()
                    break
                except requests.RequestException as e:
                    if attempt == self.max_retries - 1:
                        raise
                    logger.warning(f"初始化会话失败 (尝试 {attempt + 1}/{self.max_retries}): {str(e)}")
                    time.sleep(self.retry_delay)
            
            if response.text:
                hexin_match = re.search(r'hexin-v["\']?\s*[:=]\s*["\']([^"\']+)["\']', response.text)
                if hexin_match:
                    self.hexin_v = hexin_match.group(1)
                    logger.info(f"获取到hexin-v: {self.hexin_v}")
                else:
                    self.hexin_v = "A-eu_KDJQ7-XfsczP-j9f2-CdhC0bLunFUI_zrlUACae3QnOwTxLniUQzxbK"
            
            logger.info("会话初始化成功")
            
        except Exception as e:
            logger.warning(f"会话初始化失败: {str(e)}")
            self.hexin_v = "A-eu_KDJQ7-XfsczP-j9f2-CdhC0bLunFUI_zrlUACae3QnOwTxLniUQzxbK"
    
    def _init_driver(self):
        """初始化Edge浏览器驱动（按需初始化）"""
        if self.driver:
            return
            
        try:
            options = Options()
            
            # 基本选项
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            
            # 隐藏自动化特征
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--no-sandbox")
            
            # WebGL和GPU相关设置（解决登录页面渲染问题）
            options.add_argument("--enable-unsafe-swiftshader")  # 启用软件渲染器
            options.add_argument("--disable-web-security")
            options.add_argument("--disable-features=VizDisplayCompositor")
            options.add_argument("--disable-gpu-sandbox")
            options.add_argument("--use-gl=swiftshader")  # 使用软件渲染
            
            # 设置用户代理
            options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0')
            
            # 设置偏好（不禁用图片，因为登录页面可能需要显示验证码）
            prefs = {
                "profile.default_content_setting_values.notifications": 2,
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False
            }
            options.add_experimental_option("prefs", prefs)
            
            # 是否使用无头模式
            if self.headless:
                options.add_argument('--headless')
                options.add_argument('--window-size=1920,1080')
            
            # 创建驱动
            try:
                service = Service(self.edge_driver_path)
                self.driver = webdriver.Edge(service=service, options=options)
            except Exception as e:
                logger.error(f"使用指定路径 {self.edge_driver_path} 创建Edge驱动失败: {str(e)}")
                logger.info("尝试使用系统PATH中的msedgedriver...")
                self.driver = webdriver.Edge(options=options)
            
            # 执行CDP命令，修改navigator.webdriver标志
            self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': '''
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    })
                '''
            })
            
            # 设置页面加载超时
            self.driver.set_page_load_timeout(30)
            
            self._driver_closed = False
            logger.info("Edge浏览器驱动初始化成功")
            
        except Exception as e:
            logger.error(f"Edge浏览器驱动初始化失败: {str(e)}")
            raise
    
    def __del__(self):
        """析构函数，确保浏览器关闭"""
        self.close()
    
    def close(self):
        """关闭浏览器"""
        if self.driver and not self._driver_closed:
            try:
                self.driver.quit()
                self._driver_closed = True
                logger.info("浏览器已关闭")
            except Exception as e:
                # 忽略关闭时的连接错误
                if "Failed to establish a new connection" not in str(e):
                    logger.error(f"关闭浏览器时出错: {str(e)}")
                self._driver_closed = True
    
    def get_sector_info(self, sector_code: str) -> Dict:
        """获取板块基本信息和实时行情（使用requests）"""
        url = f"https://q.10jqka.com.cn/thshy/detail/code/{sector_code}/"
        
        for attempt in range(self.max_retries):
            try:
                # 随机延迟
                time.sleep(random.uniform(self.min_delay, self.max_delay))
                
                # 构建请求头
                headers = self.base_headers.copy()
                headers.update({
                    'Referer': 'https://q.10jqka.com.cn/thshy/',
                    'Host': 'q.10jqka.com.cn',
                })
                
                # 如果有hexin-v，添加到请求头
                if self.hexin_v:
                    headers['hexin-v'] = self.hexin_v
                
                # 发送请求
                response = self.session.get(url, headers=headers, timeout=15)
                response.raise_for_status()
                
                # 检查是否被反爬
                if '访问过于频繁' in response.text or response.status_code == 403:
                    logger.warning(f"检测到反爬限制，等待后重试 (尝试 {attempt + 1}/{self.max_retries})")
                    time.sleep(random.uniform(5, 10))
                    continue
                
                if response.encoding == 'ISO-8859-1':
                    response.encoding = response.apparent_encoding or 'gbk'
                
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # 更新hexin-v
                scripts = soup.find_all('script')
                for script in scripts:
                    if script.string:
                        hexin_match = re.search(r'hexin-v["\']?\s*[:=]\s*["\']([^"\']+)["\']', script.string)
                        if hexin_match:
                            new_hexin_v = hexin_match.group(1)
                            if new_hexin_v != self.hexin_v:
                                self.hexin_v = new_hexin_v
                                logger.debug(f"更新hexin-v: {self.hexin_v}")
                            break
                
                # 解析板块信息
                sector_info = self._parse_sector_info(soup, sector_code)
                
                # 如果成功获取数据，返回
                if sector_info and sector_info.get('sector_name'):
                    return sector_info
                
            except requests.exceptions.RequestException as e:
                logger.error(f"获取板块信息失败 {sector_code} (尝试 {attempt + 1}/{self.max_retries}): {str(e)}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    # 重新初始化会话
                    if attempt == 1:
                        self._init_session()
            except Exception as e:
                logger.error(f"解析板块信息失败 {sector_code}: {str(e)}")
                return {}
        
        logger.error(f"获取板块信息失败 {sector_code}: 超过最大重试次数")
        return {}
    
    def _parse_sector_info(self, soup: BeautifulSoup, sector_code: str) -> Dict:
        """解析板块信息"""
        info = {
            'sector_code': sector_code,
            'sector_name': '',
            'current_price': 0.0,
            'price_change': 0.0,
            'price_change_amt': 0.0,
            'open_price': 0.0,
            'close_price': 0.0,
            'high_price': 0.0,
            'low_price': 0.0,
            'volume': 0.0,
            'turnover': 0.0,
            'rise_count': 0,
            'fall_count': 0,
            'net_inflow': 0.0,
            'rank': '',
            'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # 查找board-hq元素
        board_hq = soup.find('div', class_='board-hq')
        if board_hq:
            h3 = board_hq.find('h3')
            if h3:
                text = h3.get_text(strip=True)
                info['sector_name'] = re.sub(r'\d+', '', text).strip()
            
            price_elem = board_hq.find('span', class_='board-xj')
            if price_elem:
                try:
                    info['current_price'] = float(price_elem.text.strip())
                except:
                    pass
            
            zdf_elem = board_hq.find('p', class_='board-zdf')
            if zdf_elem:
                zdf_text = zdf_elem.text.strip()
                parts = zdf_text.split()
                if len(parts) >= 2:
                    try:
                        info['price_change_amt'] = float(parts[0])
                        info['price_change'] = float(parts[1].replace('%', ''))
                    except:
                        pass
        
        # 查找board-infos元素
        board_infos = soup.find('div', class_='board-infos')
        if board_infos:
            dls = board_infos.find_all('dl')
            for dl in dls:
                dt = dl.find('dt')
                dd = dl.find('dd')
                
                if dt and dd:
                    label = dt.text.strip()
                    value = dd.text.strip()
                    
                    try:
                        if label == '今开':
                            info['open_price'] = float(value)
                        elif label == '昨收':
                            info['close_price'] = float(value)
                        elif label == '最高':
                            info['high_price'] = float(value)
                        elif label == '最低':
                            info['low_price'] = float(value)
                        elif label == '成交量(万手)':
                            info['volume'] = float(value)
                        elif label == '成交额(亿)':
                            info['turnover'] = float(value)
                        elif label == '板块涨幅':
                            info['price_change'] = float(value.replace('%', ''))
                        elif label == '涨幅排名':
                            info['rank'] = value
                        elif label == '涨跌家数':
                            rise_elem = dd.find('span', class_='arr-rise-s')
                            fall_elem = dd.find('span', class_='arr-fall-s')
                            if rise_elem:
                                info['rise_count'] = int(rise_elem.text.strip())
                            if fall_elem:
                                info['fall_count'] = int(fall_elem.text.strip())
                        elif label == '资金净流入(亿)':
                            info['net_inflow'] = float(value.replace('-', '').strip())
                            if 'c-fall' in dd.get('class', []):
                                info['net_inflow'] = -info['net_inflow']
                    except Exception as e:
                        logger.debug(f"解析 {label} 失败: {str(e)}")
        
        return info
    
    def get_all_sector_stocks(self, sector_code: str) -> pd.DataFrame:
        """获取板块的全部成分股（第一页使用requests，后续页使用Selenium获取AJAX数据）"""
        all_stocks = []
        
        logger.info(f"开始获取板块 {sector_code} 的全部成分股...")
        
        # 首先使用requests获取第一页
        first_page_url = f"https://q.10jqka.com.cn/thshy/detail/code/{sector_code}/"
        try:
            headers = self.base_headers.copy()
            headers['Referer'] = 'https://q.10jqka.com.cn/thshy/'
            
            response = self.session.get(first_page_url, headers=headers, timeout=15)
            response.raise_for_status()
            
            if response.encoding == 'ISO-8859-1':
                response.encoding = response.apparent_encoding or 'gbk'
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 更新hexin-v
            scripts = soup.find_all('script')
            for script in scripts:
                if script.string:
                    hexin_match = re.search(r'hexin-v["\']?\s*[:=]\s*["\']([^"\']+)["\']', script.string)
                    if hexin_match:
                        self.hexin_v = hexin_match.group(1)
                        logger.info(f"从第一页更新hexin-v: {self.hexin_v}")
                        break
            
            # 获取最大页数
            max_pages = 1
            pager = soup.find('div', class_='m-pager')
            if pager:
                page_info = pager.find('span', class_='page_info')
                if page_info:
                    match = re.search(r'(\d+)/(\d+)', page_info.text)
                    if match:
                        max_pages = int(match.group(2))
            
            logger.info(f"板块 {sector_code} 共有 {max_pages} 页数据")
            
            # 解析第一页的股票数据
            stocks_in_page = self._parse_stocks_from_page(soup)
            if stocks_in_page:
                all_stocks.extend(stocks_in_page)
                logger.info(f"第1页获取到 {len(stocks_in_page)} 只股票")
            
            # 如果有多页，使用Selenium获取后续页面的AJAX数据
            if max_pages > 1:
                # 初始化Selenium驱动
                self._init_driver()
                
                # 检查是否需要登录（超过5页需要登录）
                if max_pages > 5:
                    logger.warning(f"板块 {sector_code} 有 {max_pages} 页数据，超过5页需要登录才能获取全部数据")
                    
                    # 打开登录页面
                    login_url = "https://upass.10jqka.com.cn/login?redir=HTTP_REFERER"
                    logger.info(f"正在打开登录页面: {login_url}")
                    self.driver.get(login_url)
                    
                    # 等待登录页面加载完成
                    logger.info("等待登录页面加载...")
                    time.sleep(3)  # 给页面更多时间渲染
                    
                    # 尝试等待特定元素出现（如登录表单）
                    try:
                        from selenium.webdriver.common.by import By
                        from selenium.webdriver.support.ui import WebDriverWait
                        from selenium.webdriver.support import expected_conditions as EC
                        
                        # 等待登录表单或任何主要元素出现
                        wait = WebDriverWait(self.driver, 10)
                        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                        logger.info("登录页面已加载")
                    except Exception as e:
                        logger.warning(f"等待登录页面元素时出错: {e}")
                    
                    # 等待用户登录
                    logger.info("请在浏览器中完成登录，登录成功后按回车键继续...")
                    logger.info("提示：如果页面显示不正常，请手动刷新页面")
                    input("按回车键继续...")
                    
                    # 登录后重新访问板块页面以获取cookies
                    logger.info("登录完成，重新访问板块页面...")
                    self.driver.get(first_page_url)
                    time.sleep(3)  # 等待页面加载
                    
                    # 获取登录后的cookies
                    selenium_cookies = self.driver.get_cookies()
                    for cookie in selenium_cookies:
                        self.session.cookies.set(cookie['name'], cookie['value'])
                else:
                    # 设置必要的cookies
                    cookies = self.session.cookies.get_dict()
                    self.driver.get("https://q.10jqka.com.cn/")
                    for name, value in cookies.items():
                        self.driver.add_cookie({'name': name, 'value': value})
                
                # 获取后续页面（通过AJAX URL）
                for page in range(2, max_pages + 1):
                    ajax_url = f"https://q.10jqka.com.cn/thshy/detail/field/199112/order/desc/page/{page}/ajax/1/code/{sector_code}"
                    
                    logger.info(f"正在获取第 {page} 页数据...")
                    
                    try:
                        # 使用Selenium访问AJAX URL
                        self.driver.get(ajax_url)
                        
                        # 等待页面加载
                        time.sleep(random.uniform(1, 2))
                        
                        # 获取页面内容
                        page_source = self.driver.page_source
                        
                        # 解析内容
                        if page_source and '<html' in page_source:
                            # 提取body内容
                            body_match = re.search(r'<body[^>]*>(.*?)</body>', page_source, re.DOTALL)
                            if body_match:
                                content = body_match.group(1)
                            else:
                                content = page_source
                        else:
                            content = page_source
                        
                        if content and content.strip():
                            soup = BeautifulSoup(content, 'html.parser')
                            stocks_in_page = self._parse_stocks_from_page(soup)
                            
                            if stocks_in_page:
                                all_stocks.extend(stocks_in_page)
                                logger.info(f"第 {page} 页获取到 {len(stocks_in_page)} 只股票，累计 {len(all_stocks)} 只")
                            else:
                                logger.warning(f"第 {page} 页未找到股票数据")
                                # 如果是超过5页后未找到数据，可能是登录失效
                                if page > 5:
                                    logger.error(f"第 {page} 页未找到数据，可能需要重新登录")
                                    break
                        else:
                            logger.warning(f"第 {page} 页返回空内容")
                            # 如果是超过5页后返回空内容，可能是登录失效
                            if page > 5:
                                logger.error(f"第 {page} 页返回空内容，可能需要重新登录")
                                break
                            
                    except Exception as e:
                        logger.error(f"获取第 {page} 页失败: {str(e)}")
                        # 如果是超过5页后失败，可能是登录问题
                        if page > 5:
                            logger.error(f"第 {page} 页获取失败，可能需要重新登录")
                            break
                        # 尝试继续获取下一页
                        continue
                    
                    # 随机延迟，避免请求过快
                    if page < max_pages:
                        time.sleep(random.uniform(0.5, 1))
            
        except Exception as e:
            logger.error(f"获取成分股失败: {str(e)}")
            return pd.DataFrame()
        finally:
            # 确保关闭浏览器
            self.close()
        
        if all_stocks:
            df = pd.DataFrame(all_stocks)
            df = df.drop_duplicates(subset=['代码'], keep='first')
            df['更新时间'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            df['板块代码'] = sector_code
            
            # 确保数据类型正确
            numeric_columns = ['最新价', '涨跌幅', '涨跌额', '成交量(万手)', '成交额(亿元)', '换手率']
            for col in numeric_columns:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            
            # 排序
            df = df.sort_values('涨跌幅', ascending=False).reset_index(drop=True)
            
            logger.info(f"成功获取板块 {sector_code} 的 {len(df)} 只成分股")
            return df
        else:
            logger.warning(f"未能获取板块 {sector_code} 的成分股数据")
            return pd.DataFrame()
    
    def _parse_stocks_from_page(self, soup: BeautifulSoup) -> List[Dict]:
        """从页面解析股票数据"""
        stocks = []
        
        # 查找股票表格
        table = soup.find('table', class_=re.compile(r'm-table'))
        if not table:
            if soup.name == 'table':
                table = soup
            else:
                table = soup.find('table')
        
        if table:
            tbody = table.find('tbody')
            if not tbody:
                tbody = table
            
            rows = tbody.find_all('tr')
            for row in rows:
                if row.find('th'):
                    continue
                
                stock_info = self._parse_stock_row(row)
                if stock_info and stock_info.get('代码'):
                    stocks.append(stock_info)
        else:
            logger.warning("页面中未找到股票表格")
        
        return stocks
    
    def _parse_stock_row(self, row) -> Optional[Dict]:
        """解析股票表格行"""
        try:
            cells = row.find_all(['td', 'th'])
            
            if len(cells) < 9:
                return None
            
            stock_info = {
                '代码': '',
                '名称': '',
                '最新价': 0.0,
                '涨跌幅': 0.0,
                '涨跌额': 0.0,
                '成交量(万手)': 0.0,
                '成交额(亿元)': 0.0,
                '换手率': 0.0
            }
            
            # 解析代码
            code_cell_text = cells[1].get_text(strip=True)
            code_link = cells[1].find('a')
            stock_info['代码'] = code_link.get_text(strip=True) if code_link else code_cell_text
            
            if not re.match(r'^\d{6}$', stock_info['代码']):
                return None
            
            # 解析名称
            name_cell_text = cells[2].get_text(strip=True)
            name_link = cells[2].find('a')
            stock_info['名称'] = name_link.get_text(strip=True) if name_link else name_cell_text
            
            if not stock_info['名称'] or stock_info['名称'] == '--':
                return None
            
            # 解析其他数据
            stock_info['最新价'] = self._safe_float(cells[3].get_text(strip=True))
            stock_info['涨跌幅'] = self._parse_percentage(cells[4].get_text(strip=True))
            stock_info['涨跌额'] = self._safe_float(cells[5].get_text(strip=True))
            
            # 成交量
            volume_text = cells[6].get_text(strip=True)
            if volume_text == '--':
                stock_info['成交量(万手)'] = 0.0
            else:
                num_match = re.search(r'([\d.]+)', volume_text)
                if num_match:
                    val = self._safe_float(num_match.group(1))
                    if '万' in volume_text:
                        stock_info['成交量(万手)'] = val
                    else:
                        stock_info['成交量(万手)'] = val / 10000.0
            
            # 成交额
            turnover_text = cells[7].get_text(strip=True)
            parsed_turnover = self._parse_numerical_value_with_unit(turnover_text)
            stock_info['成交额(亿元)'] = parsed_turnover / 1e8
            
            # 换手率
            stock_info['换手率'] = self._parse_percentage(cells[8].get_text(strip=True))
            
            return stock_info
            
        except Exception as e:
            logger.error(f"解析股票行失败: {str(e)}")
            return None
    
    @staticmethod
    def _safe_float(text: str, default: float = 0.0) -> float:
        """安全转换为浮点数"""
        try:
            if text == '--':
                return default
            return float(text)
        except (ValueError, TypeError):
            return default
    
    @staticmethod
    def _parse_percentage(text: str, default: float = 0.0) -> float:
        """解析百分比字符串"""
        try:
            if text == '--':
                return 0.0
            return float(text.replace('%', ''))
        except (ValueError, TypeError):
            return default
    
    @staticmethod
    def _parse_numerical_value_with_unit(text: str, default: float = 0.0) -> float:
        """解析带单位的数值字符串，返回基本单位（元）"""
        text = text.strip()
        if text == '--':
            return default
        
        num_match = re.search(r'([\d.-]+)', text)
        if not num_match:
            return default
        
        value = TonghuashunScraperCombined._safe_float(num_match.group(1), default)
        
        if '亿' in text:
            value *= 1e8
        elif '万' in text:
            value *= 1e4
        
        return value


class TonghuashunAPI:
    """同花顺数据接口封装"""
    
    def __init__(self, headless: bool = True, edge_driver_path: Optional[str] = None):
        """
        初始化API
        :param headless: 是否使用无头模式
        :param edge_driver_path: EdgeDriver路径
        """
        self.scraper = None
        self.headless = headless
        self.edge_driver_path = edge_driver_path
        self._request_count = 0
        self._last_request_time = 0
    
    def _ensure_scraper(self):
        """确保爬虫实例存在"""
        if not self.scraper:
            self.scraper = TonghuashunScraperCombined(
                headless=self.headless,
                edge_driver_path=self.edge_driver_path
            )
    
    def _rate_limit(self):
        """速率限制"""
        self._request_count += 1
        
        # 每10个请求强制休息
        if self._request_count % 10 == 0:
            logger.info(f"已发送 {self._request_count} 个请求，休息一下...")
            time.sleep(random.uniform(5, 10))
        
        # 确保请求间隔
        current_time = time.time()
        if self._last_request_time > 0:
            elapsed = current_time - self._last_request_time
            if elapsed < 1:
                time.sleep(1 - elapsed)
        
        self._last_request_time = time.time()
    
    def get_sector_info(self, sector_code: str) -> Dict:
        """
        获取板块基本信息和实时行情
        
        :param sector_code: 板块代码
        :return: 板块信息字典
        """
        self._ensure_scraper()
        self._rate_limit()
        return self.scraper.get_sector_info(sector_code)
    
    def get_sector_stocks(self, sector_code: str) -> pd.DataFrame:
        """
        获取板块全部成分股
        
        :param sector_code: 板块代码
        :return: 成分股DataFrame
        """
        self._ensure_scraper()
        return self.scraper.get_all_sector_stocks(sector_code)
    
    def get_sector_data(self, sector_code: str) -> Dict:
        """
        获取板块完整数据（包括基本信息和成分股）
        
        :param sector_code: 板块代码
        :return: 包含板块信息和成分股的字典
        """
        self._ensure_scraper()
        
        result = {
            'sector_info': self.scraper.get_sector_info(sector_code),
            'stocks': self.scraper.get_all_sector_stocks(sector_code)
        }
        
        return result
    
    def close(self):
        """关闭资源"""
        if self.scraper:
            self.scraper.close()
            self.scraper = None
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.close()
        return False


def main():
    """主函数，演示API使用"""
    print("=" * 60)
    print("同花顺板块行情爬虫API - 使用示例")
    print("=" * 60)
    
    # 使用上下文管理器自动管理资源
    with TonghuashunAPI(headless=False) as api:
        sector_code = "883993"
        
        # 获取板块信息
        print(f"\n1. 获取板块 {sector_code} 的基本信息...")
        sector_info = api.get_sector_info(sector_code)
        if sector_info:
            print("\n板块基本信息：")
            for key, value in sector_info.items():
                print(f"  {key}: {value}")
        
        # 获取成分股
        print(f"\n2. 获取板块 {sector_code} 的成分股...")
        stocks_df = api.get_sector_stocks(sector_code)
        
        if not stocks_df.empty:
            print(f"\n成功获取 {len(stocks_df)} 只成分股")
            print("\n前10只股票：")
            print(stocks_df.head(10)[['代码', '名称', '最新价', '涨跌幅', '成交额(亿元)']].to_string(index=False))
            
            # 创建输出目录
            os.makedirs("output", exist_ok=True)
            
            # 保存数据
            filename = f"output/sector_{sector_code}_stocks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            stocks_df.to_csv(filename, index=False, encoding='utf-8-sig')
            print(f"\n数据已保存到: {filename}")
    
    print("\n程序结束")


if __name__ == "__main__":
    main()