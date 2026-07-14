"""
engine/trader.py - 实盘交易任务

从 打板策略_v2.4.py 提取的实盘交易执行函数 run_xt_trader_task()。
包含：盘前卖出策略、订单处理循环（买入/卖出/撤单）。
"""

import sys
import time
import json
import traceback
import numpy as np
from queue import Empty
from threading import Thread
from datetime import datetime
from loguru import logger
from xtquant import xtconstant

from config import (
    CLIENT_PATH, STOCK_ACCOUNT, STOP_TIME, STRATEGY_NAME,
    MAX_HOLDING_COUNT, MAX_CANCEL_COUNT,
    STOP_LOSS_RATE, FIXED_PREMIUM_RATE,
    PRE_MARKET_SELL_STRATEGY,
    VOLATILITY_TARGET, VOLATILITY_RATIO_MIN, VOLATILITY_RATIO_MAX,
    WATCHLIST_POSITION_RATIO,
)
from infra.common_enums import (
    OrderType, StockOrderStatusInt, EBrokerPriceType, PreMarketSellStrategy,
)
from infra.utils import send_email
from infra.trade_log import save_trade_log
from infra.data_helpers import _check_same_price
from engine.xt_queries import (
    cache_local_buy_reservation, run_with_effective_orders_locked,
    query_stock_asset, query_stock_positions, query_stock_orders,
    query_positions_and_orders, query_positions_and_orders_task,
)
from core.trailing_stop import calculate_trailing_stop_prices
from core.market_microstructure import (
    build_local_buy_reservation, exposure_slot_count,
)


def _accepted_order_id(order_id):
    """Return whether XTQuant synchronously accepted an order request."""
    try:
        return int(order_id) > 0
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------- #
#                               盘前卖出策略（来自 core 层）                        #
# ---------------------------------------------------------------------------- #

from core.pre_market_sell import execute_pre_market_sell_strategy  # noqa: E402


# ---------------------------------------------------------------------------- #
#                                  实盘交易任务                                  #
# ---------------------------------------------------------------------------- #

