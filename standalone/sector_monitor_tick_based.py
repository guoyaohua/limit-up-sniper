"""
板块监控 - 基于Tick数据的实时计算（优化版）
替代原有的网页爬虫方式，直接根据板块成分股的tick数据计算板块行情

性能优化:
1. 缓存板块映射数据，避免重复加载文件
2. 一次性获取所有股票tick数据，多个板块类型共享
3. 使用向量化操作减少循环
4. 提前构建涨停股票集合，减少查询时间
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Set
import pandas as pd
import numpy as np
from loguru import logger
from infra.utils import run_with_timeout


class SectorMonitorOptimized:
    """优化的板块监控类 - 缓存数据和批量处理"""
    def __init__(self, data_source: str = 'THS'):
        """
        初始化监控器
        
        Args:
            data_source: 数据源，'THS' (同花顺) 或 'EM' (东方财富)，默认为 'THS'
        """
        self.data_source = data_source.upper()
        self.concept_mapping = {}  # 概念板块映射缓存
        self.industry_mapping = {}  # 行业板块映射缓存
        self.all_stocks_set = set()  # 所有成分股集合
        self.last_load_time = 0  # 上次加载时间
        self.cache_duration = 3600  # 缓存1小时（秒）
        
        # 根据数据源设置路径
        if self.data_source == 'THS':
            self.concept_path = 'output/concept_sectors/THS'
            self.industry_path = 'output/industry_sectors/THS'
            logger.info("使用同花顺(THS)板块数据源")
        else:  # 默认使用东方财富
            self.data_source = 'EM'
            self.concept_path = 'output/concept_sectors'
            self.industry_path = 'output/industry_sectors'
            logger.info("使用东方财富(EM)板块数据源")

    def _load_sector_mappings(self, force_reload: bool = False):
        """
        加载板块映射数据（带缓存）
        
        Args:
            force_reload: 是否强制重新加载
        """
        current_time = time.time()

        # 检查是否需要重新加载
        if not force_reload and self.concept_mapping and (
                current_time - self.last_load_time) < self.cache_duration:
            return

        # 加载概念板块
        concept_file = os.path.join(self.concept_path, 'sector_to_stocks_mapping_latest.json')
        if os.path.exists(concept_file):
            try:
                with open(concept_file, 'r', encoding='utf-8') as f:
                    self.concept_mapping = json.load(f)
                logger.info(f"[{self.data_source}] 加载概念板块映射: {len(self.concept_mapping)} 个板块")
            except Exception as e:
                logger.error(f"[{self.data_source}] 加载概念板块映射失败: {e}")
                self.concept_mapping = {}

        # 加载行业板块
        industry_file = os.path.join(self.industry_path, 'sector_to_stocks_mapping_latest.json')
        if os.path.exists(industry_file):
            try:
                with open(industry_file, 'r', encoding='utf-8') as f:
                    self.industry_mapping = json.load(f)
                logger.info(f"[{self.data_source}] 加载行业板块映射: {len(self.industry_mapping)} 个板块")
            except Exception as e:
                logger.error(f"[{self.data_source}] 加载行业板块映射失败: {e}")
                self.industry_mapping = {}

        # 构建所有成分股集合
        self.all_stocks_set.clear()
        for sector_info in self.concept_mapping.values():
            self.all_stocks_set.update(sector_info.get('stocks', []))
        for sector_info in self.industry_mapping.values():
            self.all_stocks_set.update(sector_info.get('stocks', []))

        self.last_load_time = current_time
        logger.info(f"板块映射加载完成，共 {len(self.all_stocks_set)} 只成分股")

    def _add_stock_suffix(self, stocks: List[str]) -> List[str]:
        """
        批量添加股票代码后缀
        
        Args:
            stocks: 股票代码列表（不含后缀）
            
        Returns:
            带后缀的股票代码列表
        """
        result = []
        for stock in stocks:
            if stock.startswith('6'):
                result.append(f"{stock}.SH")
            elif stock.startswith('0') or stock.startswith('3'):
                result.append(f"{stock}.SZ")
            else:
                result.append(f"{stock}.BJ")
        return result

    def _get_limit_up_stocks(
            self, shared_data: dict) -> Tuple[Set[str], Dict[str, any]]:
        """
        获取涨停股票集合和首次涨停时间
        
        Args:
            shared_data: 共享数据
            
        Returns:
            (涨停股票集合, 涨停信息字典)
        """
        from infra.common_enums import StockLimitStatusInt

        limit_up_stocks = set()
        limit_up_info = {}

        if '涨停池' not in shared_data or '股票状态信号' not in shared_data:
            return limit_up_stocks, limit_up_info

        for stock_code, time_str in shared_data['涨停池'].items():
            if stock_code in shared_data['股票状态信号']:
                try:
                    with shared_data['股票状态信号'][stock_code]['股票状态'].get_lock():
                        stock_status = shared_data['股票状态信号'][stock_code][
                            '股票状态'].value

                    # # JUST FOR TESTING WITHOUT MULTIPROCESSING LOCK
                    # stock_status = shared_data['股票状态信号'][stock_code]['股票状态']

                    if stock_status == StockLimitStatusInt.LIMIT_UP:
                        limit_up_stocks.add(stock_code)
                        limit_up_info[stock_code] = int(
                            time_str.rstrip(',').split(',')[0])
                except:
                    pass

        return limit_up_stocks, limit_up_info

    def _calculate_sector_metrics_batch(
            self,
            tick_df: pd.DataFrame,
            sector_mapping: Dict,
            limit_up_stocks: Set[str],
            limit_up_info: Dict,
            exclude_sectors: List[str] = None) -> pd.DataFrame:
        """
        批量计算所有板块的指标
        
        Args:
            tick_df: tick数据DataFrame
            sector_mapping: 板块映射字典
            limit_up_stocks: 涨停股票集合
            limit_up_info: 涨停信息字典
            exclude_sectors: 需要排除的板块列表
            
        Returns:
            板块指标DataFrame
        """
        if exclude_sectors is None:
            exclude_sectors = []

        results = []

        # 为tick_df添加不含后缀的股票代码列，用于快速查找
        tick_df['股票代码_无后缀'] = tick_df['股票代码'].str[:-3]

        for sector_code, sector_info in sector_mapping.items():
            if sector_code in exclude_sectors:
                continue

            sector_stocks = sector_info.get('stocks', [])
            if not sector_stocks:
                continue

            # 筛选该板块的tick数据
            sector_tick = tick_df[tick_df['股票代码_无后缀'].isin(sector_stocks)]

            if sector_tick.empty:
                continue

            # 计算涨停相关指标
            sector_stocks_with_suffix = self._add_stock_suffix(sector_stocks)
            limit_up_in_sector = limit_up_stocks & set(
                sector_stocks_with_suffix)

            # 找出第一个涨停的股票
            first_limit_time = None
            leader_code = None
            leader_name = None
            leader_change = None

            if limit_up_in_sector:
                # 从涨停信息中找出最早的涨停时间
                min_time = None
                for stock in limit_up_in_sector:
                    if stock in limit_up_info:
                        limit_time = limit_up_info[stock]
                        if limit_time and (min_time is None
                                           or limit_time < min_time):
                            min_time = limit_time
                            first_limit_time = limit_time
                            # 获取该股票的信息
                            stock_row = sector_tick[sector_tick['股票代码'] ==
                                                    stock]
                            if not stock_row.empty:
                                leader_code = stock[:-3]
                                leader_name = stock_row.iloc[0].get('股票名称', '')
                                leader_change = stock_row.iloc[0]['涨跌幅']

            # 如果没有涨停股，选择涨幅最大的
            if leader_code is None and not sector_tick.empty:
                max_idx = sector_tick['涨跌幅'].idxmax()
                leader_row = sector_tick.loc[max_idx]
                leader_code = leader_row['股票代码_无后缀']
                leader_name = leader_row.get('股票名称', '')
                leader_change = leader_row['涨跌幅']

            # 构建结果
            results.append({
                '板块代码':
                sector_code,
                '板块名称':
                sector_info.get('name', ''),
                '涨跌幅':
                sector_tick['涨跌幅'].mean(),
                '最高涨跌幅':
                sector_tick['涨跌幅'].max(),
                '最低涨跌幅':
                sector_tick['涨跌幅'].min(),
                '中位数涨跌幅':
                sector_tick['涨跌幅'].median(),
                '上涨家数':
                len(sector_tick[sector_tick['涨跌幅'] > 0]),
                '下跌家数':
                len(sector_tick[sector_tick['涨跌幅'] < 0]),
                '平盘家数':
                len(sector_tick[sector_tick['涨跌幅'] == 0]),
                '涨停家数':
                len(limit_up_in_sector),
                '跌停家数':
                len(sector_tick[sector_tick['涨跌幅'] <= -9.9]),
                '领涨股票代码':
                leader_code,
                '领涨股票名称':
                leader_name,
                '领涨股票涨跌幅':
                leader_change,
                '首次涨停时间':
                first_limit_time,
                '成分股数量':
                len(sector_stocks),
                '有效数据数':
                len(sector_tick),
                '总成交额':
                sector_tick['amount'].sum()
                if 'amount' in sector_tick.columns else 0,
                '总成交量':
                sector_tick['volume'].sum()
                if 'volume' in sector_tick.columns else 0,
            })

        return pd.DataFrame(results)

    def update_all_sectors(
            self,
            shared_data: dict,
            force_reload: bool = False) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        一次性更新所有板块数据（概念+行业）
        
        Args:
            shared_data: 共享数据
            force_reload: 是否强制重新加载映射文件
            
        Returns:
            (概念板块DataFrame, 行业板块DataFrame)
        """
        from xtquant import xtdata

        # 加载板块映射（如果需要）
        self._load_sector_mappings(force_reload)

        if not self.concept_mapping and not self.industry_mapping:
            logger.error("板块映射数据为空")
            return None, None

        # 获取涨停股票信息（一次性）
        limit_up_stocks, limit_up_info = self._get_limit_up_stocks(shared_data)
        logger.debug(f"当前涨停股票数: {len(limit_up_stocks)}")

        # 一次性获取所有成分股的tick数据
        stock_pool = self._add_stock_suffix(list(self.all_stocks_set))
        logger.debug(f"获取 {len(stock_pool)} 只股票的tick数据...")

        try:
            tick_dict = run_with_timeout(xtdata.get_full_tick, args=(stock_pool,), timeout=10)
            if not tick_dict:
                logger.error("未能获取tick数据")
                return None, None

            # 转换为DataFrame
            tick_df = pd.DataFrame(tick_dict).T.reset_index(names='股票代码')
            tick_df['涨跌幅'] = (tick_df['lastPrice'] - tick_df['lastClose']
                              ) / tick_df['lastClose'] * 100

            # 添加股票名称（如果shared_data中有）
            if '股票信息' in shared_data:
                stock_info = shared_data['股票信息']
                tick_df['股票名称'] = tick_df['股票代码'].apply(
                    lambda x: stock_info.get(x, {}).get('name', '')
                    if x in stock_info else '')

            logger.info(f"成功获取 {len(tick_df)} 只股票的tick数据")
        except Exception as e:
            logger.error(f"获取tick数据失败: {e}")
            return None, None

        # 批量计算概念板块
        concept_result = None
        if self.concept_mapping:
            exclude_sectors = [
                'BK1051', 'BK0816', 'BK0817', 'BK1050', 'BK0815', 'BK0636'
            ]
            concept_result = self._calculate_sector_metrics_batch(
                tick_df, self.concept_mapping, limit_up_stocks, limit_up_info,
                exclude_sectors)
            if concept_result is not None and not concept_result.empty:
                concept_result = concept_result.sort_values('涨跌幅',
                                                            ascending=False)
                logger.info(f"计算完成: {len(concept_result)} 个概念板块")

        # 批量计算行业板块
        industry_result = None
        if self.industry_mapping:
            industry_result = self._calculate_sector_metrics_batch(
                tick_df, self.industry_mapping, limit_up_stocks, limit_up_info)
            if industry_result is not None and not industry_result.empty:
                industry_result = industry_result.sort_values('涨跌幅',
                                                              ascending=False)
                logger.info(f"计算完成: {len(industry_result)} 个行业板块")

        return concept_result, industry_result


