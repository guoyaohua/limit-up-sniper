"""
monitor/sentiment.py - 市场情绪指标计算

从 打板策略_v2.4.py 提取的市场情绪指标计算核心函数。
"""

import math
import time
import traceback

import pandas as pd
from loguru import logger

from infra.common_enums import StockLimitStatusInt
from infra.data_helpers import is_trading_time
from infra.utils import send_email


def calculate_market_sentiment_metrics(shared_data):
    """计算市场情绪指标的核心函数"""
    from xtquant import xtdata
    try:
        if not is_trading_time():
            logger.debug("当前不在交易时间，跳过市场情绪指标计算")
            return

        # 1. 涨停板数量
        limit_up_pool = shared_data['涨停池'].keys()  # 获取涨停池的股票代码,包括已炸板股票
        real_limit_up_pool = []  # 实际涨停池，排除炸板的股票
        for stock_code in limit_up_pool:
            if stock_code in shared_data['股票状态信号']:
                with shared_data['股票状态信号'][stock_code]['股票状态'].get_lock():
                    stock_status_value = shared_data['股票状态信号'][stock_code][
                        '股票状态'].value
                if stock_status_value == StockLimitStatusInt.LIMIT_UP:
                    real_limit_up_pool.append(stock_code)  # 只统计实际涨停的股票
        with shared_data['市场情绪_涨停板数量'].get_lock():
            shared_data['市场情绪_涨停板数量'].value = len(real_limit_up_pool)
        logger.debug(f"当前全市场涨停板数量: {len(real_limit_up_pool)}")

        # 2. 炸板数量
        limit_break_count = len(limit_up_pool) - len(
            real_limit_up_pool)  # 获取炸板的股票数量
        with shared_data['市场情绪_炸板数量'].get_lock():
            shared_data['市场情绪_炸板数量'].value = limit_break_count
        logger.debug(f"当前全市场炸板数量: {limit_break_count}")

        # 3. 炸板率
        if len(limit_up_pool) > 0:
            limit_break_rate = limit_break_count / len(limit_up_pool)
        else:
            limit_break_rate = 0.0
        with shared_data['市场情绪_炸板率'].get_lock():
            shared_data['市场情绪_炸板率'].value = limit_break_rate
        logger.debug(f"当前全市场炸板率: {limit_break_rate:.2%}")

        # 4. 昨日首板连板率
        yesterday_first_limit_up_stocks = shared_data['昨日首板股票']
        yesterday_first_limit_up_count = 0
        if len(yesterday_first_limit_up_stocks) > 0:
            yesterday_first_limit_up_count = len([
                stock for stock in yesterday_first_limit_up_stocks
                if stock in real_limit_up_pool
            ])
            if yesterday_first_limit_up_count > 0:
                yesterday_first_limit_up_rate = yesterday_first_limit_up_count / len(
                    yesterday_first_limit_up_stocks)
            else:
                yesterday_first_limit_up_rate = 0.0
        else:
            yesterday_first_limit_up_rate = 0.0
        with shared_data['市场情绪_昨日首板连板率'].get_lock():
            shared_data['市场情绪_昨日首板连板率'].value = yesterday_first_limit_up_rate
        with shared_data['市场情绪_昨日首板连板个数'].get_lock():
            shared_data['市场情绪_昨日首板连板个数'].value = yesterday_first_limit_up_count
        logger.debug(
            f"昨日首板连板率: {yesterday_first_limit_up_rate:.2%}, 连板个数: {yesterday_first_limit_up_count}"
        )

        # 5. 连板个数
        yesterday_limit_up_stocks = shared_data['昨日涨停股票']
        yesterday_limit_up_count = 0
        if len(yesterday_limit_up_stocks) > 0:
            yesterday_limit_up_count = len([
                stock for stock in yesterday_limit_up_stocks
                if stock in real_limit_up_pool
            ])
            if yesterday_limit_up_count > 0:
                yesterday_limit_up_rate = yesterday_limit_up_count / len(
                    yesterday_limit_up_stocks)
            else:
                yesterday_limit_up_rate = 0.0
        else:
            yesterday_limit_up_rate = 0.0
        with shared_data['市场情绪_昨日涨停连板率'].get_lock():
            shared_data['市场情绪_昨日涨停连板率'].value = yesterday_limit_up_rate
        with shared_data['市场情绪_昨日涨停连板个数'].get_lock():
            shared_data['市场情绪_昨日涨停连板个数'].value = yesterday_limit_up_count
        logger.debug(
            f"昨日涨停连板率: {yesterday_limit_up_rate:.2%}, 连板个数: {yesterday_limit_up_count}"
        )

        # 6. 大盘指数
        index_codes = ['000001.SH', '000300.SH', '399006.SZ', '399001.SZ']
        raw_ticks = xtdata.get_full_tick(index_codes)
        if not raw_ticks:
            logger.warning('大盘指数行情为空，不刷新指数时间戳')
            return
        data = pd.DataFrame(raw_ticks).T.reset_index(names='股票代码')
        if not {'股票代码', 'lastPrice', 'lastClose'}.issubset(data.columns):
            logger.warning('大盘指数行情字段不完整，不刷新指数时间戳')
            return
        last_price = pd.to_numeric(data['lastPrice'], errors='coerce')
        last_close = pd.to_numeric(data['lastClose'], errors='coerce')
        valid_prices = (
            last_price.map(math.isfinite)
            & last_close.map(math.isfinite)
            & last_price.ge(0)
            & last_close.gt(0)
        )
        if not valid_prices.all():
            logger.warning('大盘指数行情价格无效，不刷新指数时间戳')
            return
        data['涨跌幅'] = (last_price - last_close) / last_close * 100
        values = {}
        for code in index_codes:
            series = data[data['股票代码'] == code]['涨跌幅']
            if series.empty:
                logger.warning(f'大盘指数 {code} 缺失，不刷新指数时间戳')
                return
            value = float(series.iloc[0])
            if not math.isfinite(value):
                logger.warning(f'大盘指数 {code} 非有限值，不刷新指数时间戳')
                return
            values[code] = value

        for shared_key, code in (
                ('上证指数涨跌幅', '000001.SH'),
                ('沪深300涨跌幅', '000300.SH'),
                ('创业板指涨跌幅', '399006.SZ'),
                ('深证成指涨跌幅', '399001.SZ')):
            with shared_data[shared_key].get_lock():
                shared_data[shared_key].value = values[code]

        with shared_data['上证指数涨跌幅'].get_lock():
            sh_val = shared_data['上证指数涨跌幅'].value
        with shared_data['沪深300涨跌幅'].get_lock():
            hs300_val = shared_data['沪深300涨跌幅'].value
        with shared_data['创业板指涨跌幅'].get_lock():
            cyb_val = shared_data['创业板指涨跌幅'].value
        with shared_data['深证成指涨跌幅'].get_lock():
            sz_val = shared_data['深证成指涨跌幅'].value

        # 更新大盘指数数据更新时间
        with shared_data['大盘指数更新时间'].get_lock():
            shared_data['大盘指数更新时间'].value = time.time()

        logger.debug(
            f"上证指数: {sh_val:.2f}%, 沪深300: {hs300_val:.2f}%, 创业板指: {cyb_val:.2f}%, 深证成指: {sz_val:.2f}%"
        )

    except Exception as e:
        logger.exception(f"【关键错误】计算市场情绪指标发生错误: {e}")
        # 对于市场情绪指标的错误，发送邮件通知
        send_email('【关键错误】计算市场情绪指标失败',
                   f'计算市场情绪指标时发生异常: {e}\n{traceback.format_exc()}')


# 在调用 log_market_sentiment_summary 之前，需要确保shared_data中包含必要的配置信息
# 这些信息通常在初始化时设置
def setup_shared_data_config(shared_data):
    """设置共享数据中的配置信息"""
    from config import VERSION, DEBUG_MODE, STRATEGY_NAME
    shared_data['VERSION'] = VERSION
    shared_data['DEBUG_MODE'] = DEBUG_MODE
    shared_data['STRATEGY_NAME'] = STRATEGY_NAME
