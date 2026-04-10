"""
infra/data_helpers.py - 数据/时间/价格工具函数

从 打板策略_v2.4.py 提取的通用工具函数。
"""

import time
import pandas as pd
from datetime import datetime, time as dt_time
from decimal import Decimal, ROUND_HALF_UP
from loguru import logger

from config import IP, PORT, STOP_TIME
from infra.utils import send_email


def xtdata_connect(ip=IP, port=PORT):
    """连接XTQuant数据接口
    Args:
        ip (str): 数据接口IP地址
        port (int): 数据接口端口号
    """
    from xtquant import xtdata
    xtdata.reconnect(ip, port)


def get_pretrade_date(today):
    import akshare as ak

    tool_trade_date_hist_sina_df = ak.tool_trade_date_hist_sina()
    tool_trade_date_hist_sina_df = tool_trade_date_hist_sina_df.sort_values(
        'trade_date', ascending=False)
    tool_trade_date_hist_sina_df['trade_date'] = tool_trade_date_hist_sina_df[
        'trade_date'].map(lambda x: datetime.strftime(x, r'%Y%m%d'))
    tool_trade_date_hist_sina_df[
        'pre_trade_date'] = tool_trade_date_hist_sina_df['trade_date'].shift(
            -1)

    pre_trade_date = tool_trade_date_hist_sina_df.loc[
        tool_trade_date_hist_sina_df['trade_date'] >=
        today].iloc[-1]['pre_trade_date']

    return pre_trade_date


def is_trading_time() -> bool:
    """
    判断当前时间是否为开盘时间

    中国股市交易时间：
    - 上午交易时段：09:30 - 11:30
    - 下午交易时段：13:00 - 15:00

    Returns:
        bool: True表示当前为交易时间，False表示当前为非交易时间
    """
    current_time = datetime.now().time()

    # 定义交易时间段(上午和下午,增加一分钟)
    morning_start = dt_time(9, 29)  # 09:30
    morning_end = dt_time(11, 31)  # 11:30
    afternoon_start = dt_time(12, 59)  # 13:00
    afternoon_end = STOP_TIME  # 15:00

    # 判断是否在交易时间内
    is_morning_session = morning_start <= current_time <= morning_end
    is_afternoon_session = afternoon_start <= current_time <= afternoon_end

    return is_morning_session or is_afternoon_session


def _conv_time(ct, fmt='%Y%m%d%H%M%S'):
    '''
    _conv_time(1476374400000) --> '20161014000000'
    '''
    local_time = time.localtime(ct / 1000)
    data_head = time.strftime(fmt, local_time)
    return data_head


def reconnect_xtdata():
    """重连xtdata"""
    try:
        logger.warning("正在尝试重连 xtdata...")
        xtdata_connect(IP, PORT)
        logger.info("xtdata 重连成功")
    except Exception as e:
        logger.error(f"xtdata 重连失败: {e}")


def _calc_limit_up_break_duration(now: str, limit_break_time: str) -> int:
    """
    计算涨停开板持续时间（秒）
    Args:
        now (str): 当前时间，格式为 'HHMM'，例如 '0930'
        limit_break_time (str): 涨停开板时间，格式为 'HHMM'，例如 '0930'
    Returns:
        int: 涨停开板持续时间（秒）
    """
    if _conv_time(int(now), fmt='%H%M') >= '1300' and _conv_time(
            int(limit_break_time), fmt='%H%M') <= '1130':
        # 如果当前时间在13:00之后，且突破时间在11:30之前，则去掉中午休盘时间，不算做开板时长
        lunch_break_duration = 2 * 3600
        return int(
            (int(now) - int(limit_break_time)) / 1000) - lunch_break_duration
    else:
        return int((int(now) - int(limit_break_time)) / 1000)


def _round_price(price):
    """将价格精确地四舍五入到两位小数（分）。"""
    # 检查输入是否是Pandas Series，如果是，则逐元素处理
    if isinstance(price, pd.Series):
        return price.apply(lambda p: _round_price(p))

    price = round(price, 3)
    price_decimal = Decimal(str(price))
    rounded_price = (price_decimal * Decimal('100')).quantize(
        Decimal('1'), rounding=ROUND_HALF_UP) / Decimal('100')
    return float(rounded_price)


# 性能优化：缓存字典
_price_check_cache = {}
_time_conversion_cache = {}


def _check_same_price(price1, price2):
    """优化版本：添加缓存减少重复计算"""
    # 将价格四舍五入到4位小数作为缓存键
    key = (round(price1, 4), round(price2, 4))
    if key not in _price_check_cache:
        _price_check_cache[key] = abs(price1 - price2) < 0.0001
        # 限制缓存大小，避免内存泄漏
        if len(_price_check_cache) > 10000:
            # 清除一半的缓存
            keys_to_remove = list(_price_check_cache.keys())[:5000]
            for k in keys_to_remove:
                del _price_check_cache[k]
    return _price_check_cache[key]


def _calc_delay_time(time_stamp):
    return round(
        (time.time() - time.mktime(time.localtime(time_stamp / 1000))), 3)


def _conv_time_cached(ct, fmt='%Y%m%d%H%M%S'):
    """优化版本：添加缓存的时间转换函数"""
    key = (ct, fmt)
    if key not in _time_conversion_cache:
        local_time = time.localtime(ct / 1000)
        _time_conversion_cache[key] = time.strftime(fmt, local_time)
        # 限制缓存大小
        if len(_time_conversion_cache) > 5000:
            keys_to_remove = list(_time_conversion_cache.keys())[:2500]
            for k in keys_to_remove:
                del _time_conversion_cache[k]
    return _time_conversion_cache[key]
