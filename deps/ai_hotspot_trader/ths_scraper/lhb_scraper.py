import asyncio
import json
import os
from datetime import datetime
from typing import List, Dict, Any, Optional
import pandas as pd
from playwright.async_api import async_playwright, Browser, BrowserContext

# 使用 loguru 进行日志管理
try:
    from logger_config import logger
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

class LHBScraper:
    """同花顺龙虎榜数据抓取器"""
    
    BASE_API_URL = "https://data.10jqka.com.cn/dataapi/transaction/stock/v1/list"
    HOME_URL = "https://data.10jqka.com.cn/mobile/transaction/index.html#/"
    
    # 定义不同榜单的配置
    LHB_MODULES = {
        'all': {'module': 'all', 'order_field': 'hot_rank', 'name': '全部'},
        'org': {'module': 'org', 'order_field': 'org_net_value', 'name': '机构榜'},
        'hot_money': {'module': 'hot_money', 'order_field': 'hot_money_net_value', 'name': '游资榜'},
        'org_hot_money': {'module': 'org_hot_money', 'order_field': 'change', 'name': '游资+机构'},
        'market_height': {'module': 'market_height', 'order_field': 'high_days_value', 'name': '市场高度'},
        'first_limit': {'module': 'first_limit', 'order_field': 'limit_order_amount', 'name': '首板'}
    }

    def __init__(self, output_dir: str = "output/lhb", headless: bool = True):
        self.output_dir = output_dir
        self.headless = headless
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self._playwright = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(headless=self.headless)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def init_session(self):
        """初始化浏览器上下文并访问主页以获取 Cookie"""
        self.context = await self.browser.new_context(
            viewport={'width': 375, 'height': 812},
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
            is_mobile=True,
            has_touch=True
        )
        page = await self.context.new_page()
        try:
            logger.info(f"Navigating to {self.HOME_URL} to tracking cookies...")
            await page.goto(self.HOME_URL, wait_until='networkidle', timeout=30000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.warning(f"Initial navigation warning: {e}")
        finally:
            await page.close()

    async def fetch_list_data(self, date: str, module_key: str) -> List[Dict[str, Any]]:
        """
        抓取特定榜单的所有数据
        
        Args:
            date: 日期字符串 YYYY-MM-DD
            module_key: 榜单键名 (all, org, etc.)
            
        Returns:
            数据列表
        """
        config = self.LHB_MODULES.get(module_key)
        if not config:
            logger.error(f"Invalid module key: {module_key}")
            return []

        all_items = []
        page_num = 1
        page_size = 50
        
        logger.info(f"Start fetching {config['name']} ({date})...")

        while True:
            params = {
                'order_field': config['order_field'],
                'order_type': 'desc', # 大部分是 desc, 如果有例外需单独处理，但目前探测结果除了all是asc(rank)其他基本是desc。等等，all的order_field是hot_rank，探测到是asc。
                'date': date,
                'filter': '',
                'page': str(page_num),
                'size': str(page_size),
                'module': config['module'],
                'order_null_greater': '0' # API 探测中看到的值
            }
            
            # 特殊修正：全部榜单的热度排名通常是 ASC
            if module_key == 'all':
                params['order_type'] = 'asc'
                params['order_null_greater'] = '1'

            try:
                # 使用 context.request 发起 API 请求
                api_response = await self.context.request.get(self.BASE_API_URL, params=params)
                if not api_response.ok:
                    logger.error(f"API request failed: {api_response.status} {api_response.status_text}")
                    break
                
                resp_json = await api_response.json()
                data_block = resp_json.get('data', {})
                items = data_block.get('items', [])
                
                if not items:
                    break
                
                all_items.extend(items)
                logger.debug(f"Fetched page {page_num}, items: {len(items)}")

                # Check if we have fetched all data
                # 一般 API 会返回 total count，但这里 data_block 里似乎只有 count (如果是总数)
                # 简单逻辑：如果返回数量小于 page_size，说明是最后一页
                if len(items) < page_size:
                    break
                
                page_num += 1
                await asyncio.sleep(0.5) # 避免请求过快

            except Exception as e:
                logger.error(f"Error fetching page {page_num} for {module_key}: {e}")
                break
        
        logger.info(f"Finished fetching {config['name']}: Total {len(all_items)} items")
        return all_items

    def save_data(self, date_str: str, data_map: Dict[str, List[Dict]]):
        """保存数据到 CSV 并合并生成 JSON"""
        # 格式化输出目录 output/lhb/YYYYMMDD
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_dir_name = date_obj.strftime("%Y%m%d")
        save_dir = os.path.join(self.output_dir, date_dir_name)
        os.makedirs(save_dir, exist_ok=True)
        
        summary_info = {}

        # 1. 保存各分榜 CSV
        for module_key, items in data_map.items():
            if not items:
                continue
            
            df = pd.DataFrame(items)
            module_name = self.LHB_MODULES[module_key]['name']
            
            # 基础字段重命名映射，提升可读性
            column_map = {
                'code': '股票代码',
                'name': '股票名称', # 注意 API 返回可能是 name 或 stock_name
                'stock_name': '股票名称',
                'change': '涨跌幅',
                'latest': '最新价',
                'turnover_rate': '换手率',
                'amount': '成交额',
                'reason_info': '上榜原因',
                'hot_rank': '热度排名',
                'org_net_value': '机构净买入',
                'org_buy_value': '机构买入',
                'org_sell_value': '机构卖出',
                'hot_money_net_value': '游资净买入',
                'high_days_value': '连板天数',
                'limit_order_amount': '封单额',
                'first_limit_time': '首板时间'
            }
            
            # 尝试统一列名
            if 'stock_name' in df.columns and 'name' not in df.columns:
                 df.rename(columns={'stock_name': 'name'}, inplace=True)
                 
            # 仅重命名存在的列
            rename_dict = {k: v for k, v in column_map.items() if k in df.columns}
            df.rename(columns=rename_dict, inplace=True)
            
            file_name = f"{module_key}_{date_dir_name}.csv"
            file_path = os.path.join(save_dir, file_name)
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
            logger.info(f"Saved {module_name} to {file_path}")
            
            summary_info[module_name] = len(df)

        # 2. 合并数据生成 JSON
        self._merge_and_save_json(date_str, data_map, save_dir)
        
        logger.info(f"All data saved for {date_str}. Summary: {summary_info}")

    def _merge_and_save_json(self, date_str: str, data_map: Dict[str, List[Dict]], save_dir: str):
        """
        合并所有榜单数据，提取有用信息，按热度排序保存为 JSON
        """
        stock_map = {}
        
        # 字段映射（标准字段名 -> 中文名）
        field_map = {
            'code': '股票代码',
            'stock_code': '股票代码',
            'name': '股票名称',
            'stock_name': '股票名称',
            'change': '涨跌幅',
            'latest': '最新价',
            'turnover_rate': '换手率',
            'amount': '成交额',
            'hot_rank': '热度排名',
            'reason_info': '上榜原因',
            'reason': '上榜原因',  # 确保 reason 也映射
            'limit_reason': '涨停原因',
            'concept_list': '所属概念',
            'org_net_value': '机构净买入',
            'org_buy_value': '机构买入',
            'org_sell_value': '机构卖出',
            'org_buy_num': '机构买入家数',
            'org_sell_num': '机构卖出家数',
            'org_net_rate': '机构净买入占比',
            'hot_money_net_value': '游资净买入',
            'hot_money_net_rate': '游资净买入占比',
            'hot_money_items': '游资明细',
            'high_days_value': '连板天数',
            'limit_order_amount': '封单额',
            'first_limit_time': '首板时间',
            'buy_value': '买入额',
            'sell_value': '卖出额',
            'net_value': '净买入额',
            'net_rate': '净买入占比',
            'market_id': '市场ID',
            'tags': '标签原始数据',
            'range_days': '统计天数',
            'high_days_name': '板天数描述'
        }
        
        # 需要移除的旧字段/英文字段（在清理阶段使用）
        keys_to_remove = [
            'code', 'stock_code', 'name', 'stock_name', 
            'change', 'latest', 'tags',
            'reason_info', 'limit_ignore_reason', 'order_type', 'is_follow', 'rise_rate'
        ]

        # 优先处理 'all' 榜单
        if 'all' in data_map:
            for item in data_map['all']:
                self._process_item(item, stock_map, field_map)
        
        # 处理其他榜单
        for module_key, items in data_map.items():
            if module_key == 'all':
                continue
            for item in items:
                self._process_item(item, stock_map, field_map)

        # 转换为列表
        merged_list = list(stock_map.values())
        
        # 数据清洗与格式化
        for stock in merged_list:
            # 1. 概念板块提取
            if isinstance(stock.get('概念板块'), str):
                try:
                    concepts_json = stock['概念板块'].replace("'", '"')
                    concepts_list = json.loads(concepts_json)
                    stock['概念板块'] = [c.get('name') for c in concepts_list if 'name' in c]
                except:
                    pass
            
            # 2. 标签提取
            if isinstance(stock.get('标签原始数据'), str):
                try:
                    tags_json = stock['标签原始数据'].replace("'", '"')
                    tags_list = json.loads(tags_json)
                    stock['龙虎榜标签'] = [t.get('name') for t in tags_list if 'name' in t]
                except:
                     pass
            
            # 删除原始标签数据，避免冗余
            if '标签原始数据' in stock:
                del stock['标签原始数据']

            # 3. 游资明细由列表清洗（可选：仅保留名字）
            if isinstance(stock.get('游资明细'), list):
                # 保持原样详细信息，但 key 也需要汉化？ 暂时保留原样，信息量大
                pass

            # 4. 确保数值类型
            for int_field in ['热度排名', '连板天数', '统计天数', '机构买入家数', '机构卖出家数']:
                 if stock.get(int_field):
                    try:
                        stock[int_field] = int(float(stock[int_field]))
                    except:
                        pass
            
            for float_field in ['涨跌幅', '最新价', '机构净买入', '游资净买入', '成交额', '买入额', '卖出额', '净买入额', '净买入率', '封单额', '机构净买入率', '游资净买入率']:
                if stock.get(float_field):
                     try:
                        stock[float_field] = float(stock[float_field])
                     except:
                        pass
            
            # 5. 清理重复的 Key 或 英文 Key
            for key in list(stock.keys()):
                if key in keys_to_remove:
                    del stock[key]
        
        # 补充合并
        for stock in merged_list:
             if not stock.get('上榜原因') and stock.get('涨停原因'):
                  stock['上榜原因'] = stock.get('涨停原因')

        # 排序
        merged_list.sort(key=lambda x: x.get('热度排名') if x.get('热度排名') is not None else 999999)

        # 保存
        json_file_name = f"lhb_merged_{date_str.replace('-', '')}.json"
        json_path = os.path.join(save_dir, json_file_name)
        
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(merged_list, f, ensure_ascii=False, indent=2)
            
        logger.info(f"Merged JSON data saved to {json_path}, Total records: {len(merged_list)}")

    def _process_item(self, item: Dict, stock_map: Dict, field_map: Dict):
        """处理单条数据并更新到 stock_map"""
        # 获取股票代码
        code = str(item.get('code') or item.get('stock_code') or '')
        if not code:
            return
            
        # 获取统计天数，用于区分同一股票的不同榜单
        range_days = item.get('range_days') or item.get('统计天数')
        if not range_days:
            range_days = '1' # 默认为 1 日
        range_days = str(range_days)
        
        # 唯一键：代码_统计天数
        unique_key = f"{code}_{range_days}"

        if unique_key not in stock_map:
            stock_map[unique_key] = {}
            # 初始化
            for k, v in item.items():
                dest_key = field_map.get(k, k) 
                stock_map[unique_key][dest_key] = v
        else:
            stock = stock_map[unique_key]
            # 更新
            for k, v in item.items():
                if v is None:
                    continue
                dest_key = field_map.get(k, k)
                
                if dest_key not in stock:
                    stock[dest_key] = v
                else:
                    if not stock[dest_key]:
                        stock[dest_key] = v
                    elif isinstance(v, str) and isinstance(stock[dest_key], str):
                         if len(v) > len(stock[dest_key]):
                             stock[dest_key] = v
        
        # 确保有代码字段
        # 虽然上面的循环已经会处理 field_map 里的 'code' -> '股票代码'
        # 但如果原始数据里没有 'code' 只有 'stock_code'，需要确保最终有一个统一的中文key
        if '股票代码' not in stock_map[unique_key]:
             stock_map[unique_key]['股票代码'] = code
        
        # 确保统计天数存在（如果是默认覆盖的）
        if '统计天数' not in stock_map[unique_key]:
            stock_map[unique_key]['统计天数'] = range_days

    async def run(self, date_str: str = None):
        """执行主抓取流程"""
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        
        logger.info(f"Starting LHB scrape for date: {date_str}")
        
        await self.init_session()
        
        all_data = {}
        
        for key in self.LHB_MODULES.keys():
            data = await self.fetch_list_data(date_str, key)
            if data:
                all_data[key] = data
        
        if all_data:
            self.save_data(date_str, all_data)
        else:
            logger.warning("No data fetched for any module.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="同花顺龙虎榜爬虫")
    parser.add_argument("--date", type=str, help="抓取日期 (YYYY-MM-DD), 默认当天", default=None)
    args = parser.parse_args()
    
    async def main():
        async with LHBScraper(headless=True) as scraper:
            await scraper.run(args.date)
            
    asyncio.run(main())