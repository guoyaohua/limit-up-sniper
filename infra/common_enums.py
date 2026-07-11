from enum import Enum, auto, IntEnum
from infra.xtconstant_compat import xtconstant


class PreMarketSellStrategy(Enum):
    """
    盘前卖出策略枚举
    
    用于配置盘前（开盘前）持仓股票的卖出策略。
    可在策略文件中通过全局变量 PRE_MARKET_SELL_STRATEGY 进行配置。
    
    策略说明：
        - TIERED_SELL: 分批挂单策略（原有策略）
          昨日涨停股票：昨收+5%卖1/4仓位，涨停价卖1/4仓位，剩余1/2待盘中处理
          非昨日涨停股票：仅在跌破止损位时挂跌停价卖出
          
        - FIXED_PREMIUM_SELL: 固定溢价卖出策略
          昨日涨停股票：按昨收+固定溢价比例（默认2%）挂卖出单
          非昨日涨停股票：仅在跌破止损位时挂跌停价卖出
          
        - LIMIT_UP_SELL: 全部涨停价卖出策略
          昨日涨停股票：全部持仓按涨停价挂卖出单（博取最大收益）
          非昨日涨停股票：仅在跌破止损位时挂跌停价卖出
          
        - NO_PRE_MARKET_SELL: 不挂盘前卖出单
          盘前不挂任何卖出单，完全依赖盘中策略处理
    """

    TIERED_SELL = "tiered_sell"
    FIXED_PREMIUM_SELL = "fixed_premium_sell"
    LIMIT_UP_SELL = "limit_up_sell"
    NO_PRE_MARKET_SELL = "no_pre_market_sell"


class EBrokerPriceType(Enum):
    """价格类型枚举类"""
    BROKER_PRICE_ANY = 49  # 市价
    BROKER_PRICE_LIMIT = 50  # 限价
    BROKER_PRICE_BEST = 51  # 最优价
    BROKER_PRICE_PROP_ALLOTMENT = 52  # 配股
    BROKER_PRICE_PROP_REFER = 53  # 转托
    BROKER_PRICE_PROP_SUBSCRIBE = 54  # 申购
    BROKER_PRICE_PROP_BUYBACK = 55  # 回购
    BROKER_PRICE_PROP_PLACING = 56  # 配售
    BROKER_PRICE_PROP_DECIDE = 57  # 指定
    BROKER_PRICE_PROP_EQUITY = 58  # 转股
    BROKER_PRICE_PROP_SELLBACK = 59  # 回售
    BROKER_PRICE_PROP_DIVIDEND = 60  # 股息
    BROKER_PRICE_PROP_SHENZHEN_PLACING = 68  # 深圳配售确认
    BROKER_PRICE_PROP_CANCEL_PLACING = 69  # 配售放弃
    BROKER_PRICE_PROP_WDZY = 70  # 无冻质押
    BROKER_PRICE_PROP_DJZY = 71  # 冻结质押
    BROKER_PRICE_PROP_WDJY = 72  # 无冻解押
    BROKER_PRICE_PROP_JDJY = 73  # 解冻解押
    BROKER_PRICE_PROP_VOTE = 75  # 投票
    BROKER_PRICE_PROP_YSYYJC = 77  # 预售要约解除
    BROKER_PRICE_PROP_FUND_DEVIDEND = 78  # 基金设红
    BROKER_PRICE_PROP_FUND_ENTRUST = 79  # 基金申赎
    BROKER_PRICE_PROP_CROSS_MARKET = 80  # 跨市转托
    BROKER_PRICE_PROP_ETF = 81  # ETF申购
    BROKER_PRICE_PROP_EXERCIS = 83  # 权证行权
    BROKER_PRICE_PROP_PEER_PRICE_FIRST = 84  # 对手方最优价格
    BROKER_PRICE_PROP_L5_FIRST_LIMITPX = 85  # 最优五档即时成交剩余转限价
    BROKER_PRICE_PROP_MIME_PRICE_FIRST = 86  # 本方最优价格
    BROKER_PRICE_PROP_INSTBUSI_RESTCANCEL = 87  # 即时成交剩余撤销
    BROKER_PRICE_PROP_L5_FIRST_CANCEL = 88  # 最优五档即时成交剩余撤销
    BROKER_PRICE_PROP_FULL_REAL_CANCEL = 89  # 全额成交并撤单
    BROKER_PRICE_PROP_FUND_CHAIHE = 90  # 基金拆合
    BROKER_PRICE_PROP_DEBT_CONVERSION = 91  # 债转股
    BROKER_PRICE_PROP_YYSGYS = 92  # 要约收购预售
    BROKER_PRICE_BID_LIMIT = 92  # 港股通竞价限价
    BROKER_PRICE_ENHANCED_LIMIT = 93  # 港股通增强限价
    BROKER_PRICE_RETAIL_LIMIT = 94  # 港股通零股限价
    BROKER_PRICE_PROP_DIRECT_SECU_REPAY = 101  # 直接还券
    BROKER_PRICE_PROP_COLLATERAL_TRANSFER = 107  # 担保品划转
    BROKER_PRICE_PROP_INCREASE_SHARE = 'j'  # 增发
    BROKER_PRICE_PROP_NEEQ_PRICING = 'w'  # 定价（全国股转 - 挂牌公司交易 - 协议转让）
    BROKER_PRICE_PROP_NEEQ_MATCH_CONFIRM = 'x'  # 成交确认（全国股转 - 挂牌公司交易 - 协议转让）
    BROKER_PRICE_PROP_NEEQ_MUTUAL_MATCH_CONFIRM = 'y'  # 互报成交确认（全国股转 - 挂牌公司交易 - 协议转让）
    BROKER_PRICE_PROP_NEEQ_LIMIT = 'z'  # 限价（用于挂牌公司交易 - 做市转让 - 限价买卖和两网及退市交易-限价买卖）