# 全局监控器实例（单例模式）
_sector_monitor = None
_sector_monitor_data_source = None


def get_sector_monitor(data_source: str = None) -> SectorMonitorOptimized:
    """
    获取全局板块监控器实例（单例）
    
    Args:
        data_source: 数据源，'THS' (同花顺) 或 'EM' (东方财富)
                    如果为None，则使用已有实例或默认创建THS实例
    """
    global _sector_monitor, _sector_monitor_data_source
    
    # 如果指定了数据源且与当前不同，需要重新创建实例
    if data_source and data_source != _sector_monitor_data_source:
        _sector_monitor = None
        _sector_monitor_data_source = data_source
    
    # 如果没有实例，创建新实例
    if _sector_monitor is None:
        # 使用指定的数据源或默认THS
        source = _sector_monitor_data_source or 'THS'
        _sector_monitor = SectorMonitorOptimized(data_source=source)
        _sector_monitor_data_source = source
        
    return _sector_monitor


def _process_sector_callback(df_sectors: pd.DataFrame, shared_data: dict,
                             sector_type: str):
    """处理板块数据的通用回调逻辑"""
    sector_name = "概念板块" if sector_type == 'concept' else "行业板块"
    sector_effect_key = f'{sector_name}效应'
    sector_mapping_key = sector_name

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.debug(f"🔄 [{current_time}] \"{sector_name}\"数据更新（基于Tick计算）")

    # 1. 板块涨跌幅需大于1.5%
    df_sectors = df_sectors.loc[df_sectors['涨跌幅'] >= 1.5]

    if len(df_sectors.loc[df_sectors['涨跌幅'] >= 2]) <= 10:
        df_sectors = df_sectors.head(10)
    else:
        df_sectors = df_sectors.loc[df_sectors['涨跌幅'] >= 2]

    # 显示前10个板块
    logger.debug(f"📊 \"{sector_name}\"列表（强势）：")
    for idx, (_, sector) in enumerate(df_sectors.iterrows(), 1):
        change_icon = "📈" if sector['涨跌幅'] > 0 else "📉"
        logger.debug(f"   {idx}. {sector['板块名称']:12} "
                     f"{change_icon} {sector['涨跌幅']:+6.2f}% "
                     f"涨停: {sector.get('涨停家数', 0)}家")

    # 板块效应字典
    sector_effect = {}
    strong_sectors = set(df_sectors['板块代码'].tolist())

    # 更新板块效应（与原逻辑保持一致）
    if sector_mapping_key in shared_data:
        for stock_code in shared_data[sector_mapping_key]:
            sector_code_list = list(
                set(shared_data[sector_mapping_key][stock_code])
                & strong_sectors)
            if len(sector_code_list) > 0:
                # 添加股票后缀
                if stock_code.startswith('6'):
                    stock_with_suffix = f"{stock_code}.SH"
                elif stock_code.startswith('0') or stock_code.startswith('3'):
                    stock_with_suffix = f"{stock_code}.SZ"
                else:
                    stock_with_suffix = f"{stock_code}.BJ"

                sector_effect[stock_with_suffix] = df_sectors.loc[
                    df_sectors['板块代码'].isin(sector_code_list)][[
                        '板块代码', '板块名称', '涨跌幅', '上涨家数', '下跌家数', '涨停家数', '领涨股票代码'
                    ]].to_json(orient='records', force_ascii=False)

    logger.info(f"有{len(sector_effect)}只股票受强势{sector_name}影响")

    # 更新共享字典
    if sector_effect_key in shared_data:
        shared_data[sector_effect_key].clear()
        shared_data[sector_effect_key].update(sector_effect)

    # 更新时间戳
    timestamp = time.time()
    if sector_name == "概念板块":
        with shared_data['概念板块更新时间'].get_lock():
            shared_data['概念板块更新时间'].value = timestamp
    else:
        with shared_data['行业板块更新时间'].get_lock():
            shared_data['行业板块更新时间'].value = timestamp


