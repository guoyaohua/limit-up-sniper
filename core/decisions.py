"""
core/decisions.py - 交易决策函数（买入/撤单/卖出判断）

从 打板策略_v2.4.py 提取的 should_buy(), should_cancel(), should_sell() 函数。
这些是策略的核心决策逻辑。
"""

import json
import time
import traceback
from io import StringIO
from datetime import datetime

import numpy as np
import pandas as pd
from xtquant import xtconstant
from loguru import logger

from config import (
    FIRST_LIMIT_TIME_CUTOFF,
    MAX_HOLDING_COUNT,
    MAX_SAME_SECTOR_COUNT,
    MAX_TURNOVER_RATE_THRESHOLD,
    MAX_TURNOVER_RATE_BLACKLIST,
    WATCHLIST_POSITION_RATIO,
    MIN_TURNOVER_RATE_THRESHOLD,
    MIN_VOLUME_RATIO_THRESHOLD,
    MIN_LIMIT_ORDER_AMOUNT,
    MAX_CANCEL_COUNT,
    STOP_LOSS_RATE,
    STRATEGY_NAME,
    CLEAR_TIME,
    BUY_ORDER_CANCEL_DEADLINE,
    SELL_ORDER_CANCEL_DEADLINE,
    LIMIT_ORDER_AMOUNT_THRESHOLDS,
    LLM_SECTOR_PRIORITY_DISCOUNT,
    INTRADAY_TAKE_PROFIT_ENABLED,
    INTRADAY_TAKE_PROFIT_TIERS,
)
from infra.common_enums import (
    StockOrderStatusInt,
    StockOrderStatus,
    EBrokerPriceType,
    OrderType,
)
from infra.data_helpers import _conv_time_cached, _check_same_price
from infra.utils import send_email
from infra.trade_log import record_strategy_event
from core.interpolation import interpolate_seal_threshold, interpolate_sector_requirements


def _calculate_turnover_rate(tick_data, stock_info):
    """计算换手率。"""
    float_shares = stock_info.get('流通股本', 0)
    if float_shares <= 0:
        raise ValueError(f'流通股本数据异常: {float_shares}')
    return tick_data.get('pvolume', 0) / float_shares * 100


def _add_to_watchlist(shared_data,
                      blacklist,
                      stock_code,
                      stock_name,
                      turnover_rate,
                      source='turnover',
                      snapshot=None):
    """将股票加入观察名单。"""
    watch_list = shared_data.get('观察名单')
    watchlist_metadata = shared_data.get('观察名单元数据')
    if watch_list is None or stock_code in blacklist or stock_code in watch_list:
        return

    timestamp = time.time()
    watch_list[stock_code] = f'{turnover_rate:.1f}%|{timestamp}'
    metadata = {
        'turnover_rate': round(turnover_rate, 4),
        'enter_timestamp': timestamp,
        'enter_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f'),
        'source': source,
        'stock_name': stock_name,
        'position_ratio': WATCHLIST_POSITION_RATIO,
    }
    if snapshot:
        metadata['snapshot_time'] = snapshot.get('time')
        metadata['last_price'] = snapshot.get('lastPrice')
        metadata['limit_price'] = snapshot.get('bidPrice', [None])[0] if snapshot.get('bidPrice') else None
    if watchlist_metadata is not None:
        watchlist_metadata[stock_code] = metadata

    if shared_data.get('决策原因标签') is not None:
        shared_data['决策原因标签'][stock_code] = {
            'decision': 'watchlist_enter',
            'source': source,
            'turnover_rate': round(turnover_rate, 4),
            'timestamp': metadata['enter_time'],
        }

    record_strategy_event(
        shared_data,
        event_type='watchlist_enter',
        stock_code=stock_code,
        stock_name=stock_name,
        reason=f'换手率 {turnover_rate:.1f}% 偏高，加入观察名单',
        snapshot=snapshot,
        extra={
            'source': source,
            'turnover_rate': round(turnover_rate, 4),
            'position_ratio': WATCHLIST_POSITION_RATIO,
        })
    logger.warning(
        f'[观察名单] {stock_code} {stock_name} 换手率 {turnover_rate:.1f}% 偏高，加入观察名单，仓位缩减至{WATCHLIST_POSITION_RATIO*100:.0f}%'
    )


def leading_stock_check(sector_info: str, stock_code: str) -> tuple:
    """统计股票的板块效应

    Args:
        sector_info (str): 板块信息的JSON字符串，包含板块代码、名称、领涨股票等信息
        stock_code (str): 股票代码，用于检查是否为领涨股

    Returns:
        tuple[int, int]: (板块总数, 股票领涨的板块数)
    """
    if not sector_info or not stock_code:
        return 0, 0

    try:
        # 标准化股票代码，去掉后缀
        if stock_code.endswith(('.SH', '.SZ')):
            stock_code = stock_code[:-3]

        # 尝试作为DataFrame字符串表示处理
        df = pd.read_json(StringIO(sector_info), dtype={'领涨股票代码': str})
        total_sectors = len(df)

        leading_count = (df['领涨股票代码'] == stock_code).sum()

        return total_sectors, leading_count

    except Exception as e:
        logger.exception(f"解析板块信息失败：{stock_code}, 错误：{str(e)}")
        return 0, 0


