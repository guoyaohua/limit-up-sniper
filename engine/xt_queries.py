"""
engine/xt_queries.py - XTQuant 查询函数

从 打板策略_v2.4.py 提取的 XTQuant 查询相关函数。
包括：持仓查询、委托查询、资产查询、定时查询任务等。
"""

import sys
import time
import json
import traceback
from datetime import datetime
from threading import RLock
from loguru import logger
import schedule

from config import CLIENT_PATH, STOCK_ACCOUNT, STOP_TIME
from infra.utils import send_email
from core.market_microstructure import merge_active_orders_with_local_reservations


# The live executor and the two-second broker refresh run as threads in the
# same process.  Manager.dict operations are individually safe but a
# snapshot -> clear -> update sequence is not atomic, so protect the sequence
# together with accepted-order reservation writes.
_ORDER_CACHE_LOCK = RLock()


def run_with_effective_orders_locked(shared_data, active_orders, operation):
    """Run one broker submission decision against an atomic order view."""
    with _ORDER_CACHE_LOCK:
        effective = merge_active_orders_with_local_reservations(
            active_orders, dict(shared_data['委托状态'])
        )
        return operation(effective)


def replace_active_order_cache(shared_data, active_orders):
    """Atomically reconcile one broker snapshot into shared order state."""
    with _ORDER_CACHE_LOCK:
        orders = merge_active_orders_with_local_reservations(
            active_orders, dict(shared_data['委托状态'])
        )
        shared_data['委托状态'].clear()
        shared_data['委托状态'].update(orders)
        return orders


def cache_local_buy_reservation(shared_data, stock_code, encoded):
    """Publish an accepted BUY without racing the broker refresh thread."""
    with _ORDER_CACHE_LOCK:
        shared_data['委托状态'][stock_code] = encoded


def xtposition_to_dict(xtpositions):
    """
    将 XtPosition 对象列表转换为以股票代码为键的字典。

    Args:
        xtpositions: 一个包含 XtPosition 对象的列表。

    Returns:
        dict: 一个以股票代码为键，值为 XtPosition 字典的字典。
    """
    position_dict = {}
    for xtposition in xtpositions:
        if xtposition.volume <= 0:
            # 如果持仓数量小于等于0，则跳过该持仓
            continue
        # 将 XtPosition 对象转换为字典
        position_data = {
            "账号类型": xtposition.account_type,  # account_type
            "资金账号": xtposition.account_id,  # account_id
            "证券代码": xtposition.stock_code,  # stock_code
            "持仓数量": xtposition.volume,  # volume
            "可用数量": xtposition.can_use_volume,  # can_use_volume
            "开仓价": xtposition.open_price,  # open_price
            "市值": xtposition.market_value,  # market_value
            "冻结数量": xtposition.frozen_volume,  # frozen_volume
            "在途股份": xtposition.on_road_volume,  # on_road_volume
            "昨夜拥股": xtposition.yesterday_volume,  # yesterday_volume
            "成本价": xtposition.avg_price,  # avg_price
            "多空方向": xtposition.direction,  # direction
        }
        # 按股票代码存储
        position_dict[xtposition.stock_code] = json.dumps(position_data,
                                                          ensure_ascii=False)
    return position_dict