def concept_sector_data_callback(df_sectors: pd.DataFrame, shared_data: dict):
    """概念板块数据更新回调"""
    _process_sector_callback(df_sectors, shared_data, 'concept')


def industry_sector_data_callback(df_sectors: pd.DataFrame, shared_data: dict):
    """行业板块数据更新回调"""
    _process_sector_callback(df_sectors, shared_data, 'industry')


def monitor_sectors_optimized(shared_data: dict, force_reload: bool = False,
                             data_source: str = None):
    """
    优化的板块监控函数 - 一次性更新概念和行业板块
    
    Args:
        shared_data: 共享数据字典
        force_reload: 是否强制重新加载映射文件
        data_source: 数据源，'THS' (同花顺) 或 'EM' (东方财富)，默认使用THS
    """
    monitor = get_sector_monitor(data_source)

    # 一次性更新所有板块
    concept_result, industry_result = monitor.update_all_sectors(
        shared_data, force_reload)

    # 调用回调函数处理数据
    if concept_result is not None and not concept_result.empty:
        concept_sector_data_callback(concept_result, shared_data)

    if industry_result is not None and not industry_result.empty:
        industry_sector_data_callback(industry_result, shared_data)

    return concept_result, industry_result


# 向后兼容的函数（保留旧接口）
def monitor_sectors_by_tick(shared_data: dict, sector_type: str = 'concept'):
    """
    向后兼容的接口 - 已废弃，请使用 monitor_sectors_optimized
    """
    logger.warning("monitor_sectors_by_tick 已废弃，请使用 monitor_sectors_optimized")
    monitor_sectors_optimized(shared_data, force_reload=False)


if __name__ == "__main__":
    # 测试代码
    from multiprocessing import Manager

    # 创建测试用的共享数据
    manager = Manager()
    test_shared_data = {
        '概念板块': manager.dict(),
        '概念板块成分股': manager.dict(),
        '概念板块效应': manager.dict(),
        '行业板块': manager.dict(),
        '行业板块成分股': manager.dict(),
        '行业板块效应': manager.dict(),
        '涨停池': manager.dict(),
        '股票状态信号': manager.dict(),
        '股票信息': manager.dict(),
    }

    # 测试优化后的监控
    monitor_sectors_optimized(test_shared_data)
