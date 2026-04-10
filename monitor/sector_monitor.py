"""
monitor/sector_monitor.py - 板块监控模块

从 打板策略_v2.4.py 提取的板块/行业/资金流监控相关函数。
包含：
- concept_sector_data_callback: 概念板块数据更新回调
- industry_sector_data_callback: 行业板块数据更新回调
- stock_capital_flow_callback: 个股资金流数据更新回调
- sector_and_capitalflow_monitor_task: 板块和资金流监控任务
- ths_monitor_task: 同花顺数据监控任务
"""

import time
import traceback
from functools import partial
from datetime import datetime

import pandas as pd
from loguru import logger

from config import (STOP_TIME, SECTOR_DATA_SOURCE,
                    WATCHLIST_RELEASE_MINUTES)
from infra.common_enums import StockLimitStatusInt
from infra.data_helpers import is_trading_time
from infra.utils import send_email


def add_stock_code_suffix(code):
    """
    为股票代码添加交易所后缀

    Args:
        code (str): 原始股票代码，如'600000'

    Returns:
        str: 添加后缀的股票代码，如'600000.SH'
    """
    if not isinstance(code, str):
        raise ValueError(f"股票代码必须是字符串类型，当前输入: {code}")

    SH_PREFIXES = ('6', '900')  # 上交所: 主板、科创板、B股
    SZ_PREFIXES = ('0', '3', '200')  # 深交所: 主板、创业板、B股
    BJ_PREFIXES = ('8', '920')  # 北交所

    if any(code.startswith(prefix) for prefix in SH_PREFIXES):
        return f"{code}.SH"
    elif any(code.startswith(prefix) for prefix in SZ_PREFIXES):
        return f"{code}.SZ"
    elif any(code.startswith(prefix) for prefix in BJ_PREFIXES):
        return f"{code}.BJ"
    else:
        raise ValueError(f"无法识别的股票代码前缀: {code}")


def concept_sector_data_callback(df_sectors: pd.DataFrame, shared_data: dict):
    """板块数据更新回调"""
    if not is_trading_time():
        logger.debug("当前不在交易时间，跳过概念板块数据更新")
        return

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.debug(f"🔄 [{current_time}] \"概念板块\"数据更新")

    # 去除掉昨日涨停等板块指数，以获取真实概念排名
    # 1. BK1051 昨日连板_含一字
    # 2. BK0816	昨日连板
    # 3. BK0817	昨日触板
    # 4. BK1050	昨日涨停_含一字
    # 5. BK0815	昨日涨停
    df_sectors = df_sectors.loc[~df_sectors['板块代码'].isin(
        ['BK1051', 'BK0816', 'BK0817', 'BK1050', 'BK0815'])].sort_values(
            by='涨跌幅', ascending=False)

    # 1. 板块涨跌幅需大于1.5%
    df_sectors = df_sectors.loc[df_sectors['涨跌幅'] >= 1.5]

    # # 2. 上涨家数大于下跌家数
    # df_sectors = df_sectors.loc[df_sectors['上涨家数'] >= df_sectors['下跌家数']]

    # # TODO: 3. 涨停家数大于1？

    if len(df_sectors.loc[df_sectors['涨跌幅'] >= 2]) <= 10:
        df_sectors = df_sectors.head(10)  # 取前10个板块
    else:
        df_sectors = df_sectors.loc[df_sectors['涨跌幅'] >= 2]

    # 显示前10个板块
    logger.debug(f"📊 \"概念板块\"列表（强势）：")
    for idx, (_, sector) in enumerate(df_sectors.iterrows(), 1):
        change_icon = "📈" if sector['涨跌幅'] > 0 else "📉"
        logger.debug(f"   {idx}. {sector['板块名称']:12} "
                     f"{change_icon} {sector['涨跌幅']:+6.2f}% "
                     f"最新价: {sector.get('最新价', 0):>8.2f}")

    # TODO: 这里直接使用 xtquant 获取日线数据计算使用。

    # 实际涨停池
    real_limit_up_pool = []
    for stock_code in shared_data['涨停池'].keys():
        with shared_data['股票状态信号'][stock_code]['股票状态'].get_lock():
            stock_status_value = shared_data['股票状态信号'][stock_code][
                '股票状态'].value
        if stock_status_value == StockLimitStatusInt.LIMIT_UP:
            real_limit_up_pool.append(stock_code[:-3])  # 去掉后缀.SH或.SZ

    logger.debug(f"实际涨停池: {real_limit_up_pool}")

    # 计算各板块涨停家数
    df_sectors['涨停家数'] = df_sectors['板块代码'].apply(
        lambda x: 0 if x not in shared_data['概念板块成分股'] else len(
            set(shared_data['概念板块成分股'][x]) & set(real_limit_up_pool)))

    # 板块效应字典
    sector_effect = {}
    # 强势板块代码集合
    strong_sectors = set(df_sectors['板块代码'].tolist())
    for stock_code in shared_data['概念板块']:
        sector_code_list = list(
            set(shared_data['概念板块'][stock_code]) & strong_sectors)
        if len(sector_code_list) > 0:
            sector_effect[add_stock_code_suffix(stock_code)] = df_sectors.loc[
                df_sectors['板块代码'].isin(sector_code_list)][[
                    '板块代码', '板块名称', '涨跌幅', '上涨家数', '下跌家数', '涨停家数', '领涨股票代码'
                ]].to_json(orient='records', force_ascii=False)

    logger.info(f"有{len(sector_effect)}只股票受强势板块影响")

    # 更新共享字典
    shared_data['概念板块效应'].clear()
    shared_data['概念板块效应'].update(sector_effect)

    # 更新时间戳
    timestamp = time.time()
    with shared_data['概念板块更新时间'].get_lock():
        shared_data['概念板块更新时间'].value = timestamp
    logger.debug(
        f"✅ 概念板块更新时间已设置: {timestamp} ({datetime.fromtimestamp(timestamp).isoformat()})"
    )


