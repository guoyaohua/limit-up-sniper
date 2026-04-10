"""
core/pre_market_sell.py - 盘前卖出策略

从 打板策略_v2.4.py 提取的盘前卖出策略相关函数。
"""

from xtquant import xtconstant
from loguru import logger

from config import (
    PRE_MARKET_SELL_STRATEGY,
    FIXED_PREMIUM_RATE,
    STOP_LOSS_RATE,
    STRATEGY_NAME,
)
from infra.common_enums import PreMarketSellStrategy
from infra.data_helpers import _round_price
from infra.utils import send_email


def execute_pre_market_sell_strategy(xt_trader, acc, stock_code,
                                     stock_info_dict, available_volume,
                                     yesterday_close_price, position,
                                     yesterday_limit_up_stocks):
    """
    执行盘前卖出策略

    根据全局配置 PRE_MARKET_SELL_STRATEGY 执行相应的盘前卖出策略。

    Args:
        xt_trader: XTQuantTrader 实例
        acc: 资金账号
        stock_code: 股票代码
        stock_info_dict: 股票信息字典
        available_volume: 可用数量
        yesterday_close_price: 昨日收盘价
        position: 持仓信息字典
        yesterday_limit_up_stocks: 昨日涨停股票列表
    """
    strategy = PRE_MARKET_SELL_STRATEGY
    stock_name = stock_info_dict[stock_code]['股票名称']

    logger.info(f'[盘前策略] {stock_code} {stock_name} 执行策略: {strategy.value}')

    # ======================== 策略4：不挂盘前卖出单 ========================
    if strategy == PreMarketSellStrategy.NO_PRE_MARKET_SELL:
        logger.info(f'[盘前策略] {stock_code} 不挂盘前卖出单，完全依赖盘中策略处理')
        return

    # ======================== 判断是否为昨日涨停股票 ========================
    if stock_code in yesterday_limit_up_stocks:
        # 昨日涨停股票的处理策略

        if strategy == PreMarketSellStrategy.TIERED_SELL:
            # 策略1：分批挂单策略（原有策略）
            _execute_tiered_sell_strategy(xt_trader, acc, stock_code,
                                          stock_info_dict, available_volume,
                                          yesterday_close_price)

        elif strategy == PreMarketSellStrategy.FIXED_PREMIUM_SELL:
            # 策略2：固定溢价卖出策略
            _execute_fixed_premium_sell_strategy(xt_trader, acc, stock_code,
                                                 stock_info_dict,
                                                 available_volume,
                                                 yesterday_close_price)

        elif strategy == PreMarketSellStrategy.LIMIT_UP_SELL:
            # 策略3：全部涨停价卖出策略
            _execute_limit_up_sell_strategy(xt_trader, acc, stock_code,
                                            stock_info_dict, available_volume)
    else:
        # 非昨日涨停股票，判断是否需要止损
        _execute_stop_loss_strategy(xt_trader, acc, stock_code,
                                    stock_info_dict, available_volume,
                                    yesterday_close_price, position)


def _execute_tiered_sell_strategy(xt_trader, acc, stock_code, stock_info_dict,
                                  available_volume, yesterday_close_price):
    """
    策略1：分批挂单策略（原有策略）
    昨日涨停股票：昨收+5%卖1/4仓位，涨停价卖1/4仓位，剩余1/2待盘中处理
    """
    logger.info(f'[盘前策略] {stock_code} 为昨日涨停股票，执行分批挂单策略')

    stock_name = stock_info_dict[stock_code]['股票名称']

    # 计算卖出价格和数量
    # 1. 昨收+5%价格
    price_5pct = round(yesterday_close_price * 1.05, 2)
    # 2. 今日涨停价
    limit_up_price = stock_info_dict[stock_code]['涨停价']

    # 每档挂1/4仓位（向下取整到100股）
    quarter_volume = int(available_volume / 4 / 100) * 100

    if quarter_volume >= 100:  # 确保每档至少100股
        # 挂单1：昨收+5%价格卖出1/4仓位
        order_id_1 = xt_trader.order_stock(account=acc,
                                           stock_code=stock_code,
                                           order_type=xtconstant.STOCK_SELL,
                                           order_volume=quarter_volume,
                                           price_type=xtconstant.FIX_PRICE,
                                           price=price_5pct,
                                           strategy_name=STRATEGY_NAME,
                                           order_remark='昨日涨停股盘前卖出-5%档位')

        msg = f'【盘前挂单-5%档】股票: {stock_code} {stock_name}, ' f'挂单数量: {quarter_volume}, 委托价格: {price_5pct:.2f} (昨收+5%), ' f'订单编号: {order_id_1}'
        logger.warning(msg)
        send_email(f'【盘前挂单】{stock_code} 5%档位', msg)

        # 挂单2：涨停价卖出1/4仓位
        order_id_2 = xt_trader.order_stock(account=acc,
                                           stock_code=stock_code,
                                           order_type=xtconstant.STOCK_SELL,
                                           order_volume=quarter_volume,
                                           price_type=xtconstant.FIX_PRICE,
                                           price=limit_up_price,
                                           strategy_name=STRATEGY_NAME,
                                           order_remark='昨日涨停股盘前卖出-涨停档位')

        msg = f'【盘前挂单-涨停档】股票: {stock_code} {stock_name}, ' f'挂单数量: {quarter_volume}, 委托价格: {limit_up_price:.2f} (涨停价), ' f'订单编号: {order_id_2}'
        logger.warning(msg)
        send_email(f'【盘前挂单】{stock_code} 涨停档位', msg)

        # 剩余仓位（1/2）留待盘中根据走势动态处理
        remaining_volume = available_volume - quarter_volume * 2
        logger.info(f'[盘前策略] {stock_code} 剩余仓位 {remaining_volume} 股待盘中处理')
    else:
        logger.warning(f'[盘前策略] {stock_code} 可用数量 {available_volume} 不足以分批挂单')