def group_xtorders_by_stock_code(xtorders):
    """
    将 XtOrder 对象列表按股票代码分组并转换为字典。

    Args:
        xtorders: 一个包含 XtOrder 对象的列表。

    Returns:
        dict: 一个以股票代码为键，值为 XtOrder 字典列表的字典。
    """
    grouped_orders = {}
    for xtorder in xtorders:
        # 将 XtOrder 对象转换为字典
        order_dict = {
            "账号类型": xtorder.account_type,  # account_type
            "资金账号": xtorder.account_id,  # account_id
            "证券代码": xtorder.stock_code,  # stock_code
            "订单编号": xtorder.order_id,  # order_id
            "柜台合同编号": xtorder.order_sysid,  # order_sysid
            "报单时间": xtorder.order_time,  # order_time
            "委托类型": xtorder.order_type,  # order_type
            "委托数量": xtorder.order_volume,  # order_volume
            "报价类型": xtorder.price_type,  # price_type
            "委托价格": xtorder.price,  # price
            "成交数量": xtorder.traded_volume,  # traded_volume
            "成交均价": xtorder.traded_price,  # traded_price
            "委托状态": xtorder.order_status,  # order_status
            "状态描述": xtorder.status_msg,  # status_msg
            "策略名称": xtorder.strategy_name,  # strategy_name
            "委托备注": xtorder.order_remark,  # order_remark
            "多空方向": xtorder.direction,  # direction
            "交易操作": xtorder.offset_flag,  # offset_flag
        }
        # 按股票代码分组
        if xtorder.stock_code not in grouped_orders:
            grouped_orders[xtorder.stock_code] = []
        grouped_orders[xtorder.stock_code].append(order_dict)

    for stock_code, orders in grouped_orders.items():
        # 将列表转换为 JSON 字符串
        grouped_orders[stock_code] = json.dumps(orders, ensure_ascii=False)

    return grouped_orders


def xtasset_to_dict(xtasset):
    """
    将 XtAsset 对象转换为字典。

    Args:
        xtasset: 一个 XtAsset 对象。

    Returns:
        dict: 包含账号类型、资金账号、可用金额、冻结金额、持仓市值和总资产的字典。
    """
    return {
        "账号类型": xtasset.account_type,  # account_type
        "资金账号": xtasset.account_id,  # account_id
        "可用金额": xtasset.cash,  # cash
        "冻结金额": xtasset.frozen_cash,  # frozen_cash
        "持仓市值": xtasset.market_value,  # market_value
        "总资产": xtasset.total_asset,  # total_asset
    }


def query_stock_asset(xt_trader, acc):
    ''' 查询股票账户的资产信息。

    资产XtAsset
    属性	类型	注释
    account_type	int	账号类型，参见数据字典
    account_id	str	资金账号
    cash	float	可用金额
    frozen_cash	float	冻结金额
    market_value	float	持仓市值
    total_asset	float	总资产
    '''
    while True:
        asset = xt_trader.query_stock_asset(acc)
        if asset:
            return xtasset_to_dict(asset)
        else:
            logger.error('查询资产失败, 正在重试')
            time.sleep(1)


def query_stock_positions(xt_trader, acc):
    ''' 查询股票账户的持仓信息。

    持仓XtPosition
    属性	类型	注释
    account_type	int	账号类型，参见数据字典
    account_id	str	资金账号
    stock_code	str	证券代码
    volume	int	持仓数量
    can_use_volume	int	可用数量
    open_price	float	开仓价
    market_value	float	市值
    frozen_volume	int	冻结数量
    on_road_volume	int	在途股份
    yesterday_volume	int	昨夜拥股
    avg_price	float	成本价
    direction	int	多空方向，股票不适用；参见数据字典
    '''
    try:
        positions = xt_trader.query_stock_positions(acc)
        if positions:
            return xtposition_to_dict(positions)
        else:
            logger.info('[持仓信息] 查询失败或者当日持仓列表为空')
            return {}
    except Exception as e:
        logger.exception(f'【关键错误】查询股票持仓失败: {e}')
        send_email('【关键错误】查询股票持仓失败',
                   f'查询股票持仓时发生异常: {e}\n{traceback.format_exc()}')
        raise e


