"""
engine/simulator.py - 模拟交易任务

从 打板策略_v2.4.py 提取的模拟交易执行函数 run_xt_trader_simulator()。
包含：模拟买入（排板/扫板/模拟成交）、模拟撤单。
"""

import time
import json
import traceback
from queue import Empty
from datetime import datetime
from loguru import logger
from xtquant import xtconstant

from config import (
    STOP_TIME, STRATEGY_NAME, MAX_HOLDING_COUNT, MAX_CANCEL_COUNT,
    VOLATILITY_TARGET, VOLATILITY_RATIO_MIN, VOLATILITY_RATIO_MAX,
    WATCHLIST_POSITION_RATIO,
)
from infra.common_enums import OrderType, StockOrderStatusInt
from infra.utils import send_email


def run_xt_trader_simulator(order_queue,
                            shared_data,
                            shadow_signal_mode=False):
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

    cancelled_order_id_list = []

    # ------------------------------- 计算每只股票最大持仓金额 ------------------------------- #
    if shadow_signal_mode:
        total_asset = 10000000  # 1000万
        available_cash = total_asset  # 可用资金
        holding_amount_threshold = 100000  # 10万
    else:
        total_asset = 30000  # 假设总资产为3万
        available_cash = total_asset  # 可用资金
        holding_amount_threshold = total_asset / MAX_HOLDING_COUNT

    logger.info(
        f'[模拟] 当前总资产: {total_asset}, 每只股票最大持仓金额: {holding_amount_threshold}')

    logger.info('[模拟] 交易接口已启动')
    while True:
        try:
            order_req = order_queue.get(timeout=1)
            logger.info(f'[模拟] 订单信息: {order_req}')
            stock_code = order_req['股票代码']
            tick_data = order_req.get('快照')

            if '委托类型' not in order_req:
                logger.error(f'[模拟] 委托类型未指定')
                continue

            # ---------------------------------------------------------------------------- #
            #                                      买入                                     #
            # ---------------------------------------------------------------------------- #
            elif order_req['委托类型'] == OrderType.BUY:
                # ---------------------------------- 查询可用资金 ---------------------------------- #
                order_price = order_req['委托价格']
                min_holding_amount = order_price * 100  # 最小持仓金额为100股
                if available_cash < min_holding_amount:
                    logger.warning(
                        f'[模拟] [余额不足] 可用资金不足以购买100股, 股票代码：{stock_code}, 价格：{order_price}, 可用资金: {available_cash}, 最小持仓金额: {min_holding_amount}'
                    )
                    continue
                if min_holding_amount > holding_amount_threshold * 1.5:
                    logger.warning(
                        f'[模拟] [股价过高] 可用资金不足以购买100股, 股票代码：{stock_code}, 价格：{order_price}, 可用资金: {available_cash}, 最小持仓金额: {min_holding_amount}, 每只股票最大持仓金额: {holding_amount_threshold}'
                    )
                    continue

                logger.debug(f'[模拟] 可用资金: {available_cash}')

                # ---------------------------------- 查询持仓股票 ---------------------------------- #
                if stock_code in shared_data['持仓状态'].keys():
                    logger.warning(
                        f'[模拟] [跳过购买] 当前股票已持仓: {stock_code}, 持仓信息: {shared_data["持仓状态"][stock_code]}'
                    )
                    continue

                # ---------------------------- 查询当日所有的持仓和委托 ---------------------------- #
                if stock_code in shared_data['委托状态'].keys(
                ) and order_req['买入类型'] != '模拟成交':
                    logger.warning(
                        f'[模拟] [跳过购买] 当前股票已委托: {stock_code}, 委托信息: {shared_data["委托状态"][stock_code]}'
                    )
                    continue

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
                        f'[模拟] [波动率调仓] {stock_code} 振幅{stock_amplitude:.2%}, 仓位倍数{volatility_ratio:.2f}, 买入量{order_volume}'
                    )

                # U5升级：观察名单仓位缩减
                watch_list = shared_data.get('观察名单', {})
                if stock_code in watch_list:
                    order_volume = int(order_volume * WATCHLIST_POSITION_RATIO / 100) * 100
                    logger.warning(
                        f'[模拟] [观察名单-仓位缩减] {stock_code} 在观察名单中，仓位缩减至 {WATCHLIST_POSITION_RATIO*100:.0f}%，买入数量: {order_volume}'
                    )
                max_affordable_volume = int(available_cash / order_req['委托价格'] / 100) * 100
                order_volume = min(order_volume, max_affordable_volume)
                if order_volume <= 0:
                    logger.warning(f'[模拟] [跳过购买] 计算后买入数量为0: {stock_code}')
                    continue

                # ------------------------------------ 模拟下单 ------------------------------------ #
                if order_req['买入类型'] == '模拟成交':
                    if stock_code not in shared_data['委托状态'].keys():
                        logger.error(
                            f'[模拟] [成交失败] 当前无可模拟成交委托: {order_req["股票代码"]}')
                        continue

                    # 模拟成交，直接更新共享数据中的持仓状态
                    order = json.loads(shared_data['委托状态'][stock_code])[0]
                    with shared_data['股票状态信号'][stock_code]['下单状态'].get_lock():
                        shared_data['股票状态信号'][stock_code][
                            '下单状态'].value = StockOrderStatusInt.POSITION_HOLDING
                    shared_data['委托状态'].pop(stock_code, None)
                    position_info = {
                        "证券代码": stock_code,
                        "持仓数量": order["委托数量"],
                        "可用数量": 0,
                        "开仓价": order['委托价格'],
                        "市值": order["委托数量"] * tick_data['lastPrice'],
                        "冻结数量": 0,
                        "在途股份": 0,
                        "昨夜拥股": 0,
                        "成本价": order['委托价格'],
                    }
                    shared_data['持仓状态'][stock_code] = json.dumps(
                        position_info, ensure_ascii=False)
                    continue

                # 排板，则更新委托状态
                elif order_req['买入类型'] == '排板':
                    # 更新共享数据中的委托状态
                    order_info = {
                        '委托类型':
                        xtconstant.STOCK_BUY,
                        '证券代码':
                        stock_code,
                        '委托价格':
                        order_req['委托价格'],
                        '报价类型':
                        order_req['报价类型'],
                        '委托数量':
                        order_volume,
                        '订单编号':
                        f'simulated_order_{stock_code}_{datetime.now().strftime("%Y%m%d%H%M%S")}'
                    }

                    shared_data['委托状态'][stock_code] = json.dumps(
                        [order_info], ensure_ascii=False)

                    # --------------------------------- 模拟成交计算使用 --------------------------------- #
                    with shared_data['股票状态信号'][stock_code]['下单时成交量'].get_lock(
                    ):
                        shared_data['股票状态信号'][stock_code][
                            '下单时成交量'].value = tick_data['volume']

                    with shared_data['股票状态信号'][stock_code]['下单时封单量'].get_lock(
                    ):
                        shared_data['股票状态信号'][stock_code][
                            '下单时封单量'].value = tick_data['bidVol'][0]

                # 扫板，则更新持仓状态
                elif order_req['买入类型'] == '扫板':
                    # 更新共享数据中的持仓状态
                    position_info = {
                        "证券代码": stock_code,
                        "持仓数量": order_volume,
                        "可用数量": 0,
                        "开仓价": order_req['委托价格'],
                        "市值": order_volume * order_req['委托价格'],
                        "冻结数量": 0,
                        "在途股份": 0,
                        "昨夜拥股": 0,
                        "成本价": order_req['委托价格'],
                    }
                    shared_data['持仓状态'][stock_code] = json.dumps(
                        position_info, ensure_ascii=False)

                else:
                    logger.error(f'[模拟] 未知买入类型: {order_req["买入类型"]}')
                    continue

                available_cash -= order_volume * order_req['委托价格']

                # ----------------------------------- 记录日志 ----------------------------------- #
                timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                msg = f'[模拟] 【委托买入】股票代码: {order_req["股票代码"]}, 委托数量: {order_volume}, 报价类型: {order_req["报价类型"]}, 委托价格: {order_req["委托价格"]}, 策略名称: {order_req["策略名称"]}, 委托备注: {order_req["委托备注"]}, 时间: {timestamp_now}'
                msg += '\n' + order_req["操作原因"]
                send_email(f'[模拟] 【委托买入】股票代码: {order_req["股票代码"]}', msg)
                logger.warning(msg)

            # ---------------------------------------------------------------------------- #
            #                                      卖出                                     #
            # ---------------------------------------------------------------------------- #
            elif order_req['委托类型'] == OrderType.SELL:
                # 模拟中不会有卖出
                raise NotImplementedError('模拟交易中不支持卖出操作。')

            # ---------------------------------------------------------------------------- #
            #                                      撤单                                     #
            # ---------------------------------------------------------------------------- #
            elif order_req['委托类型'] == OrderType.CANCEL:
                # ---------------------------------- 查询可撤委托 ---------------------------------- #
                if stock_code not in shared_data['委托状态'].keys():
                    logger.warning(
                        f'[模拟] [撤单失败] 当前无可撤单委托: {order_req["股票代码"]}')
                    continue

                # ------------------------------------ 撤单 ------------------------------------ #
                order_list = json.loads(shared_data['委托状态'][stock_code])
                cancel_result_dict = {}
                for order in order_list:
                    available_cash += order['委托价格'] * order['委托数量']
                    # 模拟撤单，直接从共享数据中删除
                    cancel_result = {'订单编号': order['订单编号'], '撤单结果': '成功'}
                    cancel_result_dict[order['订单编号']] = cancel_result

                    if order['订单编号'] not in cancelled_order_id_list:
                        cancelled_order_id_list.append(order['订单编号'])
                        with shared_data['撤单次数'].get_lock():
                            shared_data['撤单次数'].value += 1
                if cancel_result_dict:
                    with shared_data['股票状态信号'][stock_code]['下单状态'].get_lock():
                        shared_data['股票状态信号'][stock_code][
                            '下单状态'].value = StockOrderStatusInt.CANCELLED
                    shared_data['委托状态'].pop(stock_code, None)
                    logger.info(f'[模拟] 更新下单状态为已撤单: {stock_code}')

                # ----------------------------------- 记录日志 ----------------------------------- #
                timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                msg = f'[模拟] 【委托撤单】股票代码: {order_req["股票代码"]}, 订单: {cancel_result_dict}, 时间: {timestamp_now}'
                msg += '\n' + order_req["操作原因"]
                send_email(f'[模拟] 【委托撤单】股票代码: {order_req["股票代码"]}', msg)
                logger.warning(msg)

                with shared_data['股票状态信号'][stock_code]['下单状态'].get_lock():
                    shared_data['股票状态信号'][stock_code][
                        '下单状态'].value = StockOrderStatusInt.CANCELLED
                    logger.info(f'[模拟] 更新下单状态为已撤单: {stock_code}')

                if not shadow_signal_mode:
                    with shared_data['撤单次数'].get_lock():
                        cancel_count = shared_data['撤单次数'].value
                    if cancel_count >= MAX_CANCEL_COUNT:
                        msg = f'[模拟] 【撤单次数过多】已撤单：{cancel_count}次'
                        logger.warning(msg)
                        send_email('[模拟] 撤单次数过多', msg)

            else:
                logger.error(f'[模拟] 未知委托类型: {order_req["委托类型"]}')

        except Empty:
            time.sleep(1)
            if datetime.now().time() >= STOP_TIME:
                logger.warning(f'【退出】模拟交易接口')
                return
        except Exception as e:
            logger.exception(f'【模拟交易模块】异常\t{e}\n{traceback.format_exc()}')