def run_xt_trader_task(order_queue, shared_data):
    """
    运行交易任务，处理买入、卖出和撤单委托

    主要功能：
        1. 初始化交易接口连接
        2. 处理昨日持仓（涨停价或跌停价挂单）
        3. 开启持仓/委托状态定时查询
        4. 计算每只股票最大持仓金额
        5. 处理订单队列中的交易请求

    Args:
        order_queue (Queue): 包含交易订单的队列
        shared_data (dict): 共享数据字典

    订单格式:
        {
            '委托类型': OrderType,  # OrderType.BUY/SELL/CANCEL
            '委托价格': float,      # 委托价格（市价单时可选）
            '报价类型': int,        # xtconstant中的报价类型
            '策略名称': str,        # 策略名称标识
            '委托备注': str,        # 委托备注信息
            '股票代码': str,        # 股票代码（如'600000.SH'）
            '快照': dict            # tick数据快照（可选）
        }

    异常处理：
        - 交易接口连接失败：发送邮件通知
        - 订单处理异常：记录日志并继续处理下一个订单
        - 资金不足：跳过该订单
        - 持仓不足：跳过卖出订单
    """
    sys.path.append(".")
    from engine.xt_callback import get_trader_entity

    stock_info_dict = shared_data['股票信息']
    cancelled_order_id_list = []

    try:
        xt_trader, acc = get_trader_entity(
            logger, CLIENT_PATH, STOCK_ACCOUNT, STRATEGY_NAME
        )
        if xt_trader is None:
            # ----------------------------------- 记录日志 ----------------------------------- #
            timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
            msg = f'【交易接口启动失败】获取交易接口实例失败, 时间: {timestamp_now}'
            send_email('【交易接口启动失败】', msg)
            logger.error(msg)
            return
    except Exception as e:
        logger.exception(f'【关键错误】获取交易实体失败: {e}')
        send_email('【关键错误】获取交易实体失败',
                   f'获取交易实体时发生异常: {e}\n{traceback.format_exc()}')
        return

    # --------------------------------- 处理昨日持仓股票 --------------------------------- #
    try:
        # 先查询一下持仓和委托，确保shared_data中数据更新
        query_positions_and_orders(xt_trader, acc, shared_data)

        positions = query_stock_positions(xt_trader, acc)
        # 获取昨日涨停股票列表
        yesterday_limit_up_stocks = shared_data.get('昨日涨停股票', [])

        for positon in positions.values():
            try:
                position = json.loads(positon)
                stock_code = position['证券代码']
                if stock_code not in stock_info_dict:
                    # 跳过卖出所持有的沪深A股标的
                    logger.warning(f'股票代码 {stock_code} 不在股票信息字典中，跳过处理昨日持仓卖出')
                    continue
                if position['持仓数量'] > 0:
                    # 添加安全检查
                    if '昨日收盘价' not in stock_info_dict[stock_code]:
                        logger.error(f'股票 {stock_code} 缺少昨日收盘价信息')
                        send_email('【关键错误】处理昨日持仓失败',
                                   f'股票 {stock_code} 缺少昨日收盘价信息')
                        continue
                    logger.info(position)
                    shared_data['盘前持仓'].append(stock_code)

                    yesterday_close_price = stock_info_dict[stock_code][
                        '昨日收盘价']
                    available_volume = position['可用数量']

                    # ======================== 盘前卖出策略执行 ========================
                    execute_pre_market_sell_strategy(
                        xt_trader=xt_trader,
                        acc=acc,
                        stock_code=stock_code,
                        stock_info_dict=stock_info_dict,
                        available_volume=available_volume,
                        yesterday_close_price=yesterday_close_price,
                        position=position,
                        yesterday_limit_up_stocks=yesterday_limit_up_stocks)

                    # -------------------------------- 计算止盈止损价格列表 -------------------------------- #
                    stock_status_signal = shared_data['股票状态信号'][stock_code]
                    with stock_status_signal['最高价'].get_lock():
                        stock_status_signal['最高价'].value = position['成本价']

                    calculate_trailing_stop_prices(
                        highest_price=position['成本价'],
                        limit_down_price=stock_info_dict[stock_code]
                        ['跌停价'],  # 跌停价
                        stock_code=stock_code,
                        shared_data=shared_data)

            except Exception as e:
                logger.exception(f'【关键错误】处理单个持仓股票失败 {stock_code}: {e}')
                send_email(
                    '【关键错误】处理昨日持仓失败',
                    f'处理股票 {stock_code} 昨日持仓时发生异常: {e}\n{traceback.format_exc()}'
                )
                return
    except Exception as e:
        logger.exception(f'【关键错误】处理昨日持仓失败: {e}')
        send_email('【关键错误】处理昨日持仓失败',
                   f'处理昨日持仓时发生异常: {e}\n{traceback.format_exc()}')
        return

    # ------------------------------- 开启持仓/委托查询定时查询 ------------------------------ #
    Thread(target=query_positions_and_orders_task,
           args=(xt_trader, acc, shared_data),
           daemon=True).start()

    # ------------------------------- 计算每只股票最大持仓金额 ------------------------------- #
    try:
        total_asset = query_stock_asset(xt_trader, acc)['总资产']
        holding_amount_threshold = total_asset / MAX_HOLDING_COUNT

        logger.info(
            f'当前总资产: {total_asset}, 每只股票最大持仓金额: {holding_amount_threshold}')
    except Exception as e:
        logger.exception(f'【关键错误】计算持仓金额阈值失败: {e}')
        send_email('【关键错误】计算持仓金额阈值失败',
                   f'计算持仓金额阈值时发生异常: {e}\n{traceback.format_exc()}')
        return

    logger.info('[实盘] 交易接口已启动')
    while True:
        try:
            order_req = order_queue.get(timeout=1)
            logger.info(f'订单信息: {order_req}')
            stock_code = order_req['股票代码']

            if '委托类型' not in order_req:
                logger.error(f'委托类型未指定')
                continue

            # ---------------------------------------------------------------------------- #
            #                                      买入                                     #
            # ---------------------------------------------------------------------------- #
            elif order_req['委托类型'] == OrderType.BUY:
                # ---------------------------------- 查询可用资金 ---------------------------------- #
                available_cash = query_stock_asset(xt_trader, acc)['可用金额']
                order_price = order_req['委托价格']
                min_holding_amount = order_price * 100  # 最小持仓金额为100股
                if available_cash < min_holding_amount:
                    logger.warning(
                        f'[余额不足] 可用资金不足以购买100股, 股票代码：{stock_code}, 价格：{order_price}, 可用资金: {available_cash}, 最小持仓金额: {min_holding_amount}'
                    )

                    # 还原下单状态
                    stock_status = shared_data['股票状态信号'][stock_code]
                    with stock_status['下单状态'].get_lock():
                        stock_status[
                            '下单状态'].value = StockOrderStatusInt.NOT_ORDERED

                    continue
                if min_holding_amount > holding_amount_threshold * 1.5:
                    logger.warning(
                        f'[股价过高] 可用资金不足以购买100股, 股票代码：{stock_code}, 价格：{order_price}, 可用资金: {available_cash}, 最小持仓金额: {min_holding_amount}, 每只股票最大持仓金额: {holding_amount_threshold}'
                    )
                    stock_status = shared_data['股票状态信号'][stock_code]
                    with stock_status['下单状态'].get_lock():
                        stock_status[
                            '下单状态'].value = StockOrderStatusInt.NOT_ORDERED
                    continue

                logger.debug(f'可用资金: {available_cash}')

                # ---------------------------------- 查询持仓股票 ---------------------------------- #
                positions = query_stock_positions(xt_trader, acc)
                if stock_code in positions:
                    logger.warning(
                        f'[跳过购买] 当前股票已持仓: {stock_code}, 持仓信息: {positions[stock_code]}'
                    )
                    stock_status = shared_data['股票状态信号'][stock_code]
                    with stock_status['下单状态'].get_lock():
                        stock_status[
                            '下单状态'].value = StockOrderStatusInt.POSITION_HOLDING
                    continue

                # ---------------------------- 查询当日所有的持仓和委托 ---------------------------- #
                orders = query_stock_orders(xt_trader,
                                            acc,
                                            cancelable_only=True)
                # ---------------------------------- 计算买入数量 ---------------------------------- #
                amount_threshold = min(
                    max(holding_amount_threshold, min_holding_amount),
                    available_cash)

                # U6升级：波动率加权仓位调整
                stock_amplitude = shared_data['股票信息'].get(stock_code, {}).get('近20日平均振幅', VOLATILITY_TARGET)
                volatility_ratio = VOLATILITY_TARGET / max(stock_amplitude, 0.01)
                volatility_ratio = max(VOLATILITY_RATIO_MIN, min(VOLATILITY_RATIO_MAX, volatility_ratio))
                adjusted_threshold = amount_threshold * volatility_ratio

                order_volume = int(
                    adjusted_threshold / order_req['委托价格'] / 100) * 100
                if volatility_ratio != 1.0:
                    logger.info(
                        f'[波动率调仓] {stock_code} 振幅{stock_amplitude:.2%}, 仓位倍数{volatility_ratio:.2f}, 买入量{order_volume}'
                    )

                # U5升级：观察名单仓位缩减
                watch_list = shared_data.get('观察名单', {})
                if stock_code in watch_list:
                    order_volume = int(order_volume * WATCHLIST_POSITION_RATIO / 100) * 100
                    logger.warning(
                        f'[观察名单-仓位缩减] {stock_code} 在观察名单中，仓位缩减至 {WATCHLIST_POSITION_RATIO*100:.0f}%，买入数量: {order_volume}'
                    )
                max_affordable_volume = int(available_cash / order_req['委托价格'] / 100) * 100
                order_volume = min(order_volume, max_affordable_volume)
                if order_volume <= 0:
                    logger.warning(f'[跳过购买] 计算后买入数量为0: {stock_code}')
                    stock_status = shared_data['股票状态信号'][stock_code]
                    with stock_status['下单状态'].get_lock():
                        stock_status[
                            '下单状态'].value = StockOrderStatusInt.NOT_ORDERED
                    continue

                # review 20260714: hold one process-local lock from the final
                # exposure check through broker acceptance and reservation.
                # This closes the refresh/submission race while worker signals
                # continue to arrive through the single executor queue.
                def submit_buy(effective_orders):
                    if stock_code in effective_orders:
                        return 'duplicate', effective_orders[stock_code]
                    if exposure_slot_count(
                            positions, effective_orders) >= MAX_HOLDING_COUNT:
                        return 'capacity', None
                    broker_order_id = xt_trader.order_stock(
                        account=acc,
                        stock_code=order_req["股票代码"],
                        order_type=xtconstant.STOCK_BUY,
                        order_volume=order_volume,
                        price_type=order_req['报价类型'],
                        price=order_req['委托价格'],
                        strategy_name=order_req['策略名称'],
                        order_remark=order_req['委托备注'])
                    if not _accepted_order_id(broker_order_id):
                        return 'rejected', broker_order_id
                    cache_local_buy_reservation(
                        shared_data,
                        stock_code,
                        build_local_buy_reservation(
                            stock_code,
                            broker_order_id,
                            委托价格=order_req['委托价格'],
                            委托数量=order_volume,
                            策略名称=order_req['策略名称'],
                        ),
                    )
                    return 'accepted', broker_order_id

                submission_status, submission_detail = (
                    run_with_effective_orders_locked(
                        shared_data, orders, submit_buy
                    )
                )
                if submission_status == 'duplicate':
                    logger.warning(
                        f'[跳过购买] 当前股票已有券商委托或本地预占: '
                        f'{stock_code}, 委托信息: {submission_detail}'
                    )
                    continue
                if submission_status == 'capacity':
                    logger.warning(
                        f'[跳过购买] 持仓及待成交买单已达 '
                        f'{MAX_HOLDING_COUNT} 个: {stock_code}'
                    )
                    stock_status = shared_data['股票状态信号'][stock_code]
                    with stock_status['下单状态'].get_lock():
                        stock_status[
                            '下单状态'].value = StockOrderStatusInt.NOT_ORDERED
                    continue
                if submission_status == 'rejected':
                    logger.error(
                        f'[委托买入失败] {stock_code} 柜台未返回有效订单编号: '
                        f'{submission_detail!r}')
                    stock_status = shared_data['股票状态信号'][stock_code]
                    with stock_status['下单状态'].get_lock():
                        stock_status[
                            '下单状态'].value = StockOrderStatusInt.NOT_ORDERED
                    continue
                order_id = submission_detail

                # ----------------------------------- 记录日志 ----------------------------------- #
                timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                msg = f'【委托买入】 股票代码: {order_req["股票代码"]}, 委托数量: {order_volume}, 报价类型: {order_req["报价类型"]}, 委托价格: {order_req["委托价格"]}, 订单编号: {order_id}, 策略名称: {order_req["策略名称"]}, 委托备注: {order_req["委托备注"]}, 时间: {timestamp_now}'
                msg += '\n' + order_req["操作原因"]
                send_email(f'【委托买入】股票代码: {order_req["股票代码"]}', msg)
                logger.warning(msg)

                # U8升级：结构化交易日志
                save_trade_log({
                    'action': 'BUY',
                    'stock_code': order_req['股票代码'],
                    'price': order_req['委托价格'],
                    'volume': order_volume,
                    'timestamp': timestamp_now,
                    'order_id': order_id,
                    'strategy_name': order_req['策略名称'],
                    'buy_reason': order_req.get('操作原因', ''),
                    'market_sentiment': shared_data['市场情绪_评分'].value if hasattr(shared_data.get('市场情绪_评分'), 'value') else 0,
                })

            # ---------------------------------------------------------------------------- #
            #                                      卖出                                     #
            # ---------------------------------------------------------------------------- #
            elif order_req['委托类型'] == OrderType.SELL:
                # ---------------------------------- 查询持仓股票 ---------------------------------- #
                positions = query_stock_positions(xt_trader, acc)
                if stock_code not in positions:
                    logger.error(f'[卖出失败] 当前未持仓: {order_req["股票代码"]}, 无法卖出')
                    continue

                position = json.loads(positions[stock_code])
                order_volume = position['可用数量']
                sell_volume = order_volume - order_req['剩余仓位']

                # ------------------------------------ 下单 ------------------------------------ #
                # 1. 固定价格
                if '委托价格' in order_req:
                    order_id = xt_trader.order_stock(
                        account=acc,
                        stock_code=order_req["股票代码"],
                        order_type=xtconstant.STOCK_SELL,
                        order_volume=sell_volume,
                        price_type=order_req['报价类型'],
                        price=order_req['委托价格'],
                        strategy_name=order_req['策略名称'],
                        order_remark=order_req['委托备注'])
                # 2. 市价单
                else:
                    order_id = xt_trader.order_stock(
                        account=acc,
                        stock_code=order_req["股票代码"],
                        order_type=xtconstant.STOCK_SELL,
                        order_volume=sell_volume,
                        price_type=order_req['报价类型'],
                        price=0,
                        strategy_name=order_req['策略名称'],
                        order_remark=order_req['委托备注'])
                if not _accepted_order_id(order_id):
                    # review 20260714: a rejected exit is still an open risk.
                    # Do not emit a fictitious SELL trade that can corrupt the
                    # daily PnL pairing; leave the holding eligible for retry.
                    logger.error(
                        f'[委托卖出失败] {stock_code} 柜台未返回有效订单编号: '
                        f'{order_id!r}')
                    send_email(
                        f'【委托卖出失败】股票代码: {stock_code}',
                        f'柜台未返回有效订单编号: {order_id!r}; '
                        f'原因: {order_req.get("操作原因", "")}',
                    )
                    continue

                # ----------------------------------- 记录日志 ----------------------------------- #
                timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                msg = f'【委托卖出】股票代码: {order_req["股票代码"]}, 委托数量: {sell_volume}, 报价类型: {order_req["报价类型"]}, 委托价格: {order_req["委托价格"] if "委托价格" in order_req else "市价"}, 订单编号: {order_id}, 策略名称: {order_req["策略名称"]}, 委托备注: {order_req["委托备注"]}, 时间: {timestamp_now}'
                msg += '\n' + order_req["操作原因"]
                send_email(f'【委托卖出】股票代码: {order_req["股票代码"]}', msg)
                logger.warning(msg)

                # U8升级：结构化交易日志
                save_trade_log({
                    'action': 'SELL',
                    'stock_code': order_req['股票代码'],
                    'price': order_req.get('委托价格', 0),
                    'volume': sell_volume,
                    'timestamp': timestamp_now,
                    'order_id': order_id,
                    'strategy_name': order_req['策略名称'],
                    'sell_trigger': order_req.get('操作原因', ''),
                    'market_sentiment': shared_data['市场情绪_评分'].value if hasattr(shared_data.get('市场情绪_评分'), 'value') else 0,
                })

            # ---------------------------------------------------------------------------- #
            #                                      撤单                                     #
            # ---------------------------------------------------------------------------- #
            elif order_req['委托类型'] == OrderType.CANCEL:
                # ---------------------------------- 查询可撤委托 ---------------------------------- #
                orders_can_cancel = query_stock_orders(xt_trader,
                                                       acc,
                                                       cancelable_only=True)
                if stock_code not in orders_can_cancel:
                    logger.warning(f'[撤单失败] 当前无可撤单委托: {order_req["股票代码"]}')
                    # Do not mark the local reservation as cancelled: QMT may
                    # still be acknowledging a real accepted order.  Keeping
                    # it allows a later Tick to retry once the order is visible.
                    continue

                # ------------------------------------ 撤单 ------------------------------------ #
                order_list = json.loads(orders_can_cancel[stock_code])
                cancel_result_dict = {}
                for order in order_list:
                    # NOTE:报价类型，该字段在返回时为柜台返回类型，不等价于下单传入的price_type，枚举值不一样功能一样，参见数据字典(https://dict.thinktrader.net/innerApi/enum_constants.html?id=7zqjlm#enum-ebrokerpricetype-%E4%BB%B7%E6%A0%BC%E7%B1%BB%E5%9E%8B)
                    # 50	限价
                    if order[
                            '报价类型'] != EBrokerPriceType.BROKER_PRICE_LIMIT.value:
                        logger.info(f'[跳过撤单] 报价类型非固定价格。{order}')
                        continue
                    elif order[
                            '委托类型'] == xtconstant.STOCK_SELL and _check_same_price(
                                order['委托价格'],
                                stock_info_dict[stock_code]['跌停价']):
                        logger.info(f'[跳过撤单] 以跌停价卖出。{order}')
                        continue
                    # !!! IMPORTANT: 订单编号有时候为空
                    # cancel_result = xt_trader.cancel_order_stock(
                    #     acc, order['订单编号'])

                    # 根据券商柜台返回的合同编号对委托进行撤单操作
                    market = xtconstant.SH_MARKET if order['证券代码'].endswith(
                        'SH') else xtconstant.SZ_MARKET
                    cancel_result = xt_trader.cancel_order_stock_sysid(
                        acc, market, order['柜台合同编号'])
                    try:
                        cancel_accepted = int(cancel_result) >= 0
                    except (TypeError, ValueError):
                        cancel_accepted = False
                    if not cancel_accepted:
                        logger.error(f'[撤单失败]: {order}')
                        send_email(f'【撤单失败】股票代码: {order_req["股票代码"]}',
                                   f'撤单失败, 订单信息: {order}')
                        continue
                    cancel_result_dict[order['柜台合同编号']] = cancel_result
                    if order['柜台合同编号'] not in cancelled_order_id_list:
                        cancelled_order_id_list.append(order['柜台合同编号'])
                        with shared_data['撤单次数'].get_lock():
                            shared_data['撤单次数'].value += 1

                # 如果有成功撤单的订单，则更新下单状态
                if cancel_result_dict:
                    with shared_data['股票状态信号'][stock_code]['下单状态'].get_lock():
                        shared_data['股票状态信号'][stock_code][
                            '下单状态'].value = StockOrderStatusInt.CANCELLED
                    logger.info(f'更新下单状态为已撤单: {stock_code}')

                # ----------------------------------- 记录日志 ----------------------------------- #
                timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                msg = f'【委托撤单】股票代码: {order_req["股票代码"]}, 订单: {cancel_result_dict}, 时间: {timestamp_now}'
                msg += '\n' + order_req["操作原因"]
                send_email(f'【委托撤单】股票代码: {order_req["股票代码"]}', msg)
                logger.warning(msg)

            else:
                logger.error(f'未知委托类型: {order_req["委托类型"]}')

        except Empty:
            time.sleep(1)
            if datetime.now().time() >= STOP_TIME:
                logger.warning(f'【退出】交易接口')
                return
        except Exception as e:
            logger.exception(f'【交易模块】异常\t{e}\n{traceback.format_exc()}')
