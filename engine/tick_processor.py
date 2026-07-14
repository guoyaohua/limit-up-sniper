"""
engine/tick_processor.py - Tick 数据处理与全推行情订阅

从 打板策略_v2.4.py 提取的 on_data 回调、process_tick_data 处理循环、
check_order_successed 模拟成交判断、create_whole_quote_task 订阅任务。
"""

import json
import time
import traceback
import zlib
from queue import Empty, Full
from functools import partial
from multiprocessing import current_process, Value
from datetime import datetime, time as dt_time
from loguru import logger

from config import (
    STOP_TIME, STRATEGY_NAME, DEBUG_MODE,
    LATENCY_THRESHOLD,
    MAX_UP_LIMIT_BREAK_COUNT, MAX_UP_LIMIT_BREAK_TIME,
    MAX_CANCEL_COUNT,
    WATCHLIST_RELEASE_MINUTES, WATCHLIST_RELEASE_TURNOVER,
)
from infra.xtconstant_compat import xtconstant
from infra.common_enums import (
    OrderType, StockOrderStatusInt, StockLimitStatusInt,
)
from infra.utils import send_email
from infra.data_helpers import (
    is_trading_time, _calc_delay_time, _check_same_price,
    _calc_limit_up_break_duration,
)
from infra.task_manager import CallbackHeartbeatMonitor
from core.decisions import (
    should_buy, should_cancel, should_sell,
    calculate_limit_up_sweep_capital,
)
from core.trailing_stop import calculate_trailing_stop_prices
from infra.trade_log import record_strategy_event
from engine.queue_fill import queued_buy_fill_progress
from core.market_microstructure import is_sealed_limit_up_quote


# 全局回调心跳监控器（用于监控 xtdata 回调是否正常）
_callback_heartbeat_monitor = None


def get_callback_heartbeat_monitor(
        timeout: float = 30) -> CallbackHeartbeatMonitor:
    """获取全局回调心跳监控器实例"""
    global _callback_heartbeat_monitor
    if _callback_heartbeat_monitor is None:
        _callback_heartbeat_monitor = CallbackHeartbeatMonitor(
            name="xtdata_whole_quote_callback", timeout=timeout)
    return _callback_heartbeat_monitor


# ---------------------------------------------------------------------------- #
#                               on_data 回调函数                                #
# ---------------------------------------------------------------------------- #

def on_data(datas,
            tick_queue,
            shadow_tick_queue,
            stock_info_dict,
            stop_flag,
            heartbeat_monitor=None,
            paper_market_queue=None,
            shadow_market_queue=None):
    """分笔行情回调函数

    tick - 分笔数据
        'time'                  #时间戳
        'lastPrice'             #最新价
        'open'                  #开盘价
        'high'                  #最高价
        'low'                   #最低价
        'lastClose'             #前收盘价
        'amount'                #成交总额
        'volume'                #成交总量
        'pvolume'               #原始成交总量
        'stockStatus'           #证券状态
        'openInt'               #持仓量
        'lastSettlementPrice'   #前结算
        'askPrice'              #委卖价
        'bidPrice'              #委买价
        'askVol'                #委卖量
        'bidVol'                #委买量
        'transactionNum'        #成交笔数

    Args:
        datas: tick数据字典
        tick_queue: tick数据队列
        shadow_tick_queue: 影子模式tick数据队列
        stock_info_dict: 股票信息字典
        stop_flag: 停止标志
        heartbeat_monitor: 回调心跳监控器（可选）
    """
    try:
        # 更新心跳（如果有监控器）
        if heartbeat_monitor is not None:
            heartbeat_monitor.update()

        _dispatch_ticks(datas, tick_queue)
        if shadow_tick_queue:
            _dispatch_ticks(datas, shadow_tick_queue)
        if paper_market_queue is not None:
            _put_latest_market(paper_market_queue, datas)
        if shadow_market_queue is not None:
            _put_latest_market(shadow_market_queue, datas)

        # 记录日志
        stock_code = list(datas.keys())[0]
        time_now = datetime.now().strftime('%H:%M')
        latency = _calc_delay_time(datas[stock_code]['time'])
        queue_size = _queue_size(tick_queue)
        msg = f'【回调】【Tick】延迟：{latency}s，数据大小：{len(datas)}，队列大小：{queue_size}'
        logger.debug(msg)

        if latency > LATENCY_THRESHOLD and (
            (time_now > '09:31' and time_now < '11:30') or
            (time_now > '13:00' and time_now < '14:57')):
            msg = f'【回调】【Tick】{stock_code} {stock_info_dict[stock_code]["股票名称"]} 延迟：{latency}s, 超过阈值{LATENCY_THRESHOLD}s, 重新订阅'
            logger.error(msg)
            stop_flag.value = True

    except Exception as e:
        logger.exception(
            f'【关键错误】Tick数据处理失败：{e}, 数据大小：{len(datas) if datas else 0}')
        # 记录错误到心跳监控器
        if heartbeat_monitor is not None:
            heartbeat_monitor.record_error()


def _queue_partition(stock_code, partition_count):
    """Stable queue partition independent of Python's randomized hash."""
    if partition_count <= 0:
        raise ValueError('partition_count 必须大于 0')
    return zlib.crc32(stock_code.encode('utf-8')) % partition_count


