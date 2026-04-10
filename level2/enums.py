"""
枚举定义模块

定义系统中使用的所有枚举类型，包括市场类型、委托方向、成交标志等。
"""

from enum import IntEnum, Enum
from decimal import Decimal, ROUND_HALF_UP


class Market(str, Enum):
    """市场类型"""
    SHANGHAI = 'SH'  # 上交所
    SHENZHEN = 'SZ'  # 深交所


class EntrustDirection(IntEnum):
    """委托方向（l2order.entrustDirection）"""
    BUY = 1  # 买入
    SELL = 2  # 卖出
    CANCEL_BUY = 3  # 撤买（上交所）
    CANCEL_SELL = 4  # 撤卖（上交所）


class TradeFlag(IntEnum):
    """成交标志（l2transaction.tradeFlag）"""
    BUY = 1  # 外盘（主动买入）
    SELL = 2  # 内盘（主动卖出）
    CANCEL = 3  # 撤单（深交所）


class OrderSize(str, Enum):
    """订单规模分类"""
    SUPER_LARGE = 'super_large'  # 超大单：≥50万股 或 ≥100万元
    LARGE = 'large'  # 大单：≥10万股 或 ≥20万元
    MEDIUM = 'medium'  # 中单
    SMALL = 'small'  # 小单


# 大单阈值常量
class OrderThreshold:
    """订单规模阈值"""
    # 超大单阈值
    SUPER_LARGE_VOLUME = 500_000  # 50万股
    SUPER_LARGE_AMOUNT = 1_000_000  # 100万元

    # 大单阈值
    LARGE_VOLUME = 100_000  # 10万股
    LARGE_AMOUNT = 200_000  # 20万元

    # 封板预警阈值
    SEAL_ALERT_AMOUNT = 20_000_000  # 2000万元


# 涨跌停幅度
class LimitPct:
    """涨跌停幅度"""
    NORMAL = 0.10  # 普通股票 10%
    ST = 0.05  # ST股票 5%
    NEW_STOCK_FIRST_DAY = 0.44  # 新股首日 44%（科创板/创业板注册制）
    KCBJ = 0.20  # 科创板/北交所 20%


def get_market(stock_code: str) -> Market:
    """
    根据股票代码判定市场
    
    Args:
        stock_code: 股票代码，格式如 '600000.SH' 或 '000001.SZ'
        
    Returns:
        Market枚举值
        
    Raises:
        ValueError: 无法识别的股票代码
    """
    if stock_code.endswith('.SH'):
        return Market.SHANGHAI
    elif stock_code.endswith('.SZ'):
        return Market.SHENZHEN
    else:
        raise ValueError(f"Unknown market for stock code: {stock_code}")


def is_cancel_order(stock_code: str,
                    order_data: dict = None,
                    trans_data: dict = None) -> bool:
    """
    判断是否为撤单
    
    上交所：通过 l2order.entrustDirection 判断（3=撤买, 4=撤卖）
    深交所：通过 l2transaction.tradeFlag 判断（3=撤单）
    
    Args:
        stock_code: 股票代码
        order_data: l2order数据字典（可选）
        trans_data: l2transaction数据字典（可选）
        
    Returns:
        是否为撤单
    """
    market = get_market(stock_code)

    if market == Market.SHANGHAI and order_data:
        # 上交所：检查委托方向
        direction = int(order_data.get('entrustDirection', 0))
        return direction in (EntrustDirection.CANCEL_BUY,
                             EntrustDirection.CANCEL_SELL)

    elif market == Market.SHENZHEN and trans_data:
        # 深交所：检查成交标志
        trade_flag = int(trans_data.get('tradeFlag', 0))
        return trade_flag == TradeFlag.CANCEL

    return False


def classify_order_size(volume: int, amount: float) -> OrderSize:
    """
    判定订单规模分类
    
    Args:
        volume: 成交量（股）
        amount: 成交金额（元）
        
    Returns:
        OrderSize枚举值
    """
    # 超大单：成交量≥50万股 或 成交金额≥100万元
    if volume >= OrderThreshold.SUPER_LARGE_VOLUME or amount >= OrderThreshold.SUPER_LARGE_AMOUNT:
        return OrderSize.SUPER_LARGE

    # 大单：10万股≤成交量<50万股 或 20万元≤成交金额<100万元
    if (OrderThreshold.LARGE_VOLUME <= volume <
            OrderThreshold.SUPER_LARGE_VOLUME or OrderThreshold.LARGE_AMOUNT <=
            amount < OrderThreshold.SUPER_LARGE_AMOUNT):
        return OrderSize.LARGE

    # 中单和小单
    if volume >= 10_000 or amount >= 40_000:
        return OrderSize.MEDIUM

    return OrderSize.SMALL


def is_large_order(volume: int, amount: float = None) -> bool:
    """
    判断是否为大单（包括超大单和大单）
    
    Args:
        volume: 委托量或成交量
        amount: 委托金额或成交金额（可选）
        
    Returns:
        是否为大单
    """
    if volume >= OrderThreshold.LARGE_VOLUME:
        return True

    if amount and amount >= OrderThreshold.LARGE_AMOUNT:
        return True

    return False


def _round_price(price):
    """将价格精确地四舍五入到两位小数（分）。

    Python 内置的 round(x, 2) 对二进制浮点数会存在“银行家舍入”(ties-to-even)、
    以及浮点表示误差带来的边界问题（例如 10.235 这类值在二进制中无法精确表示），
    这会导致在涨跌停价格这类关键位置出现“差一分”的情况。

    这里采用：
    1) 先做 3 位小数的预处理（与历史实现兼容，降低噪声）
    2) 再将价格转为 Decimal，按“分”放大到整数并使用 ROUND_HALF_UP
    3) 最后缩放回 2 位小数

    同时兼容 Pandas Series 输入（逐元素处理）。
    """
    price = round(price, 3)
    price_decimal = Decimal(str(price))
    rounded_price = (price_decimal * Decimal('100')).quantize(
        Decimal('1'), rounding=ROUND_HALF_UP) / Decimal('100')
    return float(rounded_price)


def get_limit_price(last_close: float,
                    is_st: bool = False,
                    is_kcbj: bool = False) -> float:
    """
    计算涨停价

    Args:
        last_close: 昨收价
        is_st: 是否ST股票
        is_kcbj: 是否科创板/北交所

    Returns:
        涨停价（保留2位小数，采用 ROUND_HALF_UP 精确四舍五入）
    """
    if is_kcbj:
        pct = LimitPct.KCBJ
    elif is_st:
        pct = LimitPct.ST
    else:
        pct = LimitPct.NORMAL

    return _round_price(last_close * (1 + pct))


def is_limit_up_price(price: float,
                      limit_price: float,
                      tolerance: float = 0.0001) -> bool:
    """
    判断是否为涨停价
    
    Args:
        price: 当前价格
        limit_price: 涨停价
        tolerance: 价格容差（默认0.001元）
        
    Returns:
        是否为涨停价
    """
    return abs(price - limit_price) < tolerance