class SignalStatus(Enum):
    """买入信号状态枚举"""
    INIT = 0  # 初始状态
    ORDER_PLACED = auto()  # 已下单
    MONEY_FLOW_READY = auto()  # 资金流入满足条件
    BREAK_LIMIT_UP = auto()  # 已炸板
    VOLUME_READY = auto()  # 成交量满足条件


# 股票下单状态
#   1. 未下单
#   2. 已下单买入
#   3. 已下单卖出
#   4. 已撤单
class StockOrderStatus(Enum):
    """股票下单状态枚举"""
    NOT_ORDERED = '未下单'  # 未下单
    ORDERED_BUY = '已下单买入'  # 已下单买入
    ORDERED_SELL = '已下单卖出'  # 已下单卖出
    CANCELLED = '已撤单'  # 已撤单
    # 已持仓
    POSITION_HOLDING = '持仓中'  # 持仓中
    # 部分成交
    PARTIALLY_FILLED = '部分成交'  # 部分成交


# 股票下单状态枚举 - 整数版本
class StockOrderStatusInt(IntEnum):
    """股票下单状态枚举 - 整数版本"""
    NOT_ORDERED = auto()  # 未下单
    ORDERED_BUY = auto()  # 已下单买入
    ORDERED_SELL = auto()  # 已下单卖出
    CANCELLED = auto()  # 已撤单
    POSITION_HOLDING = auto()  # 持仓中
    PARTIALLY_FILLED = auto()  # 部分成交


# 股票涨停状态枚举
class StockLimitStatusInt(IntEnum):
    """股票涨停状态枚举"""
    NOT_LIMIT_UP = auto()  # 未涨停
    LIMIT_UP = auto()  # 涨停
    LIMIT_UP_BROKEN = auto()  # 炸板
    LIMIT_UP_REBOUND = auto()  # 回封


class StockLimitStatus(Enum):
    """股票涨停状态枚举"""
    # 未涨停
    NOT_LIMIT_UP = '未涨停'
    # 涨停
    LIMIT_UP = '涨停'
    # 炸板
    LIMIT_UP_BROKEN = '炸板'
    # 回封
    LIMIT_UP_REBOUND = '回封'