def _dispatch_ticks(datas, queues):
    """Dispatch every stock to one deterministic FIFO queue.

    Consecutive ticks for the same stock can otherwise be processed by
    different workers in reverse completion order, corrupting previous-price,
    break/reseal and shrinking-offer signals.  A single Queue is still
    accepted for API compatibility.
    """
    if isinstance(queues, (list, tuple)):
        buckets = {}
        for stock_code, tick in datas.items():
            partition = _queue_partition(stock_code, len(queues))
            buckets.setdefault(partition, {})[stock_code] = tick
        for partition, payload in buckets.items():
            queues[partition].put(payload)
        return
    queues.put(datas)


def _queue_size(queues):
    if isinstance(queues, (list, tuple)):
        total = 0
        for queue in queues:
            try:
                total += queue.qsize()
            except (AttributeError, NotImplementedError):
                pass
        return total
    try:
        return queues.qsize()
    except (AttributeError, NotImplementedError):
        return -1


def _put_latest_market(market_queue, datas):
    """Keep simulation marks fresh without ever blocking XTQuant callback."""
    payload = dict(datas)
    try:
        market_queue.put_nowait(payload)
    except Full:
        try:
            market_queue.get_nowait()
        except Empty:
            pass
        try:
            market_queue.put_nowait(payload)
        except Full:
            pass


# ---------------------------------------------------------------------------- #
#                             模拟成交判断                                       #
# ---------------------------------------------------------------------------- #