def _execute_fixed_premium_sell_strategy(xt_trader, acc, stock_code,
                                         stock_info_dict, available_volume,
                                         yesterday_close_price):
    """
    策略2：固定溢价卖出策略
    昨日涨停股票：按昨收+固定溢价比例（默认2%）挂卖出单
    """
    logger.info(
        f'[盘前策略] {stock_code} 为昨日涨停股票，执行固定溢价({FIXED_PREMIUM_RATE:.1%})卖出策略')

    stock_name = stock_info_dict[stock_code]['股票名称']

    # 计算卖出价格：昨收 + 固定溢价
    sell_price = round(yesterday_close_price * (1 + FIXED_PREMIUM_RATE), 2)

    # 确保卖出价格不超过涨停价
    limit_up_price = stock_info_dict[stock_code]['涨停价']
    if sell_price > limit_up_price:
        sell_price = limit_up_price
        logger.warning(
            f'[盘前策略] {stock_code} 计算价格超过涨停价，调整为涨停价 {limit_up_price}')

    if available_volume >= 100:
        order_id = xt_trader.order_stock(
            account=acc,
            stock_code=stock_code,
            order_type=xtconstant.STOCK_SELL,
            order_volume=available_volume,
            price_type=xtconstant.FIX_PRICE,
            price=sell_price,
            strategy_name=STRATEGY_NAME,
            order_remark=f'昨日涨停股盘前卖出-溢价{FIXED_PREMIUM_RATE:.0%}')

        msg = f'【盘前挂单-固定溢价】股票: {stock_code} {stock_name}, ' f'挂单数量: {available_volume}, 委托价格: {sell_price:.2f} (昨收+{FIXED_PREMIUM_RATE:.0%}), ' f'订单编号: {order_id}'
        logger.warning(msg)
        send_email(f'【盘前挂单】{stock_code} 固定溢价{FIXED_PREMIUM_RATE:.0%}', msg)
    else:
        logger.warning(f'[盘前策略] {stock_code} 可用数量 {available_volume} 不足100股')


def _execute_limit_up_sell_strategy(xt_trader, acc, stock_code,
                                    stock_info_dict, available_volume):
    """
    策略3：全部涨停价卖出策略
    昨日涨停股票：全部持仓按涨停价挂卖出单（博取最大收益）
    """
    logger.info(f'[盘前策略] {stock_code} 为昨日涨停股票，执行涨停价卖出策略')

    stock_name = stock_info_dict[stock_code]['股票名称']
    limit_up_price = stock_info_dict[stock_code]['涨停价']

    if available_volume >= 100:
        order_id = xt_trader.order_stock(account=acc,
                                         stock_code=stock_code,
                                         order_type=xtconstant.STOCK_SELL,
                                         order_volume=available_volume,
                                         price_type=xtconstant.FIX_PRICE,
                                         price=limit_up_price,
                                         strategy_name=STRATEGY_NAME,
                                         order_remark='昨日涨停股盘前卖出-涨停价')

        msg = f'【盘前挂单-涨停价】股票: {stock_code} {stock_name}, ' f'挂单数量: {available_volume}, 委托价格: {limit_up_price:.2f} (涨停价), ' f'订单编号: {order_id}'
        logger.warning(msg)
        send_email(f'【盘前挂单】{stock_code} 涨停价', msg)
    else:
        logger.warning(f'[盘前策略] {stock_code} 可用数量 {available_volume} 不足100股')


def _execute_stop_loss_strategy(xt_trader, acc, stock_code, stock_info_dict,
                                available_volume, yesterday_close_price,
                                position):
    """
    非昨日涨停股票的止损策略
    如果跌破止损位，挂跌停价卖出止损
    """
    # 判断是否需要止损
    if yesterday_close_price < position['成本价'] * (1 - STOP_LOSS_RATE):
        stock_name = stock_info_dict[stock_code]['股票名称']

        # 挂跌停价卖出止损
        order_id = xt_trader.order_stock(
            account=acc,
            stock_code=stock_code,
            order_type=xtconstant.STOCK_SELL,
            order_volume=available_volume,
            price_type=xtconstant.FIX_PRICE,
            price=stock_info_dict[stock_code]['跌停价'],
            strategy_name=STRATEGY_NAME,
            order_remark='盘前止损卖出')

        msg = f'【盘前止损】股票: {stock_code} {stock_name}, ' f'挂单数量: {available_volume}, 委托价格: {stock_info_dict[stock_code]["跌停价"]:.2f}, ' f'订单编号: {order_id}'
        logger.warning(msg)
        send_email(f'【盘前止损】{stock_code}', msg)