def industry_sector_data_callback(df_sectors: pd.DataFrame, shared_data: dict):
    """板块数据更新回调"""
    if not is_trading_time():
        logger.debug("当前不在交易时间，跳过行业板块数据更新")
        return

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.debug(f"🔄 [{current_time}] \"行业板块\"数据更新")

    df_sectors = df_sectors.sort_values(by='涨跌幅', ascending=False)

    # 1. 板块涨跌幅需大于1.5%
    df_sectors = df_sectors.loc[df_sectors['涨跌幅'] >= 1.5]

    # # 2. 上涨家数大于下跌家数
    # df_sectors = df_sectors.loc[df_sectors['上涨家数'] >= df_sectors['下跌家数']]

    # # TODO: 3. 涨停家数大于1？

    if len(df_sectors.loc[df_sectors['涨跌幅'] >= 2]) <= 10:
        df_sectors = df_sectors.head(10)  # 取前10个板块
    else:
        df_sectors = df_sectors.loc[df_sectors['涨跌幅'] >= 2]

    # 显示前10个板块
    logger.debug(f"📊 \"行业板块\"列表（强势）：")
    for idx, (_, sector) in enumerate(df_sectors.iterrows(), 1):
        change_icon = "📈" if sector['涨跌幅'] > 0 else "📉"
        logger.debug(f"   {idx}. {sector['板块名称']:12} "
                     f"{change_icon} {sector['涨跌幅']:+6.2f}% "
                     f"最新价: {sector.get('最新价', 0):>8.2f}")

    # TODO: 这里直接使用 xtquant 获取日线数据计算使用。

    # 实际涨停池
    real_limit_up_pool = []
    for stock_code in shared_data['涨停池'].keys():
        with shared_data['股票状态信号'][stock_code]['股票状态'].get_lock():
            stock_status_value = shared_data['股票状态信号'][stock_code][
                '股票状态'].value
        if stock_status_value == StockLimitStatusInt.LIMIT_UP:
            real_limit_up_pool.append(stock_code[:-3])  # 去掉后缀.SH或.SZ

    # 计算各板块涨停家数
    df_sectors['涨停家数'] = df_sectors['板块代码'].apply(
        lambda x: 0 if x not in shared_data['行业板块成分股'] else len(
            set(shared_data['行业板块成分股'][x]) & set(real_limit_up_pool)))

    # 板块效应字典
    sector_effect = {}
    # 强势板块代码集合
    strong_sectors = set(df_sectors['板块代码'].tolist())
    for stock_code in shared_data['行业板块']:
        sector_code_list = list(
            set(shared_data['行业板块'][stock_code]) & strong_sectors)
        if len(sector_code_list) > 0:
            sector_effect[add_stock_code_suffix(stock_code)] = df_sectors.loc[
                df_sectors['板块代码'].isin(sector_code_list)][[
                    '板块代码', '板块名称', '涨跌幅', '上涨家数', '下跌家数', '涨停家数', '领涨股票代码'
                ]].to_json(orient='records', force_ascii=False)

    logger.info(f"有{len(sector_effect)}只股票受强势板块影响")

    # 更新共享字典
    shared_data['行业板块效应'].clear()
    shared_data['行业板块效应'].update(sector_effect)

    # 更新时间戳
    timestamp = time.time()
    with shared_data['行业板块更新时间'].get_lock():
        shared_data['行业板块更新时间'].value = timestamp