# 委托类型，包括买入、卖出和撤单
class OrderType(Enum):
    """委托类型枚举"""
    BUY = '买入'  # 买入
    SELL = '卖出'  # 卖出
    CANCEL = '撤单'  # 撤单

    # 交易模块使用
    买入 = xtconstant.STOCK_BUY
    卖出 = xtconstant.STOCK_SELL


class PriceType(Enum):
    '''
    报价类型(price_type)
    
    上交所 股票
        最优五档即时成交剩余撤销 - xtconstant.MARKET_SH_CONVERT_5_CANCEL
        最优五档即时成交剩转限价 - xtconstant.MARKET_SH_CONVERT_5_LIMIT
        对手方最优价格委托 - xtconstant.MARKET_PEER_PRICE_FIRST
        本方最优价格委托 - xtconstant.MARKET_MINE_PRICE_FIRST
    
    深交所 股票 期权
        对手方最优价格委托 - xtconstant.MARKET_PEER_PRICE_FIRST
        本方最优价格委托 - xtconstant.MARKET_MINE_PRICE_FIRST
        即时成交剩余撤销委托 - xtconstant.MARKET_SZ_INSTBUSI_RESTCANCEL
        最优五档即时成交剩余撤销 - xtconstant.MARKET_SZ_CONVERT_5_CANCEL
        全额成交或撤销委托 - xtconstant.MARKET_SZ_FULL_OR_CANCEL
    '''

    上交所_最优五档即时成交剩余撤销 = xtconstant.MARKET_SH_CONVERT_5_CANCEL
    上交所_最优五档即时成交剩转限价 = xtconstant.MARKET_SH_CONVERT_5_LIMIT
    上交所_对手方最优价格委托 = xtconstant.MARKET_PEER_PRICE_FIRST
    上交所_本方最优价格委托 = xtconstant.MARKET_MINE_PRICE_FIRST

    深交所_对手方最优价格委托 = xtconstant.MARKET_PEER_PRICE_FIRST
    深交所_本方最优价格委托 = xtconstant.MARKET_MINE_PRICE_FIRST
    深交所_即时成交剩余撤销委托 = xtconstant.MARKET_SZ_INSTBUSI_RESTCANCEL
    深交所_最优五档即时成交剩余撤销 = xtconstant.MARKET_SZ_CONVERT_5_CANCEL
    深交所_全额成交或撤销委托 = xtconstant.MARKET_SZ_FULL_OR_CANCEL


class OrderStatus(Enum):
    '''委托状态(order_status)

    枚举变量名	值	含义
    xtconstant.ORDER_UNREPORTED	48	未报
    xtconstant.ORDER_WAIT_REPORTING	49	待报
    xtconstant.ORDER_REPORTED	50	已报
    xtconstant.ORDER_REPORTED_CANCEL	51	已报待撤
    xtconstant.ORDER_PARTSUCC_CANCEL	52	部成待撤
    xtconstant.ORDER_PART_CANCEL	53	部撤（已经有一部分成交，剩下的已经撤单）
    xtconstant.ORDER_CANCELED	54	已撤
    xtconstant.ORDER_PART_SUCC	55	部成（已经有一部分成交，剩下的待成交）
    xtconstant.ORDER_SUCCEEDED	56	已成
    xtconstant.ORDER_JUNK	57	废单
    xtconstant.ORDER_UNKNOWN	255	未知
    '''
    未报 = xtconstant.ORDER_UNREPORTED
    待报 = xtconstant.ORDER_WAIT_REPORTING
    已报 = xtconstant.ORDER_REPORTED
    已报待撤 = xtconstant.ORDER_REPORTED_CANCEL
    部成待撤 = xtconstant.ORDER_PARTSUCC_CANCEL
    部撤 = xtconstant.ORDER_PART_CANCEL
    已撤 = xtconstant.ORDER_CANCELED
    部成 = xtconstant.ORDER_PART_SUCC
    已成 = xtconstant.ORDER_SUCCEEDED
    废单 = xtconstant.ORDER_JUNK
    未知 = xtconstant.ORDER_UNKNOWN