def query_stock_orders(xt_trader, acc, cancelable_only=True):
    ''' 查询股票账户的委托订单信息。

    委托XtOrder
    属性	类型	注释
    account_type	int	账号类型，参见数据字典
    account_id	str	资金账号
    stock_code	str	证券代码，例如"600000.SH"
    order_id	int	订单编号
    order_sysid	str	柜台合同编号
    order_time	int	报单时间
    order_type	int	委托类型，参见数据字典
    order_volume	int	委托数量
    price_type	int	报价类型，该字段在返回时为柜台返回类型，不等价于下单传入的price_type，枚举值不一样功能一样，参见数据字典
    price	float	委托价格
    traded_volume	int	成交数量
    traded_price	float	成交均价
    order_status	int	委托状态，参见数据字典
    status_msg	str	委托状态描述，如废单原因
    strategy_name	str	策略名称
    order_remark	str	委托备注
    direction	int	多空方向，股票不适用；参见数据字典
    offset_flag	int	交易操作，用此字段区分股票买卖，期货开、平仓，期权买卖等；参见数据字典
    '''
    try:
        orders = xt_trader.query_stock_orders(acc,
                                              cancelable_only=cancelable_only)
        if orders:
            return group_xtorders_by_stock_code(orders)
        else:
            logger.info(f'[委托订单] 当日{"可撤" if cancelable_only else ""}委托订单列表为空')
            return {}
    except Exception as e:
        logger.exception(f'【关键错误】查询股票委托订单失败: {e}')
        send_email('【关键错误】查询股票委托订单失败',
                   f'查询股票委托订单时发生异常: {e}\n{traceback.format_exc()}')
        raise e


def query_positions_and_orders(xt_trader, acc, shared_data):
    """
    查询持仓和委托订单，并更新共享数据。
    Args:
        xt_trader: XTQuantTrader 实例。
        acc: 资金账号。
        shared_data: 包含股票信息的共享数据字典。
    Returns:
        None
    """
    try:
        logger.info('查询持仓和委托订单')

        # 查询持仓
        positions = query_stock_positions(xt_trader, acc)
        shared_data['持仓状态'].clear()
        shared_data['持仓状态'].update(positions)

        # 查询委托订单
        orders = query_stock_orders(xt_trader, acc, cancelable_only=True)
        # review 20260714: a just-accepted order may not appear in QMT's next
        # cancellable-order snapshot.  Preserve its short-lived local
        # reservation so that refresh cannot momentarily reopen a buy slot.
        orders = replace_active_order_cache(shared_data, orders)

        logger.info(
            f'持仓和委托订单查询完成, 更新共享数据。 持仓状态: {shared_data["持仓状态"]}, 委托状态: {shared_data["委托状态"]}'
        )
    except Exception as e:
        logger.exception(f'【关键错误】查询持仓和委托订单失败: {e}')
        send_email('【关键错误】查询持仓和委托订单失败',
                   f'查询持仓和委托订单时发生异常: {e}\n{traceback.format_exc()}')
        raise e


def safe_query_positions_and_orders(xt_trader, acc, shared_data):
    """查询持仓和委托的安全包装函数"""
    try:
        query_positions_and_orders(xt_trader, acc, shared_data)
    except Exception as e:
        logger.exception(f"定时任务 query_positions_and_orders 执行异常: {e}")
        send_email(
            '【定时任务异常】查询持仓和委托',
            f'定时任务 query_positions_and_orders 执行异常:\n{e}\n{traceback.format_exc()}'
        )


def query_positions_and_orders_task(xt_trader, acc, shared_data):
    """
    定时查询持仓和委托订单，并更新共享数据。
    Args:
        xt_trader: XTQuantTrader 实例。
        acc: 资金账号。
        shared_data: 包含股票信息的共享数据字典。
    Returns:
        None
    """
    logger.info('开启持仓和委托查询定时任务')

    # 设置定时任务 - 使用safe wrapper
    schedule.every(2).seconds.do(safe_query_positions_and_orders, xt_trader,
                                 acc, shared_data)

    try:
        while True:
            # 检查停止时间
            if datetime.now().time() >= STOP_TIME:
                logger.warning('【进程退出】退出持仓和委托查询任务')
                break
            # 执行定时任务
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.warning('【进程退出】退出持仓和委托查询任务')
    except Exception as e:
        logger.exception(f'【市场情绪监控任务】异常\t{e}\n{traceback.format_exc()}')
    finally:
        # 清理定时任务
        schedule.clear()
        logger.warning('清理定时任务，退出持仓和委托查询任务')