def stock_capital_flow_callback(df_stock: pd.DataFrame, shared_data: dict):
    """个股资金流数据更新回调"""
    if not is_trading_time():
        logger.debug("当前不在交易时间，跳过个股资金流数据更新")
        return

    current_time = datetime.now().strftime("%H:%M:%S")
    logger.debug(f"💰 [{current_time}] 个股资金流更新：{len(df_stock)} 只股票")

    logger.debug(f'\n{df_stock.head()}')
    if df_stock.empty or df_stock['主力净流入'].isna().any():
        logger.warning("⚠️ 个股资金流数据异常，跳过处理")
        return

    # 大额流入统计
    large_inflow_count = len(df_stock[df_stock['主力净流入'] > 10000])
    logger.debug(f"   💸 大额流入(>1亿)：{large_inflow_count} 只")

    # 保存完整数据用于报告显示（保留原始DataFrame的关键列）
    df_for_report = df_stock[[
        '股票代码', '股票名称', '最新价', '涨跌幅', '主力净流入', '主力净流入占比', '成交额'
    ]].copy()

    # 更新Manager.list() - 使用切片赋值替换整个列表内容
    shared_data['个股资金流入_原始数据'][:] = df_for_report.to_dict('records')

    df_stock = df_stock.loc[df_stock['涨跌幅'] > 0]
    stock_money_flow_dict = {}
    for _, row in df_stock.iterrows():
        msg = ''
        if row['主力净流入'] > 5000 and row['主力净流入占比'] > 0.1:
            msg += f'主力净流入: {row["主力净流入"]}, 主力净流入占比: {row["主力净流入占比"]}\t'

        if row['超大单净流入'] > 3000 and row['超大单净流入占比'] > 0.05:
            msg += f'超大单净流入: {row["超大单净流入"]}, 超大单净流入占比: {row["超大单净流入占比"]}\t'

        if msg:
            stock_money_flow_dict[add_stock_code_suffix(row['股票代码'])] = msg
            logger.debug(
                f'[{row["股票名称"]}] {row["股票代码"]} 最新价: {row["最新价"]}, 涨跌幅: {row["涨跌幅"]}, 主力净流入: {row["主力净流入"]}, 主力净流入占比: {row["主力净流入占比"]}, 超大单净流入: {row["超大单净流入"]}, 超大单净流入占比: {row["超大单净流入占比"]}'
            )

    shared_data['个股资金流入'].clear()
    shared_data['个股资金流入'].update(stock_money_flow_dict)

    # 更新时间戳
    timestamp = time.time()
    with shared_data['个股资金流入更新时间'].get_lock():
        shared_data['个股资金流入更新时间'].value = timestamp