def should_buy(shared_data,
               tick_data,
               stock_code,
               is_limit_up,
               is_near_limit_up,
               stock_info=None,
               stock_status=None,
               market_sentiment_score_obj=None,
               blacklist=None,
               strong_stocks=None,
               holding_status=None,
               limit_up_pool=None,
               concept_sector_effect=None,
               industry_sector_effect=None,
               individual_capital_inflow=None,
               cancel_count=None,
               shadow_signal_mode=False,
               order={}):
    """判断是否满足买入条件

    该函数实现了多因子买入决策逻辑，包括：
    1. 市场情绪判断：基于市场整体情绪评分决定是否参与
    2. 基本面筛选：换手率、量比等技术指标
    3. 板块效应：检查个股是否受到板块联动影响
    4. 资金流向：判断是否有主力资金流入
    5. 买入时机：区分排板（涨停价买入）和扫板（即时买入）

    Args:
        shared_data (dict): 共享数据字典，包含股票状态信号、涨停池等信息
        tick_data (dict): 当前tick数据，包含价格、成交量等信息
        stock_code (str): 股票代码
        is_limit_up (bool): 是否涨停
        is_near_limit_up (bool): 是否接近涨停
        stock_info (dict, optional): 缓存的股票信息
        stock_status (dict, optional): 缓存的股票状态信号
        market_sentiment_score_obj (Value, optional): 缓存的市场情绪评分对象
        blacklist (dict, optional): 缓存的黑名单
        strong_stocks (dict, optional): 缓存的强势股票
        holding_status (dict, optional): 缓存的持仓状态
        limit_up_pool (dict, optional): 缓存的涨停池
        concept_sector_effect (dict, optional): 缓存的概念板块效应
        industry_sector_effect (dict, optional): 缓存的行业板块效应
        individual_capital_inflow (dict, optional): 缓存的个股资金流入
        cancel_count (int, optional): 撤单次数

    Returns:
        bool: 是否满足买入条件

    Raises:
        KeyError: 当访问不存在的股票信息时
    """
    try:
        log_prefix = ''
        if shadow_signal_mode:
            log_prefix = '[影子信号] '
        # 性能优化：使用缓存的数据引用，如果没有提供则回退到原始方式
        if stock_info is None:
            stock_info = shared_data['股票信息'][stock_code]
        if stock_status is None:
            stock_status = shared_data['股票状态信号'][stock_code]
        if market_sentiment_score_obj is None:
            market_sentiment_score_obj = shared_data['市场情绪_评分']
        if blacklist is None:
            blacklist = shared_data['黑名单']
        if strong_stocks is None:
            strong_stocks = shared_data['强势股票']
        if holding_status is None:
            holding_status = shared_data['持仓状态']
        if limit_up_pool is None:
            limit_up_pool = shared_data['涨停池']
        if concept_sector_effect is None:
            concept_sector_effect = shared_data['概念板块效应']
        if industry_sector_effect is None:
            industry_sector_effect = shared_data['行业板块效应']
        if individual_capital_inflow is None:
            individual_capital_inflow = shared_data['个股资金流入']
        if cancel_count is None:
            cancel_count = shared_data['撤单次数']

        # ------------------------------- 14:50之后不下单买入 ------------------------------- #
        if datetime.now().strftime('%H:%M') >= '14:50':
            return False

        # ----------------------------- 如果非涨停或触及涨停，则跳过 ---------------------------- #
        if not is_limit_up and not is_near_limit_up:
            return False

        # ------------------------------ 如果不在强势股票列表中，则跳过 ----------------------------- #
        if stock_code not in strong_stocks:
            return False

        # --------------------------------- 市场情绪判断 -------------------------------- #
        # 市场情绪评分说明：
        # >= 8: 极强，积极扫板
        # >= 7: 强势，适度扫板
        # >= 5.5: 中性偏强，谨慎扫板
        # >= 4: 中性，观望为主
        # >= 2.5: 弱势，暂停扫板
        # < 2.5: 极弱，空仓等待
        with market_sentiment_score_obj.get_lock():
            market_sentiment_score = market_sentiment_score_obj.value
        if market_sentiment_score < 2.5:
            logger.debug(
                f'{log_prefix}[不买入-市场情绪] {stock_code} 市场情绪评分 {market_sentiment_score:.1f} < 2.5，市场极弱，空仓等待'
            )
            return False

        # ------------------------------- 1. 如果在黑名单则跳过 ------------------------------- #
        if stock_code in blacklist:
            logger.debug(f'{log_prefix}[不买入-黑名单] {stock_code} 在黑名单中，跳过买入判断')
            return False

        # ------------------------------ 2. 若非未下单状态或已撤单，则跳过 ------------------------------ #
        with stock_status['下单状态'].get_lock():
            order_status_value = stock_status['下单状态'].value
        if order_status_value not in [
                StockOrderStatusInt.NOT_ORDERED, StockOrderStatusInt.CANCELLED
        ]:
            # Convert integer enum to string enum for logging
            status_mapping = {
                StockOrderStatusInt.NOT_ORDERED:
                StockOrderStatus.NOT_ORDERED,
                StockOrderStatusInt.ORDERED_BUY:
                StockOrderStatus.ORDERED_BUY,
                StockOrderStatusInt.ORDERED_SELL:
                StockOrderStatus.ORDERED_SELL,
                StockOrderStatusInt.CANCELLED:
                StockOrderStatus.CANCELLED,
                StockOrderStatusInt.POSITION_HOLDING:
                StockOrderStatus.POSITION_HOLDING,
                StockOrderStatusInt.PARTIALLY_FILLED:
                StockOrderStatus.PARTIALLY_FILLED,
            }
            logger.debug(
                f'{stock_code} 当前状态为 {status_mapping.get(order_status_value, order_status_value)}，跳过买入判断'
            )
            return False

        # ----------------------------- 3. 如果已持有该股票，则跳过 ----------------------------- #
        if stock_code in holding_status:
            logger.debug(f'{stock_code} 已持仓，跳过买入判断')
            return False

        # --------------------------- 4. 只买入首次涨停在10:30之前的股票 -------------------------- #
        if stock_code in limit_up_pool:
            first_limit_time = _conv_time_cached(int(
                limit_up_pool[stock_code].split(',')[0]),
                                                 fmt=r'%H:%M')
            if first_limit_time >= FIRST_LIMIT_TIME_CUTOFF:
                logger.debug(
                    f'{log_prefix}[不买入-首次涨停时间] {stock_code} 首次涨停时间 {first_limit_time} >= {FIRST_LIMIT_TIME_CUTOFF}，跳过买入判断'
                )
                return False
        else:
            # 如果当前时间超过10:30，则不扫首封板，只扫回封板
            current_time = datetime.now().strftime('%H:%M')
            if current_time >= FIRST_LIMIT_TIME_CUTOFF:
                logger.debug(
                    f'{log_prefix}[不买入-扫板时间] {stock_code} 当前时间 {current_time} >= {FIRST_LIMIT_TIME_CUTOFF}，跳过买入判断'
                )
                return False

        # ---------------------------------------------------------------------------- #
        #                                    验证买入条件                               #
        # ---------------------------------------------------------------------------- #

        # 买入原因
        buy_reason = ''
        stock_name = stock_info["股票名称"]
        # -------------------------- 1. 换手率（分桶处理） ------------------------- #
        try:
            turnover_rate = _calculate_turnover_rate(tick_data, stock_info)
        except ValueError as exc:
            logger.error(f'{log_prefix}[不买入-流通股本异常] {stock_code} {exc}')
            return False

        if turnover_rate < MIN_TURNOVER_RATE_THRESHOLD:
            logger.debug(
                f'{log_prefix}[不买入-换手率] {stock_code} {turnover_rate:.2f}% < {MIN_TURNOVER_RATE_THRESHOLD}% 不满足买入条件'
            )
            return False
        elif turnover_rate >= MAX_TURNOVER_RATE_BLACKLIST:
            msg = f'[黑名单] {stock_code} {stock_name} 换手率 {turnover_rate:.1f}% >= {MAX_TURNOVER_RATE_BLACKLIST}%'
            if stock_code not in blacklist:
                blacklist[stock_code] = msg
                logger.warning(msg)
                send_email(f'【黑名单】{stock_code} {stock_name}', msg)
            return False
        elif turnover_rate >= MAX_TURNOVER_RATE_THRESHOLD:
            _add_to_watchlist(shared_data,
                              blacklist,
                              stock_code,
                              stock_name,
                              turnover_rate,
                              source='turnover_rate',
                              snapshot=tick_data)
            buy_reason += f'[换手率] 换手率 {turnover_rate:.1f}% (观察名单，仓位缩减)\n'
        else:
            buy_reason += f'[换手率] 满足买入条件, 换手率 {turnover_rate:.1f}%\n'

        # -------------------------------- 2. 量比>=1.5 -------------------------------- #
        avg_volume_5d = stock_info['5日平均成交量']
        if avg_volume_5d > 0:
            volume_ratio = tick_data['volume'] / avg_volume_5d
            if volume_ratio >= MIN_VOLUME_RATIO_THRESHOLD:
                buy_reason += f'[量比] 满足买入条件, 量比 {volume_ratio:.2f} >= {MIN_VOLUME_RATIO_THRESHOLD}\n'
            else:
                logger.debug(
                    f'{log_prefix}[不买入-量比] {stock_code} {volume_ratio:.2f} < {MIN_VOLUME_RATIO_THRESHOLD} 不满足买入条件'
                )
                return False
        else:
            logger.debug(
                f'{log_prefix}[不买入-量比异常] {stock_code} 5日平均成交量为0，不满足买入条件')
            return False

        # ---------------------------------- 3. 板块效应 --------------------------------- #
        # 领涨板块数
        leading_count = 0
        total_sectors = 0
        # 检查是否满足板块效应
        if stock_code in concept_sector_effect or stock_code in industry_sector_effect:
            details = ''
            # 板块效应，满足买入条件
            if stock_code in concept_sector_effect:
                _total_sectors, _leading_count = leading_stock_check(
                    concept_sector_effect.get(stock_code, ''), stock_code)
                leading_count += _leading_count
                total_sectors += _total_sectors
                details += concept_sector_effect.get(stock_code, '')
            if stock_code in industry_sector_effect:
                _total_sectors, _leading_count = leading_stock_check(
                    industry_sector_effect.get(stock_code, ''), stock_code)
                leading_count += _leading_count
                total_sectors += _total_sectors
                details += industry_sector_effect.get(stock_code, '')
            buy_reason += f'[板块效应] 满足买入条件, 板块效应个数: {total_sectors}, 领涨个数: {leading_count}, 详情: {details}\n'
        else:
            logger.debug(f'{log_prefix}[不买入-板块效应] {stock_code} 不满足买入条件')
            return False

        # -------------------------------- 3.5 板块集中度控制 (v3.0) -------------------------------- #
        current_holdings = list(holding_status.keys())
        if current_holdings:
            concept_sector_mapping = shared_data.get('概念板块', {})
            stock_sectors = concept_sector_mapping.get(stock_code, [])
            if not stock_sectors:
                # 尝试不带后缀的代码
                stock_code_short = stock_code.split('.')[0] if '.' in stock_code else stock_code
                stock_sectors = concept_sector_mapping.get(stock_code_short, [])
            for sector in stock_sectors:
                same_sector_count = 0
                for held_code in current_holdings:
                    held_code_short = held_code.split('.')[0] if '.' in held_code else held_code
                    held_sectors = concept_sector_mapping.get(held_code, concept_sector_mapping.get(held_code_short, []))
                    if sector in held_sectors:
                        same_sector_count += 1
                if same_sector_count >= MAX_SAME_SECTOR_COUNT:
                    logger.debug(f'{log_prefix}[不买入-板块集中] {stock_code} 板块 {sector} 已持有 {same_sector_count} 只，超过上限 {MAX_SAME_SECTOR_COUNT}')
                    return False

        # ---------------------------------- 4. 资金流入 --------------------------------- #
        if stock_code in individual_capital_inflow:
            # 个股资金流入
            buy_reason += f'[资金流入] 满足买入条件, {individual_capital_inflow.get(stock_code,"")}\n'
        else:
            logger.debug(f'{log_prefix}[不买入-资金流入] {stock_code} 不满足买入条件')
            return False

        # ---------------------------------------------------------------------------- #
        #                                     买入判断                                  #
        # ---------------------------------------------------------------------------- #

        # ----------------------------------- 1. 排板 ---------------------------------- #
        with cancel_count.get_lock():
            cancel_count_val = cancel_count.value

        if is_limit_up and cancel_count_val <= MAX_CANCEL_COUNT:
            # 封单额（单位：元）- 从已更新的stock_status中读取（在process_tick_data中已先于should_buy更新）
            with stock_status['封单金额'].get_lock():
                limit_order_amount = stock_status['封单金额'].value

            # 封单额小于最小封单额时，不满足买入条件
            if limit_order_amount < MIN_LIMIT_ORDER_AMOUNT:
                logger.debug(
                    f'{log_prefix}[不买入-封单额] [排板] {stock_code} 封单额 {limit_order_amount/1e4:.0f}万 < {MIN_LIMIT_ORDER_AMOUNT/1e4:.0f}万, 不满足买入条件'
                )
                return False

            # ======================== 基于封单金额的买入判断（v3.0优化：连续插值）========================
            # v3.0: 替换离散阈值为连续插值，消除边界效应

            # 市场情绪 < 2.5 不买入（安全底线）
            if market_sentiment_score < 2.5:
                logger.debug(
                    f'{log_prefix}[不买入-市场情绪] [排板] {stock_code} 市场情绪评分 {market_sentiment_score:.1f} < 2.5，市场极弱，不满足买入条件'
                )
                return False

            threshold = interpolate_seal_threshold(market_sentiment_score)

            # U7升级：LLM优先板块降低封单门槛
            sector_priority = shared_data.get('板块优先级', {})
            if sector_priority:
                concept_sector_mapping = shared_data.get('概念板块', {})
                stock_sectors = concept_sector_mapping.get(stock_code, [])
                for s in stock_sectors:
                    if s in sector_priority:
                        weight = float(sector_priority[s])
                        discount = 1 - weight * LLM_SECTOR_PRIORITY_DISCOUNT
                        threshold *= discount
                        buy_reason += f'[LLM优先板块] {s}(权重{weight:.1f}), 封单门槛降至{threshold/1e4:.0f}万\n'
                        break  # 只取第一个匹配的优先板块

            # 弱势市场额外要求板块效应
            required_sectors = max(0, int(3 - market_sentiment_score * 0.3))
            required_leading = max(0, int(2 - market_sentiment_score * 0.2))

            if limit_order_amount >= threshold and total_sectors >= required_sectors and leading_count >= required_leading:
                buy_reason += (
                    f'[市场情绪] 满足买入条件, 情绪={market_sentiment_score:.1f}, '
                    f'封单额 {limit_order_amount/1e4:.0f}万 >= 阈值{threshold/1e4:.0f}万, '
                    f'板块效应: {total_sectors}(需>={required_sectors}), '
                    f'领涨: {leading_count}(需>={required_leading})\n'
                )
                msg = f'[排板买入] {stock_code} {stock_name} 满足买入条件, 原因: {buy_reason}'
                logger.warning(msg)
                order.clear()
                order.update({'操作原因': msg})
                return True
            else:
                logger.debug(
                    f'{log_prefix}[不买入-排板] {stock_code} 情绪={market_sentiment_score:.1f}, '
                    f'封单额 {limit_order_amount/1e4:.0f}万 vs 阈值{threshold/1e4:.0f}万, '
                    f'板块={total_sectors} vs 需{required_sectors}, '
                    f'领涨={leading_count} vs 需{required_leading}'
                )
                return False

        # ----------------------------------- 2. 扫板 ---------------------------------- #
        else:
            # 扫板买入逻辑

            # ----------------------------------- 扫板前提 ----------------------------------- #
            # 拉涨停所需资金变小 且 小于300W
            limit_up_amount_required = np.dot(tick_data['bidPrice'],
                                              tick_data['bidVol']) * 100
            with stock_status['拉板所需资金'].get_lock():
                required_capital = stock_status['拉板所需资金'].value
            if not required_capital:
                return False
            if limit_up_amount_required > required_capital or limit_up_amount_required > 3e6:
                logger.debug(
                    f'{log_prefix}[不买入-拉板资金] [扫板] {stock_code} 拉涨停所需资金 {limit_up_amount_required} > {required_capital} 或 > 300W, 不满足扫板买入条件'
                )
                return False
            # 股价下跌趋势则不扫板
            with stock_status['前一价格'].get_lock():
                previous_price = stock_status['前一价格'].value
            if not previous_price or tick_data['bidPrice'][0] < previous_price:
                logger.debug(
                    f'{log_prefix}[不买入-价格下跌] [扫板] {stock_code} 股价 {tick_data["bidPrice"][0]} < 前一价格 {previous_price}, 不满足扫板买入条件'
                )
                return False

            # 扫板买入 — 连续化阈值（U4升级：消除离散边界效应）
            if market_sentiment_score < 4:
                logger.debug(
                    f'{log_prefix}[不买入-市场情绪] [扫板] {stock_code} 市场情绪评分 {market_sentiment_score} < 4，不满足买入条件'
                )
                return False

            required_sectors, required_leading = interpolate_sector_requirements(market_sentiment_score)
            if total_sectors >= required_sectors and leading_count >= required_leading:
                buy_reason += f'[市场情绪] 满足买入条件, 市场情绪评分 {market_sentiment_score}, 板块效应个数: {total_sectors}>={required_sectors}, 领涨个数: {leading_count}>={required_leading}, 扫板买入\n'
                msg = f'[扫板买入] {stock_code} {stock_name} 满足买入条件, 原因: {buy_reason}'
                logger.warning(msg)
                order.clear()
                order.update({'操作原因': msg})
                return True
            else:
                logger.debug(
                    f'{log_prefix}[不买入-板块效应] [扫板] {stock_code} 市场情绪评分 {market_sentiment_score}, 板块效应个数 {total_sectors} < {required_sectors} 或 领涨个数 {leading_count} < {required_leading}, 不满足买入条件'
                )
                return False

    except Exception as e:
        logger.exception(f'【关键错误】买入判断异常 {e}')
        send_email('【关键错误】买入判断异常 ', f'买入判断异常: {e}\n{traceback.format_exc()}')

    # 默认不买入
    return False