def check_order_successed(shared_data,
                          stock_code,
                          tick_data,
                          is_limit_up,
                          strong_stocks=None,
                          order_status=None,
                          stock_status=None):
    """Conservatively confirm a queued limit-up buy.

    XTQuant's cumulative ``volume`` and level-one ``bidVol`` are both in lots.
    A falling bid queue is not execution evidence because cancellations are
    indistinguishable from trades in a snapshot.  Therefore an order is only
    considered filled while the stock is still sealed and cumulative trades
    since submission cover both the queue ahead and our complete order.
    """
    # 性能优化：使用缓存的数据引用
    if strong_stocks is None:
        strong_stocks = shared_data['强势股票']
    if order_status is None:
        order_status = shared_data['委托状态']
    if stock_status is None:
        stock_status = shared_data['股票状态信号'][stock_code]

    if stock_code not in strong_stocks:
        return False

    if stock_code not in order_status.keys():
        return False

    try:
        encoded_orders = order_status[stock_code]
        orders = (json.loads(encoded_orders)
                  if isinstance(encoded_orders, str) else encoded_orders)
        pending_order = orders[0]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        # Restored legacy orders do not have an auditable queue snapshot.  Fail
        # closed rather than manufacturing a profitable fill.
        logger.warning(f'[模拟] {stock_code} 排板委托缺少完整队列快照，保持未成交')
        return False

    if not is_limit_up:
        # Once the board opens, the original queue position is no longer
        # auditable.  Persist the invalidation before should_cancel() enqueues
        # its asynchronous cancel so a fast reseal cannot resurrect the order.
        pending_order["排队已失效"] = True
        pending_order["排队失效原因"] = "limit-up opened before confirmed fill"
        try:
            order_status[stock_code] = json.dumps(orders, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.warning(f'[模拟] {stock_code} 无法持久化排队失效状态')
        logger.info(f'[模拟] {stock_code} 已开板；排队位置作废并保持未成交')
        return False

    progress = queued_buy_fill_progress(
        pending_order, tick_data, is_limit_up=is_limit_up
    )
    if progress.confirmed:
        logger.warning(
            f'[模拟] {stock_code} 排队成交证据充分：新增成交 '
            f'{progress.traded_lots:g} 手 >= 所需 {progress.required_lots:g} 手'
        )
        return True

    logger.info(
        f'[模拟] {stock_code} 排队未成交：新增成交 {progress.traded_lots:g} 手 '
        f'< 所需 {progress.required_lots:g} 手（{progress.reason}；封单减少不计成交）'
    )

    return False


def _increment_review_counter(counter_dict, key, step=1):
    """累计复盘计数器。"""
    if counter_dict is None:
        return
    counter_dict[key] = counter_dict.get(key, 0) + step



def _update_decision_tag(decision_tags, stock_code, decision, reason, extra=None):
    """更新最近一次决策标签。"""
    if decision_tags is None:
        return
    payload = {
        'decision': decision,
        'reason': reason,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f'),
    }
    if extra:
        payload.update(extra)
    decision_tags[stock_code] = payload



def _check_watchlist_release(shared_data,
                             stock_code,
                             tick_data,
                             stock_info,
                             watch_list,
                             watchlist_metadata,
                             decision_tags,
                             review_counters):
    """检查观察名单自动释放。"""
    if stock_code not in watch_list:
        return

    metadata = watchlist_metadata.get(stock_code, {}) if watchlist_metadata is not None else {}
    entry_timestamp = metadata.get('enter_timestamp')
    if entry_timestamp is None:
        try:
            parts = str(watch_list[stock_code]).split('|')
            entry_timestamp = float(parts[1]) if len(parts) == 2 else None
        except Exception:
            entry_timestamp = None

    if entry_timestamp is None:
        return

    elapsed_minutes = (time.time() - entry_timestamp) / 60
    float_shares = stock_info.get('流通股本', 0)
    turnover_rate = tick_data.get('pvolume', 0) / float_shares * 100 if float_shares else 0

    if elapsed_minutes < WATCHLIST_RELEASE_MINUTES or turnover_rate < WATCHLIST_RELEASE_TURNOVER:
        return

    stock_name = stock_info['股票名称']
    watch_list.pop(stock_code, None)
    if watchlist_metadata is not None:
        watchlist_metadata.pop(stock_code, None)

    reason = (
        f'观察名单释放：已观察{elapsed_minutes:.1f}分钟，换手率恢复至{turnover_rate:.1f}%'
    )
    _update_decision_tag(
        decision_tags,
        stock_code,
        'watchlist_release',
        reason,
        extra={
            'elapsed_minutes': round(elapsed_minutes, 2),
            'turnover_rate': round(turnover_rate, 4),
        })
    _increment_review_counter(review_counters, 'watchlist_release_count')
    record_strategy_event(
        shared_data,
        event_type='watchlist_release',
        stock_code=stock_code,
        stock_name=stock_name,
        reason=reason,
        snapshot=tick_data,
        extra={
            'elapsed_minutes': round(elapsed_minutes, 2),
            'turnover_rate': round(turnover_rate, 4),
        })
    logger.info(f'[观察名单解除] {stock_code} {stock_name} {reason}')



def _start_break_episode(shared_data,
                         stock_code,
                         stock_info,
                         tick_data,
                         break_episode_state,
                         review_counters):
    """开始新的炸板 episode。"""
    if break_episode_state is None:
        return

    episode = break_episode_state.get(stock_code, {})
    start_ts = tick_data['time']
    episode.update({
        'stock_name': stock_info['股票名称'],
        'first_limit_time': episode.get('first_limit_time'),
        'last_limit_time': episode.get('last_limit_time'),
        'episode_start': start_ts,
        'episode_end': None,
        'last_break_time': start_ts,
        'last_reseal_time': episode.get('last_reseal_time'),
        'episode_duration': 0,
        'fast_reseal_count': episode.get('fast_reseal_count', 0),
        'deep_break_count': episode.get('deep_break_count', 0),
        'break_count': episode.get('break_count', 0) + 1,
        'is_active': True,
    })

    limit_price = stock_info['涨停价']
    drawdown = 0.0
    if limit_price:
        drawdown = max(0.0, (limit_price - tick_data['lastPrice']) / limit_price)
    if drawdown >= 0.03:
        episode['deep_break_count'] = episode.get('deep_break_count', 0) + 1

    break_episode_state[stock_code] = episode
    _increment_review_counter(review_counters, 'break_episode_start_count')
    record_strategy_event(
        shared_data,
        event_type='break_episode_start',
        stock_code=stock_code,
        stock_name=stock_info['股票名称'],
        reason='涨停打开，开始记录炸板 episode',
        snapshot=tick_data,
        extra={
            'break_count': episode['break_count'],
            'deep_break_count': episode['deep_break_count'],
            'drawdown_from_limit': round(drawdown, 4),
        })



def _finish_break_episode(shared_data,
                          stock_code,
                          stock_info,
                          tick_data,
                          break_episode_state,
                          review_counters):
    """结束炸板 episode。"""
    if break_episode_state is None or stock_code not in break_episode_state:
        return None

    episode = dict(break_episode_state.get(stock_code, {}))
    if not episode.get('episode_start'):
        return episode

    duration = _calc_limit_up_break_duration(tick_data['time'], episode['episode_start'])
    episode['episode_end'] = tick_data['time']
    episode['episode_duration'] = duration
    episode['last_reseal_time'] = tick_data['time']
    episode['is_active'] = False
    if duration <= 60:
        episode['fast_reseal_count'] = episode.get('fast_reseal_count', 0) + 1

    break_episode_state[stock_code] = episode
    _increment_review_counter(review_counters, 'break_episode_end_count')
    record_strategy_event(
        shared_data,
        event_type='break_episode_end',
        stock_code=stock_code,
        stock_name=stock_info['股票名称'],
        reason=f'回封完成，开板时长 {int(duration)} 秒',
        snapshot=tick_data,
        extra={
            'duration_seconds': int(duration),
            'fast_reseal_count': episode.get('fast_reseal_count', 0),
            'deep_break_count': episode.get('deep_break_count', 0),
        })
    return episode


# ---------------------------------------------------------------------------- #
#                           process_tick_data 处理循环                           #
# ---------------------------------------------------------------------------- #

def process_tick_data(shared_data,
                      tick_queue,
                      order_queue,
                      shadow_signal_mode=False):
    '''全推行情数据处理函数，分笔数据
    '''

    logger.info('开启处理tick数据进程')

    # 性能优化：缓存常用数据引用，减少字典查找开销
    stock_info_dict = shared_data['股票信息']
    stock_status_signals = shared_data['股票状态信号']
    market_sentiment_score = shared_data['市场情绪_评分']
    limit_up_pool = shared_data['涨停池']
    break_pool = shared_data['炸板池']
    blacklist = shared_data['黑名单']
    strong_stocks = shared_data['强势股票']
    concept_sector_effect = shared_data['概念板块效应']
    industry_sector_effect = shared_data['行业板块效应']
    individual_capital_inflow = shared_data['个股资金流入']
    holding_status = shared_data['持仓状态']
    cancel_count = shared_data['撤单次数']
    break_count = shared_data['开板次数']
    max_break_time = shared_data['最大开板回封时间']
    pre_market_holdings = shared_data['盘前持仓']
    order_status = shared_data['委托状态']
    watch_list = shared_data.get('观察名单', {})
    watchlist_metadata = shared_data.get('观察名单元数据')
    break_episode_state = shared_data.get('炸板episode状态')
    review_counters = shared_data.get('复盘统计计数器')
    decision_tags = shared_data.get('决策原因标签')

    while True:
        try:
            start_time = time.time()
            datas = tick_queue.get(timeout=1)

            # 丢弃每次订阅时传递的旧tick数据
            if not is_trading_time():
                logger.info(f"当前不在交易时间，跳过当前tick数据。{datas}")
                continue

            # 处理tick数据
            for stock_code in datas:
                order = {}
                data = datas[stock_code]

                # 性能优化：使用缓存版本的时间转换
                # time_now = _conv_time_cached(data['time'], fmt='%H:%M')

                # 缓存单只股票的常用信息，减少重复查找
                stock_info = stock_info_dict[stock_code]
                stock_status = stock_status_signals[stock_code]
                stock_name = stock_info["股票名称"]
                limit_up_price = stock_info["涨停价"]
                down_limit_price = stock_info["跌停价"]
                is_near_limit_up = False
                is_limit_up = False
                is_down_limit = False
                # 去除集合竞价时间
                if data.get('openInt', 0) != 13 and data.get('openInt',
                                                             0) != 15:
                    # 跳过集合竞价
                    logger.info(f'跳过集合竞价，{data}')
                    continue
                else:
                    _check_watchlist_release(shared_data,
                                             stock_code,
                                             data,
                                             stock_info,
                                             watch_list,
                                             watchlist_metadata,
                                             decision_tags,
                                             review_counters)

                    # 性能优化：使用缓存的价格比较函数和预先缓存的价格
                    # 涨停状态, 真实封板, 买一价为涨停价
                    # review 20260714: use the same conservative sealed-board
                    # definition in live signals, simulation and backtests.
                    is_limit_up = is_sealed_limit_up_quote(data, limit_up_price)

                    # 触及涨停价，委卖价格不为空，且最大委卖价格等于涨停价，卖一价格为涨停价
                    is_near_limit_up = (not is_limit_up and data['askPrice']
                                        and _check_same_price(
                                            data['askPrice'][0],
                                            limit_up_price))

                    is_down_limit = (not is_limit_up and data['askPrice']
                                     and _check_same_price(
                                         data['askPrice'][0],
                                         down_limit_price))

                    # ---------------------------------- 更新股票状态 --------------------------------- #
                    if is_limit_up:
                        # 1. 维持涨停
                        with stock_status['股票状态'].get_lock():
                            current_stock_status = stock_status['股票状态'].value
                        if current_stock_status == StockLimitStatusInt.LIMIT_UP:
                            # 已经是涨停状态，仅更新封单金额
                            # 封单金额
                            limit_up_amount = data['bidVol'][0] * data[
                                'bidPrice'][0] * 100
                            # 优化封单金额变化率计算，避免除零错误
                            with stock_status['封单金额'].get_lock():
                                previous_amount = stock_status['封单金额'].value
                            if previous_amount > 0:
                                change_rate = (
                                    limit_up_amount -
                                    previous_amount) / previous_amount
                            else:
                                change_rate = 0 if limit_up_amount == 0 else 1.0  # 从0变为正数视为100%增长

                            with stock_status['封单金额变化率'].get_lock():
                                stock_status['封单金额变化率'].value = change_rate
                            with stock_status['封单金额'].get_lock():
                                stock_status['封单金额'].value = limit_up_amount

                            logger.debug(
                                f'{stock_code} 封单金额变化率 {change_rate:.2%}, 当前封单金额 {limit_up_amount}, 快照: {data}'
                            )

                        # 2. 首次涨停
                        elif current_stock_status == StockLimitStatusInt.NOT_LIMIT_UP:
                            with stock_status['股票状态'].get_lock():
                                stock_status[
                                    '股票状态'].value = StockLimitStatusInt.LIMIT_UP
                            with stock_status['封单金额'].get_lock():
                                stock_status['封单金额'].value = data['bidVol'][
                                    0] * data['bidPrice'][0] * 100

                            if stock_code not in limit_up_pool:
                                limit_up_pool[stock_code] = f'{data["time"]},'
                            else:
                                # 追加涨停时间
                                limit_up_pool[stock_code] += f'{data["time"]},'

                            if break_episode_state is not None:
                                previous_episode = dict(
                                    break_episode_state.get(stock_code, {}))
                                previous_episode.update({
                                    'stock_name': stock_name,
                                    'first_limit_time': previous_episode.get('first_limit_time', data['time']),
                                    'last_limit_time': data['time'],
                                    'is_active': False,
                                })
                                break_episode_state[stock_code] = previous_episode

                            _increment_review_counter(review_counters,
                                                      'limit_up_count')
                            record_strategy_event(
                                shared_data,
                                event_type='limit_up',
                                stock_code=stock_code,
                                stock_name=stock_name,
                                reason='首次涨停',
                                snapshot=data,
                                extra={'limit_price': limit_up_price})

                            logger.info(
                                f'{stock_code} {stock_info["股票名称"]} 涨停，当前价格：{data["bidPrice"][0]}，涨停价：{limit_up_price}'
                            )

                        # 3. 涨停回封
                        elif current_stock_status == StockLimitStatusInt.LIMIT_UP_BROKEN:
                            with stock_status['股票状态'].get_lock():
                                stock_status[
                                    '股票状态'].value = StockLimitStatusInt.LIMIT_UP
                            with stock_status['封单金额'].get_lock():
                                stock_status['封单金额'].value = data['bidVol'][
                                    0] * data['bidPrice'][0] * 100
                            limit_up_pool[stock_code] += f'{data["time"]},'

                            # 开板时长
                            limit_up_break_duration = _calc_limit_up_break_duration(
                                data['time'],
                                break_pool[stock_code][:-1].split(',')[-1])

                            if limit_up_break_duration <= 60:
                                break_count[
                                    stock_code] -= 1  # 如果开板时间小于60秒，暂不记录开板次数

                            if (stock_code not in max_break_time
                                    or limit_up_break_duration >
                                    max_break_time[stock_code]):
                                max_break_time[
                                    stock_code] = limit_up_break_duration

                            episode = _finish_break_episode(shared_data,
                                                            stock_code,
                                                            stock_info,
                                                            data,
                                                            break_episode_state,
                                                            review_counters)
                            if episode is not None:
                                episode['last_limit_time'] = data['time']
                                break_episode_state[stock_code] = episode

                            logger.info(
                                f'{stock_code} {stock_info["股票名称"]} 回封涨停，开板时长:{int(limit_up_break_duration)}秒, 当前价格：{data["bidPrice"][0]}，涨停价：{limit_up_price}'
                            )

                    else:
                        # 1. 炸板
                        with stock_status['股票状态'].get_lock():
                            current_stock_status_for_break = stock_status[
                                '股票状态'].value
                        if current_stock_status_for_break == StockLimitStatusInt.LIMIT_UP:
                            # 涨停开板
                            with stock_status['股票状态'].get_lock():
                                stock_status[
                                    '股票状态'].value = StockLimitStatusInt.LIMIT_UP_BROKEN
                            with stock_status['封单金额'].get_lock():
                                stock_status['封单金额'].value = 0.0

                            # 记录炸板时间
                            if stock_code not in break_pool:
                                break_pool[stock_code] = f'{data["time"]},'
                            else:
                                # 追加炸板时间
                                break_pool[stock_code] += f'{data["time"]},'

                            if stock_code in break_count:
                                break_count[stock_code] += 1
                            else:
                                break_count[stock_code] = 1

                            _start_break_episode(shared_data,
                                                 stock_code,
                                                 stock_info,
                                                 data,
                                                 break_episode_state,
                                                 review_counters)

                            logger.info(
                                f'{stock_code} {stock_info["股票名称"]} 开板，当前价格：{data["bidPrice"][0]}，涨停价：{limit_up_price}'
                            )

                        # 2. 开板未回封
                        elif current_stock_status_for_break == StockLimitStatusInt.LIMIT_UP_BROKEN:
                            # 开板时长
                            limit_up_break_duration = _calc_limit_up_break_duration(
                                data['time'],
                                break_pool[stock_code][:-1].split(',')[-1])

                            if (stock_code not in max_break_time
                                    or limit_up_break_duration >
                                    max_break_time[stock_code]):
                                max_break_time[
                                    stock_code] = limit_up_break_duration

                            # 如果开板次数超过阈值，或者开板时间超过阈值，则加入黑名单
                            up_limit_break_count = break_count[stock_code]
                            if limit_up_break_duration <= 60:
                                up_limit_break_count -= 1  # 如果开板时间小于60秒，暂不记录开板次数

                            episode = dict(
                                break_episode_state.get(stock_code, {})) if break_episode_state is not None else {}
                            episode['episode_duration'] = limit_up_break_duration
                            limit_drawdown = max(
                                0.0,
                                (limit_up_price - data['lastPrice']) / limit_up_price
                            ) if limit_up_price else 0.0
                            episode['latest_drawdown_from_limit'] = limit_drawdown
                            if break_episode_state is not None:
                                break_episode_state[stock_code] = episode

                            # 开板次数过多，加入黑名单
                            if up_limit_break_count >= MAX_UP_LIMIT_BREAK_COUNT:
                                # 加入黑名单
                                msg = f'[黑名单] {stock_code} {stock_info["股票名称"]} 开板次数过多，加入黑名单，开板次数：{up_limit_break_count}'
                                if stock_code not in blacklist:
                                    blacklist[stock_code] = msg
                                    logger.warning(msg)
                                    record_strategy_event(
                                        shared_data,
                                        event_type='blacklist_enter',
                                        stock_code=stock_code,
                                        stock_name=stock_name,
                                        reason=msg,
                                        snapshot=data,
                                        extra={
                                            'blacklist_reason': 'break_count',
                                            'break_count': up_limit_break_count,
                                        })
                                    send_email(
                                        f'【黑名单】{stock_code} {stock_info["股票名称"]}',
                                        msg)

                            # 开板时间过长，加入黑名单
                            if max_break_time[
                                    stock_code] >= MAX_UP_LIMIT_BREAK_TIME:
                                # 加入黑名单
                                msg = f'[黑名单] {stock_code} {stock_info["股票名称"]} 开板时间过长，加入黑名单，最大开板时长：{int(max_break_time[stock_code])}秒'
                                if stock_code not in blacklist:
                                    blacklist[stock_code] = msg
                                    logger.warning(msg)
                                    record_strategy_event(
                                        shared_data,
                                        event_type='blacklist_enter',
                                        stock_code=stock_code,
                                        stock_name=stock_name,
                                        reason=msg,
                                        snapshot=data,
                                        extra={
                                            'blacklist_reason': 'break_duration',
                                            'max_break_duration': int(max_break_time[stock_code]),
                                        })
                                    send_email(
                                        f'【黑名单】{stock_code} {stock_info["股票名称"]}',
                                        msg)

                            # 如果当前股价下跌超3%，且开板时间超过10分钟，则加入黑名单
                            if (float(data['lastPrice'] / limit_up_price) <
                                    0.97 and limit_up_break_duration >
                                    MAX_UP_LIMIT_BREAK_TIME / 2):
                                msg = f'[黑名单] {stock_code} {stock_info["股票名称"]} 开板后股价下跌超过3%，加入黑名单，当前价格：{data["lastPrice"]}, 涨停价：{limit_up_price}'
                                if stock_code not in blacklist:
                                    blacklist[stock_code] = msg
                                    logger.warning(msg)
                                    record_strategy_event(
                                        shared_data,
                                        event_type='blacklist_enter',
                                        stock_code=stock_code,
                                        stock_name=stock_name,
                                        reason=msg,
                                        snapshot=data,
                                        extra={
                                            'blacklist_reason': 'break_drawdown',
                                            'drawdown_from_limit': round(limit_drawdown, 4),
                                            'break_duration': int(limit_up_break_duration),
                                        })
                                    send_email(
                                        f'【黑名单】{stock_code} {stock_info["股票名称"]}',
                                        msg)

                    # --------------------------- 更新区间最高价并计算移动止损价格 -------------------------- #
                    if stock_code in pre_market_holdings and stock_code in holding_status:
                        with stock_status['最高价'].get_lock():
                            current_highest = stock_status['最高价'].value

                        new_price = down_limit_price if is_down_limit else data[
                            'bidPrice'][0]

                        if new_price > current_highest:
                            with stock_status['最高价'].get_lock():
                                stock_status['最高价'].value = new_price

                            logger.info(f"[{stock_code}] 更新最高价为: {new_price}")

                            # 重新计算止盈止损价格
                            calculate_trailing_stop_prices(
                                highest_price=new_price,
                                limit_down_price=down_limit_price,  # 跌停价
                                stock_code=stock_code,
                                shared_data=shared_data)

                    # ---------------------------------------------------------------------------- #
                    #                                  模拟 - 检查是否成交                          #
                    # ---------------------------------------------------------------------------- #
                    if (DEBUG_MODE
                            or shadow_signal_mode) and check_order_successed(
                                shared_data, stock_code, data, is_limit_up,
                                strong_stocks, order_status, stock_status):
                        _order = {
                            '委托类型': OrderType.BUY,
                            '股票代码': stock_code,
                            '委托价格': limit_up_price,
                            '报价类型': xtconstant.FIX_PRICE,
                            '策略名称': STRATEGY_NAME,
                            '委托备注': '买入',
                            '买入类型': '模拟成交',
                            '快照': data  # 附带当前Tick快照
                        }
                        logger.warning(f'模拟成交订单: {_order}')
                        order_queue.put(_order)

                    # ---------------------------------------------------------------------------- #
                    #                                    决策信号生成                               #
                    # ---------------------------------------------------------------------------- #
                    if should_buy(shared_data,
                                  data,
                                  stock_code,
                                  is_limit_up,
                                  is_near_limit_up,
                                  stock_info,
                                  stock_status,
                                  market_sentiment_score,
                                  blacklist,
                                  strong_stocks,
                                  holding_status,
                                  limit_up_pool,
                                  concept_sector_effect,
                                  industry_sector_effect,
                                  individual_capital_inflow,
                                  cancel_count,
                                  shadow_signal_mode=shadow_signal_mode,
                                  order=order):
                        # ------------------------------------ 买入 ------------------------------------ #
                        with cancel_count.get_lock():
                            cancel_count_val = cancel_count.value
                        if is_limit_up and cancel_count_val > MAX_CANCEL_COUNT:
                            logger.warning(
                                f'{stock_code} 撤单次数超过{MAX_CANCEL_COUNT}次，跳过排板买入'
                            )
                            continue

                        order.update({
                            '委托类型': OrderType.BUY,
                            '股票代码': stock_code,
                            '委托价格': limit_up_price,
                            '报价类型': xtconstant.FIX_PRICE,
                            '策略名称': STRATEGY_NAME,
                            '委托备注': '买入',
                            '买入类型': '排板' if is_limit_up else '扫板',
                            '快照': data  # 附带当前Tick快照
                        })
                        order_queue.put(order)
                        with stock_status['下单状态'].get_lock():
                            stock_status[
                                '下单状态'].value = StockOrderStatusInt.ORDERED_BUY
                        _update_decision_tag(decision_tags,
                                             stock_code,
                                             'buy_decision',
                                             order.get('操作原因', ''),
                                             extra={
                                                 'buy_type': order.get('买入类型', '排板' if is_limit_up else '扫板')
                                             })
                        _increment_review_counter(review_counters,
                                                  'buy_decision_count')
                        record_strategy_event(
                            shared_data,
                            event_type='buy_decision',
                            stock_code=stock_code,
                            stock_name=stock_name,
                            reason=order.get('操作原因', ''),
                            snapshot=data,
                            extra={
                                'buy_type': order.get('买入类型', '排板' if is_limit_up else '扫板'),
                                'is_limit_up': is_limit_up,
                                'is_near_limit_up': is_near_limit_up,
                                'limit_up_price': limit_up_price,
                            })

                    elif should_cancel(shared_data,
                                       data,
                                       stock_code,
                                       is_limit_up,
                                       is_down_limit,
                                       stock_info,
                                       stock_status,
                                       market_sentiment_score,
                                       blacklist,
                                       concept_sector_effect,
                                       industry_sector_effect,
                                       individual_capital_inflow,
                                       holding_status,
                                       order_status,
                                       shadow_signal_mode=shadow_signal_mode,
                                       order=order):
                        # ------------------------------------ 撤单 ------------------------------------ #
                        order.update({
                            '委托类型': OrderType.CANCEL,
                            '股票代码': stock_code,
                            '策略名称': STRATEGY_NAME,
                            '委托备注': '撤单',
                            '快照': data  # 附带当前Tick快照
                        })
                        order_queue.put(order)
                        _update_decision_tag(decision_tags,
                                             stock_code,
                                             'cancel_decision',
                                             order.get('操作原因', ''))
                        _increment_review_counter(review_counters,
                                                  'cancel_decision_count')
                        record_strategy_event(
                            shared_data,
                            event_type='cancel_decision',
                            stock_code=stock_code,
                            stock_name=stock_name,
                            reason=order.get('操作原因', ''),
                            snapshot=data,
                            extra={'is_limit_up': is_limit_up})

                    elif should_sell(shared_data=shared_data,
                                     stock_code=stock_code,
                                     tick_data=data,
                                     is_down_limit=is_down_limit,
                                     is_near_limit_up=is_near_limit_up,
                                     is_limit_up=is_limit_up,
                                     down_limit_price=down_limit_price,
                                     stock_status=stock_status,
                                     stock_info=stock_info,
                                     holding_status=holding_status,
                                     pre_market_holdings=pre_market_holdings,
                                     order=order):
                        # ----------------------------------- 卖出 ------------------------------------ #
                        order_queue.put(order)
                        _update_decision_tag(decision_tags,
                                             stock_code,
                                             'sell_decision',
                                             order.get('操作原因', ''))
                        _increment_review_counter(review_counters,
                                                  'sell_decision_count')
                        record_strategy_event(
                            shared_data,
                            event_type='sell_decision',
                            stock_code=stock_code,
                            stock_name=stock_name,
                            reason=order.get('操作原因', ''),
                            snapshot=data,
                            extra={
                                'order_remark': order.get('委托备注', ''),
                                'target_remaining_volume': order.get('剩余仓位'),
                            })

                # ----------------------------- 记录前买一价格及扫板所需资金等 ----------------------------- #
                with stock_status['前一价格'].get_lock():
                    stock_status['前一价格'].value = data['bidPrice'][0] if data[
                        'bidPrice'] else data['lastPrice']
                with stock_status['拉板所需资金'].get_lock():
                    stock_status['拉板所需资金'].value = (
                        0.0 if not is_near_limit_up else
                        calculate_limit_up_sweep_capital(data, limit_up_price))

            # ----------------------------------- 记录日志 ----------------------------------- #
            if datas:  # 性能优化：只在有数据时记录日志
                stock_code = list(datas.keys())[0]
                latency = _calc_delay_time(datas[stock_code]['time'])
                cost_time = round(time.time() - start_time, 2)
                logger.debug(
                    f'【Tick数据处理】延迟：{latency}s\t总耗时：{cost_time}\t大小：{len(datas.keys())}'
                )

        except Empty:
            time.sleep(1)
            if datetime.now().time() >= STOP_TIME:
                logger.warning(f'【进程退出】{current_process().name}')
                return
        except Exception as e:
            logger.exception(f'处理tick数据失败：{e}\n{traceback.format_exc()}')


# ---------------------------------------------------------------------------- #
#                         create_whole_quote_task 订阅任务                       #
# ---------------------------------------------------------------------------- #

def create_whole_quote_task(stock_pool,
                            stock_info_dict,
                            tick_queue,
                            shadow_tick_queue=None,
                            paper_market_queue=None,
                            shadow_market_queue=None):
    """创建全推行情订阅任务

    Args:
        stock_pool (list): 股票池，包含股票代码
        stock_info_dict (dict): 股票信息字典，包含股票代码和相关信息
        tick_queue (Queue): 用于存储tick数据的队列
        shadow_tick_queue (Queue, optional): 影子模式tick数据队列

    Features (v2.4新增):
        - 回调心跳监控：监控xtdata回调是否正常工作
        - 自动重新订阅：当回调超时时自动重新订阅
        - 详细日志记录：记录回调次数和健康状态
    """
    from xtquant import xtdata

    # 停止标识，用于控制线程退出
    stop_flag = Value('b', False)
    subscribe_id = -1

    # 获取回调心跳监控器（v2.4新增）
    heartbeat_monitor = get_callback_heartbeat_monitor(timeout=30)
    heartbeat_monitor.reset()  # 重置监控器状态

    try:
        start_time = time.time()
        partial_on_data = partial(
            on_data,
            tick_queue=tick_queue,
            shadow_tick_queue=shadow_tick_queue,
            stock_info_dict=stock_info_dict,
            stop_flag=stop_flag,
            heartbeat_monitor=heartbeat_monitor,
            paper_market_queue=paper_market_queue,
            shadow_market_queue=shadow_market_queue)
        while subscribe_id < 0:
            subscribe_id = xtdata.subscribe_whole_quote(
                stock_pool, callback=partial_on_data)
            if subscribe_id < 0:
                logger.error(f'[全推行情订阅] 订阅失败，重试中...')
                time.sleep(1)

        # ----------------------------------- 订阅成功 ----------------------------------- #
        # 记录日志
        timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        msg = f'【订阅成功】【全推行情】订阅成功, 总耗时：{round(time.time() - start_time,2)}秒，信号发出时间：{timestamp_now}'
        logger.info(msg)

    except Exception as e:
        logger.exception(f'【关键错误】全推行情订阅失败：{e}')
        send_email('【关键错误】全推行情订阅失败',
                   f'全推行情订阅时发生异常: {e}\n{traceback.format_exc()}')

    # 回调心跳检查计数器
    heartbeat_check_count = 0
    last_callback_count = 0

    # 定义交易时间段（粗略设置：上午9:30-11:30，下午13:00-15:00）
    def is_callback_monitor_time() -> bool:
        """判断是否在回调监控时间段内（交易时间）"""
        current_time = datetime.now().time()
        morning_start, morning_end = dt_time(9, 30), dt_time(11, 30)
        afternoon_start, afternoon_end = dt_time(13, 0), dt_time(15, 0)
        return (morning_start <= current_time <= morning_end) or (
            afternoon_start <= current_time <= afternoon_end)

    while True:
        time.sleep(1)
        if datetime.now().time() >= STOP_TIME:
            logger.warning(f'【进程退出】{current_process().name}')
            return

        # v2.4新增：检查回调心跳健康状态（每5秒检查一次，仅在交易时间内）
        heartbeat_check_count += 1
        if heartbeat_check_count >= 5:
            heartbeat_check_count = 0

            # 仅在交易时间内进行心跳监控
            if not is_callback_monitor_time():
                logger.debug('[回调心跳] 当前不在交易时间，跳过心跳检查')
                continue

            # 获取当前回调次数
            current_callback_count = heartbeat_monitor.get_callback_count()

            # 检查回调是否停止（回调次数没有增加）
            if current_callback_count == last_callback_count and last_callback_count > 0:
                # 检查心跳是否超时
                if heartbeat_monitor.check_and_notify():
                    logger.critical(
                        f'【回调异常】回调心跳超时，回调次数无变化: {current_callback_count}，'
                        f'距离上次回调: {heartbeat_monitor.get_last_callback_age():.1f}s，'
                        f'错误次数: {heartbeat_monitor.get_error_count()}')
                    send_email(
                        '【关键告警】全推行情回调异常', f'全推行情回调心跳超时，可能已停止工作。\n'
                        f'回调次数: {current_callback_count}\n'
                        f'距离上次回调: {heartbeat_monitor.get_last_callback_age():.1f}s\n'
                        f'错误次数: {heartbeat_monitor.get_error_count()}\n'
                        f'正在尝试重新订阅...')
                    stop_flag.value = True  # 触发重新订阅

            last_callback_count = current_callback_count

            # 每5秒输出一次心跳状态（DEBUG级别）
            logger.debug(
                f'[回调心跳] 回调次数: {current_callback_count}, '
                f'距离上次回调: {heartbeat_monitor.get_last_callback_age():.1f}s, '
                f'健康: {heartbeat_monitor.is_healthy()}')

        if stop_flag.value:
            # 取消订阅，关闭进程，释放资源，重新订阅
            if subscribe_id > 0:
                xtdata.unsubscribe_quote(subscribe_id)
                logger.warning(f'【取消订阅】【全推行情】{subscribe_id}')

            # 重新订阅
            logger.warning(f'【进程退出】{current_process().name}，连接断开或回调异常，重新订阅')
            create_whole_quote_task(
                stock_pool, stock_info_dict, tick_queue, shadow_tick_queue,
                paper_market_queue, shadow_market_queue)
            return