def sector_and_capitalflow_monitor_task(shared_data):
    """东方财富数据监控任务 - 使用Tick数据计算板块行情（优化版）"""
    try:
        from standalone.sector_monitor_tick_based import monitor_sectors_optimized
        from scraper.stock_capital_flow_monitor import StockCapitalFlowMonitor

        logger.info(f'数据监控任务已启动（板块数据基于Tick计算 - 优化版）...')

        # 创建个股资金流监控器（保留原有的资金流监控）
        stock_monitor = StockCapitalFlowMonitor(max_pages=2)

        # 设置个股资金流回调
        partial_stock_capital_flow_callback = partial(
            stock_capital_flow_callback, shared_data=shared_data)
        partial_stock_capital_flow_callback.__name__ = "个股资金流数据回调"
        stock_monitor.set_callback(partial_stock_capital_flow_callback)

        try:
            # 启动个股资金流监控器
            stock_monitor.start(interval=60)
            logger.info("🔄 个股资金流监控器已启动")

            # 板块监控的更新间隔（秒）
            sector_update_interval = 10
            last_sector_update = 0
            last_watchlist_check = 0

            # 主循环
            while True:
                current_time = time.time()

                # U5升级：定期检查观察名单自动解除（每60秒检查一次）
                if current_time - last_watchlist_check >= 60:
                    last_watchlist_check = current_time
                    try:
                        watch_list = shared_data.get('观察名单', {})
                        for code in list(watch_list.keys()):
                            entry = watch_list[code]
                            # 解析 "turnover%|timestamp" 格式
                            parts = entry.split('|')
                            if len(parts) == 2:
                                add_time = float(parts[1])
                                elapsed_min = (current_time - add_time) / 60
                                if elapsed_min >= WATCHLIST_RELEASE_MINUTES:
                                    del watch_list[code]
                                    logger.info(
                                        f'[观察名单解除] {code} 已超过{WATCHLIST_RELEASE_MINUTES}分钟，自动移出观察名单'
                                    )
                    except Exception as e:
                        logger.debug(f'观察名单检查异常: {e}')

                # 检查是否需要更新板块数据
                if current_time - last_sector_update >= sector_update_interval:
                    if is_trading_time():
                        try:
                            last_sector_update = current_time
                            # 一次性更新概念和行业板块（优化：避免重复加载文件和获取tick数据）
                            logger.debug(
                                f"📊 开始计算板块数据（概念+行业）[数据源: {SECTOR_DATA_SOURCE}]..."
                            )
                            monitor_sectors_optimized(
                                shared_data,
                                force_reload=False,
                                data_source=SECTOR_DATA_SOURCE)

                            next_update_in = sector_update_interval - (
                                time.time() - last_sector_update)
                            logger.debug(
                                f"✅ 板块数据更新完成，下次更新时间：{next_update_in if next_update_in > 0 else 0}秒后"
                            )

                        except Exception as e:
                            logger.error(f"板块数据更新失败: {e}")

                # 检查是否到达停止时间
                if datetime.now().time() >= STOP_TIME:
                    logger.warning('【进程退出】数据监控任务')
                    return

                # 短暂休眠，避免CPU占用过高
                time.sleep(1)

        except KeyboardInterrupt:
            logger.warning(f"⚠️ 用户中断，正在停止监控器...")
        finally:
            stock_monitor.stop()
            logger.warning(f"✅ 监控器已停止")

    except Exception as e:
        logger.exception(f"【关键错误】数据监控任务失败: {e}")
        send_email('【关键错误】数据监控任务失败',
                   f'数据监控任务时发生异常: {e}\n{traceback.format_exc()}')


def ths_monitor_task(shared_data):
    """同花顺数据监控任务"""
    from xtquant import xtdata
    try:
        if not is_trading_time():
            logger.debug("当前不在交易时间，跳过同花顺数据监控任务")
            return

        yesterday_first_limit_up_stocks = shared_data['昨日首板股票']
        yesterday_limit_up_stocks = shared_data['昨日涨停股票']

        # 获取最新tick
        data = xtdata.get_full_tick(
            list(
                set(yesterday_limit_up_stocks)
                | set(yesterday_first_limit_up_stocks)))
        data = pd.DataFrame(data).T.reset_index(names='股票代码')
        data['涨跌幅'] = (data['lastPrice'] -
                       data['lastClose']) / data['lastClose'] * 100

        with shared_data['市场情绪_昨日首板表现'].get_lock():
            shared_data['市场情绪_昨日首板表现'].value = data[data['股票代码'].isin(
                yesterday_first_limit_up_stocks)]['涨跌幅'].mean()
        with shared_data['市场情绪_昨日涨停表现'].get_lock():
            shared_data['市场情绪_昨日涨停表现'].value = data[data['股票代码'].isin(
                yesterday_limit_up_stocks)]['涨跌幅'].mean()

        # 更新昨日涨停表现数据更新时间
        with shared_data['昨日涨停表现更新时间'].get_lock():
            shared_data['昨日涨停表现更新时间'].value = time.time()
    except Exception as e:
        logger.exception(f"【关键错误】同花顺数据监控任务失败: {e}")
        send_email('【关键错误】同花顺数据监控任务失败',
                   f'同花顺数据监控任务时发生异常: {e}\n{traceback.format_exc()}')
