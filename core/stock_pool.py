"""
core/stock_pool.py - 股票池初始化与股票代码处理

从 打板策略_v2.4.py 提取的 init_stock_pool() 和 add_stock_code_suffix() 函数。
"""

import os
import time
import traceback
from functools import partial
from datetime import datetime

import numpy as np
import pandas as pd
from tqdm import tqdm
from loguru import logger

from config import TODAY, SHOULD_DOWNLOAD_KLINE
from infra.data_helpers import _round_price
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
        logger.warning(f'股票代码有误: {code}')
        return code


def init_stock_pool(end_date):
    try:
        from xtquant import xtdata
        import akshare as ak

        try:
            # 获取流通股本
            stock_hold_change_cninfo_df = ak.stock_hold_change_cninfo(
                symbol="全部")
            stock_hold_change_cninfo_df = stock_hold_change_cninfo_df[[
                '证券代码', '已流通股份'
            ]].copy()
            float_volume_dict = stock_hold_change_cninfo_df.set_index(
                '证券代码')['已流通股份'].to_dict()
        except Exception as e:
            logger.exception(f'获取流通股本失败: {e}')
            send_email('【发现错误】获取流通股本失败',
                       f'获取流通股本时发生异常: {e}\n{traceback.format_exc()}')
            float_volume_dict = {}

        # 下载板块分类信息
        logger.info('开始下载板块分类信息...')
        xtdata.download_sector_data()
        logger.info('下载完成')

        # 获取全量A股
        stock_pool = xtdata.get_stock_list_in_sector('沪深A股')  # 全部A股
        # 股票信息字典
        stock_info_dict = {}

        # 去掉科创板
        stock_pool = [
            stock for stock in stock_pool if not stock.startswith('68')
        ]

        # 去掉北京交易所
        stock_pool = [
            stock for stock in stock_pool if not stock.endswith('.BJ')
        ]
    except Exception as e:
        logger.exception(f'【关键错误】初始化股票池失败: {e}')
        send_email('【关键错误】初始化股票池失败',
                   f'初始化股票池时发生异常: {e}\n{traceback.format_exc()}')
        raise e

    # ------------------------------- 去掉当日停牌和ST的股票 ------------------------------- #
    invalid_stock_list = []
    # 新股列表
    new_stock_list = []
    for stock_code in stock_pool:
        try:
            stock_info = xtdata.get_instrument_detail(stock_code,
                                                      iscomplete=False)
            if stock_info['InstrumentStatus'] > 0:
                '''停牌标记
                0 - 正常
                1 - 停牌
                -1 - 当日起复牌
                >1 - 停牌N天
                '''
                logger.debug(
                    f'{stock_code} 停牌 {stock_info["InstrumentStatus"]} {stock_info["InstrumentName"]}'
                )
                # 停牌
                invalid_stock_list.append(stock_code)
                continue
            if 'st' in stock_info['InstrumentName'].lower():
                # ST股
                invalid_stock_list.append(stock_code)
                continue
            if stock_info['OpenDate'] == '19700101' or stock_info[
                    'OpenDate'] >= TODAY:
                stock_name = stock_info["InstrumentName"]
                # 未上市/新上市
                logger.debug(f'【未上市/新上市】 {stock_code} {stock_name}')
                invalid_stock_list.append(stock_code)

                # --------------------------------- 识别新股列表 --------------------------------- #
                # 新股定义：上市时间不足5个交易日的股票，股票名称通常以"N"或"C"开头
                # N: 首日上市
                # C: 第2-5个交易日
                if stock_name and (stock_name.startswith('N')
                                   or stock_name.startswith('C')):
                    new_stock_list.append(stock_code)
                    logger.debug(f'【新股识别】 {stock_code} {stock_name}')
                continue
            # 过滤掉上市时间小于100天的股票
            if (datetime.strptime(TODAY, '%Y%m%d') - datetime.strptime(
                    stock_info['OpenDate'], '%Y%m%d')).days < 100:
                logger.debug(
                    f'【上市时间小于100天】 {stock_code} {stock_info["InstrumentName"]}'
                )
                invalid_stock_list.append(stock_code)
                continue

            # 去掉股价小于2的股票
            if stock_info['PreClose'] < 2:
                logger.debug(
                    f'【股价小于2】 {stock_code} {stock_info["InstrumentName"]}')
                invalid_stock_list.append(stock_code)
                continue

            stock_info_dict[stock_code] = {}
            stock_info_dict[stock_code]['涨停价'] = _round_price(
                stock_info['UpStopPrice'])
            stock_info_dict[stock_code]['跌停价'] = _round_price(
                stock_info['DownStopPrice'])
            if not stock_info['FloatVolume']:
                if stock_info['InstrumentID'] not in float_volume_dict:
                    logger.error(f'{stock_code} 流通股本无法获取，且在备用数据中也没有找到，请检查数据源。')
                stock_info_dict[stock_code]['流通股本'] = float_volume_dict.get(
                    stock_info['InstrumentID'], 0) * 10000  # 转换为股
            else:
                stock_info_dict[stock_code]['流通股本'] = stock_info['FloatVolume']
            stock_info_dict[stock_code]['股票名称'] = stock_info['InstrumentName']
            stock_info_dict[stock_code]['昨日收盘价'] = stock_info['PreClose']

            # ----------------------------------- 数据检验 ----------------------------------- #
            if not stock_info_dict[stock_code]['涨停价'] or not stock_info_dict[
                    stock_code]['跌停价'] or not stock_info_dict[stock_code][
                        '流通股本'] or not stock_info_dict[stock_code]['昨日收盘价']:
                logger.error(
                    f'{stock_code} 数据不完整，请检查数据源。{stock_info_dict[stock_code]}')
        except Exception as e:
            logger.exception(f'处理股票 {stock_code} 信息失败: {e}')
            # 将出错的股票加入无效列表，继续处理其他股票
            invalid_stock_list.append(stock_code)
            continue

    stock_pool = [
        stock for stock in stock_pool if stock not in invalid_stock_list
    ]

    logger.info(f'日期：{TODAY}，股票池大小：{len(stock_pool)}')

    # ---------------------------- 根据历史N日K线计算更多数据 --------------------------- #
    N = 60
    done = [False]

    def _on_data(data, task, done):
        # 使用tqdm展示数据下载进度条
        if not hasattr(_on_data, 'pbar'):
            # 首次调用时创建进度条对象，设置总数和描述
            _on_data.pbar = tqdm(total=data['total'], desc=f"【{task}】")

        # 更新进度，只增加尚未显示的进度部分
        current_progress = data['finished']
        _on_data.pbar.update(current_progress - _on_data.pbar.n)

        # 检查下载是否完成
        if data['total'] == data['finished']:
            # 关闭进度条
            _on_data.pbar.close()
            # 清理进度条对象，释放内存
            delattr(_on_data, 'pbar')
            # 设置完成标志
            done[0] = True

    try:
        # ------------------------ 判断强势股票文件是否存在，间接判断是否需要下载K线数据 ----------------------- #
        output_path = os.path.join('output', '强势股票', f'强势股票_{TODAY}.csv')
        have_strong_stocks = os.path.exists(output_path)

        if SHOULD_DOWNLOAD_KLINE and not have_strong_stocks:
            on_data_1d = partial(_on_data, task='日线数据下载', done=done)
            logger.info(f'日期：{TODAY}，开始下载日线数据，N={N}')
            xtdata.download_history_data2(stock_pool,
                                          period='1d',
                                          end_time=TODAY,
                                          callback=on_data_1d,
                                          incrementally=True)
            while not done[0]:
                time.sleep(1)
            logger.info('日线数据下载完成')

        # ---------------------------------- 计算60日均线 --------------------------------- #
        data = xtdata.get_market_data(field_list=['close'],
                                      stock_list=stock_pool,
                                      period='1d',
                                      start_time='',
                                      end_time=end_date,
                                      count=N,
                                      dividend_type='none',
                                      fill_data=True)

        m60_price = (data['close'].mean(axis=1)).to_dict()

        # --------------------------------- 计算5日平均成交量 -------------------------------- #
        data = xtdata.get_market_data(field_list=['volume', 'amount'],
                                      stock_list=stock_pool,
                                      period='1d',
                                      start_time='',
                                      end_time=end_date,
                                      count=5,
                                      dividend_type='none',
                                      fill_data=True)
        m5_volume = (data['volume'].mean(axis=1)).to_dict()
        m5_amount = (data['amount'].mean(axis=1)).to_dict()
    except Exception as e:
        logger.exception(f'【关键错误】获取历史数据失败: {e}')
        send_email('【关键错误】获取历史数据失败',
                   f'获取历史数据时发生异常: {e}\n{traceback.format_exc()}')
        raise e

    # --------------------------------- 将60日均线和5日平均成交量添加到股票信息字典 -------------------------------- #
    try:
        for stock_code in stock_info_dict:
            try:
                if stock_code in m60_price:
                    stock_info_dict[stock_code]['60日均线'] = m60_price[
                        stock_code]
                else:
                    stock_info_dict[stock_code][
                        '60日均线'] = 1e5  # 设置为一个很大的数，表示无法获取60日均线
                    logger.warning(f'{stock_code} 无法获取60日均线，设置为1e5')

                if stock_code in m5_volume:
                    stock_info_dict[stock_code]['5日平均成交量'] = m5_volume[
                        stock_code]
                else:
                    stock_info_dict[stock_code][
                        '5日平均成交量'] = 0  # 设置为0，表示无法获取5日平均成交量
                    logger.warning(f'{stock_code} 无法获取5日平均成交量，设置为0')

                if stock_code in m5_amount:
                    stock_info_dict[stock_code]['5日平均成交额'] = m5_amount[
                        stock_code]
                else:
                    stock_info_dict[stock_code][
                        '5日平均成交额'] = 0  # 设置为0，表示无法获取5日平均成交额
                    logger.warning(f'{stock_code} 无法获取5日平均成交额，设置为0')
            except Exception as e:
                logger.exception(f'处理股票 {stock_code} 历史数据失败: {e}')
                # 设置默认值确保程序能继续
                stock_info_dict[stock_code]['60日均线'] = 1e5
                stock_info_dict[stock_code]['5日平均成交量'] = 0
                stock_info_dict[stock_code]['5日平均成交额'] = 0

        logger.info(f'识别到 {len(new_stock_list)} 只新股（上市不足5日）')

        return stock_pool, stock_info_dict, new_stock_list
    except Exception as e:
        logger.exception(f'【关键错误】完成股票信息字典构建失败: {e}')
        send_email('【关键错误】完成股票信息字典构建失败',
                   f'完成股票信息字典构建时发生异常: {e}\n{traceback.format_exc()}')
        raise e