def should_cancel(shared_data,
                  tick_data,
                  stock_code,
                  is_limit_up,
                  is_down_limit,
                  stock_info=None,
                  stock_status=None,
                  market_sentiment_score_obj=None,
                  blacklist=None,
                  concept_sector_effect=None,
                  industry_sector_effect=None,
                  individual_capital_inflow=None,
                  holding_status=None,
                  order_status=None,
                  shadow_signal_mode=False,
                  order={}):
    """判断是否满足撤单条件

    撤单逻辑说明：
    1. 撤买单条件：
       - 封单金额小于500万
       - 封单金额变化率小于-20%（封单大幅减少）
       - 换手率超过15%（过度投机）
       - 板块效应消失
       - 资金流入不足
       - 尾盘未成交（14:55后）

    2. 撤卖单条件：
       - 跌破止损位（成本价-5%）
       - 尾盘固定价格单未成交（14:50后改市价卖出）

    Args:
        shared_data (dict): 共享数据字典，包含股票状态信号、涨停池等信息
        tick_data (dict): 当前tick数据
        stock_code (str): 股票代码
        is_limit_up (bool): 是否涨停
        is_down_limit (bool): 是否跌停
        stock_info (dict, optional): 缓存的股票信息
        stock_status (dict, optional): 缓存的股票状态信号
        market_sentiment_score_obj (Value, optional): 缓存的市场情绪评分对象
        blacklist (dict, optional): 缓存的黑名单
        concept_sector_effect (dict, optional): 缓存的概念板块效应
        industry_sector_effect (dict, optional): 缓存的行业板块效应
        individual_capital_inflow (dict, optional): 缓存的个股资金流入
        holding_status (dict, optional): 缓存的持仓状态
        order_status (dict, optional): 缓存的委托状态
        shadow_signal_mode (bool, optional): 是否为影子信号模式，默认False

    Returns:
        bool: 是否满足撤单条件
    """
    try:
        log_prefix = ''
        if shadow_signal_mode:
            log_prefix = '[影子信号] '
        # 性能优化：使用缓存的数据引用，如果没有提供则回退到原始方式
        if stock_info is None:
            stock_info = shared_data['股票信息'][stock_code]
        if stock_status is None:
            stock_status = shared_data['股票状态信号'][stock_code]
        if market_sentiment_score_obj is None:
            market_sentiment_score_obj = shared_data['市场情绪_评分']
        if blacklist is None:
            blacklist = shared_data['黑名单']
        if concept_sector_effect is None:
            concept_sector_effect = shared_data['概念板块效应']
        if industry_sector_effect is None:
            industry_sector_effect = shared_data['行业板块效应']
        if individual_capital_inflow is None:
            individual_capital_inflow = shared_data['个股资金流入']
        if holding_status is None:
            holding_status = shared_data['持仓状态']
        if order_status is None:
            order_status = shared_data['委托状态']

        # ------------------------------ 1. 如果没有可撤委托则跳过 ------------------------------ #
        xt_orders = order_status.get(stock_code, '')
        if not xt_orders:
            return False

        # --------------------------------- 2. 撤买撤卖判断 -------------------------------- #
        xt_orders = json.loads(xt_orders)
        cancel_buy = xt_orders[0][
            '委托类型'] == xtconstant.STOCK_BUY  # 不会存在一天内同时买入卖出的情况
        cancel_sell = xt_orders[0][
            '委托类型'] == xtconstant.STOCK_SELL  # 不会存在一天内同时买入卖出的情况

        # 预先计算常用值
        stock_name = stock_info["股票名称"]
        with market_sentiment_score_obj.get_lock():
            market_sentiment_score = market_sentiment_score_obj.value

        # ---------------------------------------------------------------------------- #
        #                                     撤买判断                                  #
        # ---------------------------------------------------------------------------- #
        if cancel_buy:
            # -------------- 1. 判断是否为涨停状态，如果否则证明打板均已成交，跳过撤单判断 (理论上不会进入这个逻辑) -------------- #
            if not is_limit_up:
                logger.error(f'{stock_code} {stock_name} 非涨停状态，无法撤单，跳过撤单判断')
                return False

            # 缓存常用值
            with stock_status['封单金额'].get_lock():
                limit_order_amount = stock_status['封单金额'].value
            with stock_status['封单金额变化率'].get_lock():
                change_rate = stock_status['封单金额变化率'].value
            try:
                turnover_rate = _calculate_turnover_rate(tick_data, stock_info)
            except ValueError as exc:
                logger.error(f'{log_prefix}[撤单判断-流通股本异常] {stock_code} {exc}')
                return False

            # -------------------------------- 2. 封单绝对值判断 -------------------------------- #
            if limit_order_amount < MIN_LIMIT_ORDER_AMOUNT:
                # 封单金额小于2000万，撤单
                msg = f'{stock_code} {stock_name} 封单金额 {limit_order_amount} < {MIN_LIMIT_ORDER_AMOUNT}，撤单'
                logger.warning(msg)

                order.clear()
                order.update({'操作原因': msg})

                # send_email(f'【撤买】{stock_code} {stock_name}', msg)
                return True

            # -------------------------------- 3. 封单金额变化率判断（v2.4.1优化）-------------------------------- #
            # 触发条件：封单变化率 < -20%（封单大幅减少时才检查是否需要撤单）
            if change_rate < -0.2:
                cancel_order = False
                cancel_reason = ""

                # 使用封单金额作为撤单判断依据（与买入逻辑对称）
                # 【极强】市场情绪评分 >= 8：仅当封单额 < 3000万时撤单
                if market_sentiment_score >= 8:
                    threshold = LIMIT_ORDER_AMOUNT_THRESHOLDS['STRONG_8']
                    if limit_order_amount < threshold:
                        cancel_order = True
                        cancel_reason = f'市场极强(>={8})但封单额{limit_order_amount/1e4:.0f}万 < {threshold/1e4:.0f}万'

                # 【强势】市场情绪评分 >= 7：封单额 < 5000万时撤单
                elif market_sentiment_score >= 7:
                    threshold = LIMIT_ORDER_AMOUNT_THRESHOLDS['STRONG_7']
                    if limit_order_amount < threshold:
                        cancel_order = True
                        cancel_reason = f'市场强势(>={7})但封单额{limit_order_amount/1e4:.0f}万 < {threshold/1e4:.0f}万'

                # 【中性偏强】市场情绪评分 >= 5.5：封单额 < 8000万时撤单
                elif market_sentiment_score >= 5.5:
                    threshold = LIMIT_ORDER_AMOUNT_THRESHOLDS['NEUTRAL_55']
                    if limit_order_amount < threshold:
                        cancel_order = True
                        cancel_reason = f'市场中性偏强(>={5.5})但封单额{limit_order_amount/1e4:.0f}万 < {threshold/1e4:.0f}万'

                # 【中性】市场情绪评分 >= 4：封单额 < 1亿时撤单
                elif market_sentiment_score >= 4:
                    threshold = LIMIT_ORDER_AMOUNT_THRESHOLDS['NEUTRAL_4']
                    if limit_order_amount < threshold:
                        cancel_order = True
                        cancel_reason = f'市场中性(>={4})但封单额{limit_order_amount/1e4:.0f}万 < {threshold/1e4:.0f}万'

                # 【弱势】市场情绪评分 >= 2.5：封单额 < 1.5亿时撤单
                elif market_sentiment_score >= 2.5:
                    threshold = LIMIT_ORDER_AMOUNT_THRESHOLDS['WEAK_25']
                    if limit_order_amount < threshold:
                        cancel_order = True
                        cancel_reason = f'市场弱势(>={2.5})但封单额{limit_order_amount/1e4:.0f}万 < {threshold/1e4:.0f}万'

                # 【极弱】市场情绪评分 < 2.5：直接撤单
                else:
                    cancel_order = True
                    cancel_reason = f'市场极弱(<{2.5})，直接撤单'

                if cancel_order:
                    msg = f'{stock_code} {stock_name} 触发撤单条件：{cancel_reason}\n'
                    msg += f'封单金额变化率: {change_rate:.2%}, 当前封单额: {limit_order_amount/1e4:.0f}万, 市场情绪: {market_sentiment_score:.1f}'
                    logger.warning(msg)

                    order.clear()
                    order.update({'操作原因': msg})

                    return True
            else:
                logger.debug(
                    f'{stock_code} {stock_name} 封单金额变化率 {change_rate:.2%}，封单额 {limit_order_amount/1e4:.0f}万，未触发撤单条件'
                )

            # --------------------------------- 4. 换手率判断 --------------------------------- #
            # U5升级：分级处理 — ≥25%拉黑并撤单，15-25%加入观察名单但不撤单
            if turnover_rate >= MAX_TURNOVER_RATE_BLACKLIST:
                msg = f'{stock_code} {stock_name} 换手率 {turnover_rate:.2f}% >= {MAX_TURNOVER_RATE_BLACKLIST:.2f}%，撤单'
                logger.warning(msg)
                order.clear()
                order.update({'操作原因': msg})
                if stock_code not in blacklist:
                    msg = f'[黑名单] {stock_code} {stock_name} 换手率 {turnover_rate:.1f}% >= {MAX_TURNOVER_RATE_BLACKLIST}%'
                    blacklist[stock_code] = msg
                    logger.warning(msg)
                    send_email(f'【黑名单】{stock_code} {stock_name}', msg)
                return True
            elif turnover_rate >= MAX_TURNOVER_RATE_THRESHOLD:
                _add_to_watchlist(shared_data, blacklist, stock_code, stock_name,
                                  turnover_rate)

            # # --------------------------------- 5. 板块效应判断 -------------------------------- #
            # if (stock_code not in concept_sector_effect
            #         and stock_code not in industry_sector_effect):
            #     # 板块效应不存在，撤单
            #     msg = f'{stock_code} {stock_name} 板块效应不存在，撤单'
            #     logger.warning(msg)
            #     order.clear()
            #     order.update({'操作原因': msg})
            #     # send_email(f'【撤买】{stock_code} {stock_name}', msg)
            #     return True

            # --------------------------------- 6. 资金流入判断 -------------------------------- #
            if stock_code not in individual_capital_inflow:
                # 个股资金流入不存在，撤单
                msg = f'{stock_code} {stock_name} 个股资金流入不存在，撤单'
                logger.warning(msg)
                order.clear()
                order.update({'操作原因': msg})
                # send_email(f'【撤买】{stock_code} {stock_name}', msg)
                return True

            # -------------------------------- 7. 尾盘未成交则撤单 ------------------------------- #
            if datetime.now().time() >= BUY_ORDER_CANCEL_DEADLINE:
                # 尾盘时间段，未成交则撤单
                msg = f'{stock_code} {stock_name} 尾盘未成交，撤单'
                logger.warning(msg)
                order.clear()
                order.update({'操作原因': msg})
                # send_email(f'【撤买】{stock_code} {stock_name}', msg)
                return True

            logger.debug(f'{log_prefix}{stock_code} {stock_name} 不满足撤单条件，不撤单')

            return False

        # ---------------------------------------------------------------------------- #
        #                                     撤卖判断                                  #
        # ---------------------------------------------------------------------------- #
        elif cancel_sell:
            # ----------------------------- 当没有固定价格委托的订单时，不撤单 ---------------------------- #
            if not xt_orders or all(xt_order['报价类型'] != EBrokerPriceType.
                                    BROKER_PRICE_LIMIT.value  # 限价单
                                    for xt_order in xt_orders):
                logger.debug(
                    f'{log_prefix}{stock_code} {stock_name} 没有固定价格委托的订单，跳过撤单判断'
                )
                return False

            # ------------------------------ 当股价跌停且挂单价也跌停时，不撤单 ----------------------------- #
            down_limit_price = stock_info["跌停价"]
            all_down_limit = True
            if is_down_limit:
                for xt_order in xt_orders:
                    # 跳过市价单
                    if xt_order[
                            '报价类型'] != EBrokerPriceType.BROKER_PRICE_LIMIT.value:  # 限价单
                        continue
                    # 如果有卖单的委托价格不等于跌停价，则继续撤单检查
                    if not _check_same_price(xt_order['委托价格'],
                                             down_limit_price):
                        all_down_limit = False
                        break
                if all_down_limit:
                    logger.debug(
                        f'{log_prefix}{stock_code} {stock_name} 股价跌停且挂单价也跌停，跳过撤单判断'
                    )
                    return False

            # ------------------------------- 1. 判断是否跌破止损位(使用成本价计算) ------------------------------- #
            xt_position = holding_status.get(stock_code, '')

            if not xt_position:
                logger.error(f'{stock_code} {stock_name} 当前没有持仓，无法判断撤单条件')
                return False

            xt_position = json.loads(xt_position)
            if tick_data['lastPrice'] < xt_position['成本价'] * (1 -
                                                              STOP_LOSS_RATE):
                # 跌破止损位，撤单
                msg = f'{stock_code} {stock_name} 跌破止损位，撤单'
                logger.warning(msg)
                order.clear()
                order.update({'操作原因': msg})
                # send_email(f'【撤卖】{stock_code} {stock_name}', msg)
                return True

            # -------------------- 2. 14:50之后如果所挂卖出单还未成交，则撤单（排除挂跌停的订单） ------------------- #
            if datetime.now().time() >= SELL_ORDER_CANCEL_DEADLINE:
                contain_fix_price_order = False
                # 判断是否是固定价格单
                for xt_order in xt_orders:
                    if (xt_order['报价类型']
                            == EBrokerPriceType.BROKER_PRICE_LIMIT.value  # 限价单
                            and not _check_same_price(xt_order['委托价格'],
                                                      down_limit_price)):
                        contain_fix_price_order = True
                        break

                # 对于固定价格报单，且非跌停价卖出的，撤单，后续以市价单卖出
                if contain_fix_price_order:
                    # 尾盘时间段，固定报价未成交则撤单
                    msg = f'{stock_code} {stock_name} 尾盘卖出未成交，撤单'
                    logger.warning(msg)
                    order.clear()
                    order.update({'操作原因': msg})
                    # send_email(f'【撤卖】{stock_code} {stock_name}', msg)
                    return True

        else:
            logger.error(
                f'{stock_code} 委托状态异常, 无法判断撤单条件. {order_status.get(stock_code,"未知状态")}'
            )

    except Exception as e:
        logger.exception(f'【关键错误】撤单判断异常 {e}')
        send_email('【关键错误】撤单判断异常 ', f'撤单判断异常: {e}\n{traceback.format_exc()}')

    # 默认不撤单
    return False


def should_sell(shared_data,
                stock_code,
                tick_data,
                is_down_limit,
                is_near_limit_up,
                is_limit_up,
                down_limit_price,
                stock_status=None,
                stock_info=None,
                holding_status=None,
                pre_market_holdings=None,
                order={}):
    """判断是否满足卖出条件

    Args:
        shared_data (dict): 共享数据字典，包含股票状态信号、涨停池等信息
        stock_code (str): 股票代码
        tick_data (dict): 当前tick数据
        stock_info (dict, optional): 缓存的股票信息
        holding_status (dict, optional): 缓存的持仓状态
        pre_market_holdings (list, optional): 缓存的盘前持仓

    Returns:
        bool: 是否满足卖出条件
    """
    try:
        # 性能优化：使用缓存的数据引用，如果没有提供则回退到原始方式
        if stock_info is None:
            stock_info = shared_data['股票信息'][stock_code]
        if stock_status is None:
            stock_status = shared_data['股票状态信号'][stock_code]
        if holding_status is None:
            holding_status = shared_data['持仓状态']
        if pre_market_holdings is None:
            pre_market_holdings = shared_data['盘前持仓']

        # 如果当前没有持仓，则不卖出
        if stock_code not in pre_market_holdings:
            return False

        # ---------------------------- 如果已卖出成交或已挂单卖出则跳过判断 ---------------------------- #
        xt_position = holding_status.get(stock_code, '')
        if not xt_position:
            return False

        xt_position = json.loads(xt_position)
        can_use_volume = xt_position['可用数量']
        if can_use_volume <= 0:
            logger.debug(
                f'{stock_code} 当前\"可用持仓数量\"为0，跳过卖出判断。持仓状态: {xt_position}')
            return False

        # 预先计算常用值
        price_type = (xtconstant.MARKET_SH_CONVERT_5_CANCEL
                      if stock_code.endswith('.SH') else
                      xtconstant.MARKET_SZ_CONVERT_5_CANCEL)
        stock_name = stock_info["股票名称"]
        cost_price = xt_position['成本价']
        # v3.0: 移除静态成本止损，统一由追踪止损数组处理
        last_price = tick_data['bidPrice'][0] if tick_data[
            'bidPrice'] else tick_data['lastPrice']
        with stock_status['前一价格'].get_lock():
            previous_price = stock_status['前一价格'].value

        # ------------------------------ 当14:50之后，以市价单卖出 ----------------------------- #
        if datetime.now().time() >= CLEAR_TIME:
            # 尾盘时间段，市价单卖出

            # 卖出数量, 合规检验，卖出数量应该小于盘口的1/10
            sell_volume = int(
                min(can_use_volume / 100, sum(tick_data['bidVol']))) * 100

            if sell_volume <= 0:
                logger.info(
                    f'{stock_code} 当前\"可卖出数量\"为0，跳过卖出。卖出数量: {sell_volume}, 可用数量: {can_use_volume}, 盘口买入量: {tick_data["bidVol"]}'
                )
                return False

            msg = f'{stock_code} {stock_name} 尾盘未成交，市价单卖出。'

            order.clear()
            order.update({
                '委托类型': OrderType.SELL,
                '股票代码': stock_code,
                '报价类型': price_type,  # 市价
                '策略名称': STRATEGY_NAME,
                '委托备注': '卖出',
                '操作原因': msg,
                '剩余仓位': can_use_volume - sell_volume,  # 0 清仓
                '快照': tick_data  # 附带当前Tick快照
            })

            logger.warning(f'{msg}\t{order}')
            # send_email(f'【卖出】{stock_code} {stock_name}', msg)
            return True

        # ---------------------------- 跌停时，则以跌停价清仓卖出 ---------------------------- #
        if is_down_limit:
            # 板上固定价格卖出
            msg = f'{stock_code} {stock_name} 跌停，挂跌停价清仓卖出。'

            order.clear()
            order.update({
                '委托类型': OrderType.SELL,
                '股票代码': stock_code,
                '委托价格': down_limit_price,
                '报价类型': xtconstant.FIX_PRICE,
                '策略名称': STRATEGY_NAME,
                '委托备注': '卖出',
                '操作原因': msg,
                '剩余仓位': 0,
                '快照': tick_data  # 附带当前Tick快照
            })

            logger.warning(f'{msg}\t{order}')
            # send_email(f'【卖出】{stock_code} {stock_name}', msg)
            return True

        # ------------------------------ 涨停或临近涨停，市价清仓卖出 ------------------------------ #
        if is_near_limit_up or is_limit_up:
            # 涨停或临近涨停，市价单卖出

            # 卖出数量, 合规检验，卖出数量应该小于盘口的1/10
            sell_volume = int(
                min(can_use_volume / 100, sum(tick_data['bidVol']))) * 100

            if sell_volume <= 0:
                logger.info(
                    f'{stock_code} 当前\"可卖出数量\"为0，跳过卖出。卖出数量: {sell_volume}, 可用数量: {can_use_volume}, 盘口买入量: {tick_data["bidVol"]}'
                )
                return False

            msg = f'{stock_code} {stock_name} 涨停或临近涨停，市价单清仓卖出。'

            order.clear()
            order.update({
                '委托类型': OrderType.SELL,
                '股票代码': stock_code,
                '报价类型': price_type,  # 市价
                '策略名称': STRATEGY_NAME,
                '委托备注': '卖出',
                '操作原因': msg,
                '剩余仓位': can_use_volume - sell_volume,  # 0 清仓
                '快照': tick_data  # 附带当前Tick快照
            })

            logger.warning(f'{msg}\t{order}')
            # send_email(f'【卖出】{stock_code} {stock_name}', msg)
            return True

        # -------------------------------- 股价上涨阶段，不卖出 -------------------------------- #
        if not previous_price or last_price > previous_price:
            logger.debug(
                f'[卖出判断] {stock_code} 股价 {last_price} > 前一价格 {previous_price}, 股价上涨，不卖出'
            )
            return False

        # -------------------------------- 日内分档止盈 (v3.0) -------------------------------- #
        if INTRADAY_TAKE_PROFIT_ENABLED and not is_limit_up:
            # 涨停板上不止盈，避免挂卖单干扰封板
            profit_ratio = (last_price - cost_price) / cost_price if cost_price > 0 else 0
            hold_volume = xt_position.get('持仓数量', can_use_volume)
            for tier_profit, tier_sell_ratio, tier_desc in reversed(INTRADAY_TAKE_PROFIT_TIERS):
                if profit_ratio >= tier_profit:
                    tier_key = f'止盈_{int(tier_profit*100)}pct'
                    # 检查该档位是否已触发过（用 stock_status 中的标记）
                    tier_triggered = stock_status.get(tier_key, None)
                    already_triggered = False
                    if tier_triggered is not None:
                        if hasattr(tier_triggered, 'get_lock'):
                            with tier_triggered.get_lock():
                                already_triggered = tier_triggered.value != 0
                        else:
                            already_triggered = bool(tier_triggered)
                    if not already_triggered:
                        target_sell_volume = hold_volume * tier_sell_ratio
                        sell_volume = int(target_sell_volume / 100) * 100
                        if sell_volume <= 0 and target_sell_volume > 0 and can_use_volume >= 100:
                            sell_volume = 100
                        sell_volume = min(sell_volume, can_use_volume)

                        # 合规检验
                        sell_volume = int(min(sell_volume / 100, sum(tick_data['bidVol']))) * 100
                        if sell_volume > 0 and sell_volume <= can_use_volume:
                            # 标记已触发
                            if tier_triggered is not None and hasattr(tier_triggered, 'get_lock'):
                                with tier_triggered.get_lock():
                                    tier_triggered.value = 1
                            msg = f'{stock_code} {stock_name} 触发日内止盈: {tier_desc}, 盈利{profit_ratio:.1%}, 卖出{sell_volume}股'
                            order.clear()
                            order.update({
                                '委托类型': OrderType.SELL,
                                '股票代码': stock_code,
                                '报价类型': price_type,
                                '策略名称': STRATEGY_NAME,
                                '委托备注': f'止盈-{tier_desc}',
                                '操作原因': msg,
                                '剩余仓位': can_use_volume - sell_volume,
                                '快照': tick_data,
                                '止盈档位': tier_desc,
                                '止盈收益率': round(profit_ratio, 4),
                            })
                            logger.warning(msg)
                            return True
                    break  # 只检查最高匹配档位

        # v3.0: 静态成本止损已移除，统一由下方追踪止盈止损数组处理

        # -------------------------------- 股票下跌，跟踪止盈止损 ------------------------------- #
        price_array = stock_status['止盈止损价格列表']
        volume_array = stock_status['目标剩余仓位']
        for i in range(9, -1, -1):
            if last_price < price_array[i]:
                # 如果当前价格小于止盈止损价格，则卖出

                # 卖出数量
                sell_volume = can_use_volume - volume_array[i]
                # 合规检验，卖出数量应该小于盘口的1/10
                sell_volume = int(
                    min(sell_volume / 100, sum(tick_data['bidVol']))) * 100

                if sell_volume <= 0:
                    logger.info(
                        f'{stock_code} 当前\"可卖出数量\"为0，跳过卖出。卖出数量: {sell_volume}, 可用数量: {can_use_volume}, 盘口买入量: {tick_data["bidVol"]}, 目标剩余仓位: {volume_array[i]}'
                    )
                    return False

                msg = f'{stock_code} {stock_name} 跌破止盈止损{i+1}价格 {price_array[i]}, 卖出。'

                order.clear()
                order.update({
                    '委托类型': OrderType.SELL,
                    '股票代码': stock_code,
                    '报价类型': price_type,  # 市价
                    '策略名称': STRATEGY_NAME,
                    '委托备注': f'卖出止盈止损{i+1}',
                    '操作原因': msg,
                    '剩余仓位': can_use_volume - sell_volume,
                    '快照': tick_data  # 附带当前Tick快照
                })

                logger.warning(f'{msg}\t{order}')
                # send_email(f'【卖出】{stock_code} {stock_name}', msg)
                return True
    except Exception as e:
        logger.exception(f'【关键错误】卖出判断异常 {e}')
        send_email('【关键错误】卖出判断异常 ', f'卖出判断异常: {e}\n{traceback.format_exc()}')

    # 默认不满足卖出条件
    return False
