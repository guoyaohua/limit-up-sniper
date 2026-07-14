"""Key market indicators and the live market-regime score."""

import math
import time
import traceback
from datetime import datetime

import numpy as np
from loguru import logger

from config import MAX_MARKET_SENTIMENT_AGE_SECONDS
from infra.data_helpers import is_trading_time
from infra.utils import send_email


def _shared_value(shared_data, key, default=0.0):
    value = shared_data.get(key)
    if value is None:
        return default
    if hasattr(value, 'get_lock'):
        with value.get_lock():
            return value.value
    return getattr(value, 'value', value)


def _timestamp_is_fresh(shared_data, key, now=None):
    now = time.time() if now is None else now
    try:
        timestamp = float(_shared_value(shared_data, key, 0.0))
    except (TypeError, ValueError):
        return False
    age = now - timestamp
    return 0 <= age <= MAX_MARKET_SENTIMENT_AGE_SECONDS


def market_sentiment_inputs_are_fresh(shared_data, now=None):
    """Require both current-market and prior-board snapshots to be live."""
    return all(
        _timestamp_is_fresh(shared_data, key, now=now)
        for key in ('大盘指数更新时间', '昨日涨停表现更新时间')
    )


def calculate_market_sentiment_score(
        *, limit_up_count, break_count, break_rate,
        yesterday_first_rate, yesterday_limit_rate,
        yesterday_first_perf, yesterday_limit_perf, index_values):
    """Return the documented 1-10 regime score from normalized inputs.

    Rates use fractions. ``yesterday_*_perf`` and index values use percentage
    points, matching the monitor that calculates ``change * 100``.
    """
    inputs = [
        limit_up_count, break_count, break_rate, yesterday_first_rate,
        yesterday_limit_rate, yesterday_first_perf, yesterday_limit_perf,
        *index_values,
    ]
    if not all(math.isfinite(float(value)) for value in inputs):
        raise ValueError('市场情绪输入包含非有限值')
    if len(index_values) == 0:
        raise ValueError('缺少市场指数输入')

    score = 5.0
    if limit_up_count >= 80:
        score += 1.5
    elif limit_up_count >= 60:
        score += 1.2
    elif limit_up_count >= 40:
        score += 0.8
    elif limit_up_count >= 20:
        score += 0.4
    elif limit_up_count < 10:
        score -= 0.8

    # 0/0 means no board-quality evidence yet, not a perfect 0% break rate.
    if int(limit_up_count) + int(break_count) > 0:
        if break_rate <= 0.2:
            score += 1.5
        elif break_rate <= 0.3:
            score += 0.8
        elif break_rate <= 0.5:
            score += 0.0
        elif break_rate <= 0.7:
            score -= 0.8
        else:
            score -= 1.5

    avg_yesterday_rate = (yesterday_first_rate + yesterday_limit_rate) / 2
    if avg_yesterday_rate >= 0.4:
        score += 1.2
    elif avg_yesterday_rate >= 0.3:
        score += 0.8
    elif avg_yesterday_rate >= 0.2:
        score += 0.4
    elif avg_yesterday_rate < 0.1:
        score -= 0.6

    # Percentage-point thresholds: 3.0 means +3%, not 0.03%.
    avg_yesterday_perf = (yesterday_first_perf + yesterday_limit_perf) / 2
    if avg_yesterday_perf >= 3.0:
        score += 1.0
    elif avg_yesterday_perf >= 1.0:
        score += 0.5
    elif avg_yesterday_perf <= -3.0:
        score -= 1.0
    elif avg_yesterday_perf <= -1.0:
        score -= 0.5

    avg_index = sum(index_values) / len(index_values)
    if avg_index >= 2.0:
        score += 1.0
    elif avg_index >= 1.0:
        score += 0.7
    elif avg_index >= 0.5:
        score += 0.4
    elif avg_index >= 0:
        score += 0.2
    elif avg_index >= -0.5:
        score -= 0.2
    elif avg_index >= -1.0:
        score -= 0.5
    elif avg_index >= -2.0:
        score -= 0.7
    else:
        score -= 1.0

    index_std = float(np.std(index_values))
    if index_std <= 0.5:
        score += 0.3
    elif index_std >= 1.5:
        score -= 0.3
    return max(1.0, min(10.0, score))


def log_key_market_indicators(shared_data):
    """Calculate, publish and log the current 1-10 market score."""
    try:
        if not is_trading_time():
            logger.debug('当前不在交易时间，跳过关键市场指标记录')
            return
        if not market_sentiment_inputs_are_fresh(shared_data):
            logger.warning('市场情绪底层行情过期，保留旧评分并停止刷新时间戳')
            return

        current_time = datetime.now().strftime('%H:%M:%S')
        limit_up_count = _shared_value(shared_data, '市场情绪_涨停板数量')
        break_count = _shared_value(shared_data, '市场情绪_炸板数量')
        break_rate = _shared_value(shared_data, '市场情绪_炸板率')
        yesterday_first_count = _shared_value(
            shared_data, '市场情绪_昨日首板连板个数')
        yesterday_limit_count = _shared_value(
            shared_data, '市场情绪_昨日涨停连板个数')
        yesterday_first_perf = _shared_value(
            shared_data, '市场情绪_昨日首板表现')
        yesterday_limit_perf = _shared_value(
            shared_data, '市场情绪_昨日涨停表现')
        yesterday_first_rate = _shared_value(
            shared_data, '市场情绪_昨日首板连板率')
        yesterday_limit_rate = _shared_value(
            shared_data, '市场情绪_昨日涨停连板率')
        index_values = [
            _shared_value(shared_data, '上证指数涨跌幅'),
            _shared_value(shared_data, '沪深300涨跌幅'),
            _shared_value(shared_data, '创业板指涨跌幅'),
            _shared_value(shared_data, '深证成指涨跌幅'),
        ]

        score = calculate_market_sentiment_score(
            limit_up_count=limit_up_count,
            break_count=break_count,
            break_rate=break_rate,
            yesterday_first_rate=yesterday_first_rate,
            yesterday_limit_rate=yesterday_limit_rate,
            yesterday_first_perf=yesterday_first_perf,
            yesterday_limit_perf=yesterday_limit_perf,
            index_values=index_values,
        )
        avg_yesterday_rate = (yesterday_first_rate + yesterday_limit_rate) / 2
        avg_yesterday_perf = (yesterday_first_perf + yesterday_limit_perf) / 2
        avg_index = sum(index_values) / len(index_values)
        index_std = float(np.std(index_values))

        last_score = _shared_value(shared_data, '市场情绪_评分')
        score_obj = shared_data['市场情绪_评分']
        with score_obj.get_lock():
            score_obj.value = score
        timestamp_obj = shared_data.get('市场情绪_更新时间')
        if timestamp_obj is not None:
            with timestamp_obj.get_lock():
                timestamp_obj.value = time.time()

        if score >= 8:
            sentiment, buy_advice = '极强', '积极扫板'
        elif score >= 7:
            sentiment, buy_advice = '强势', '适度扫板'
        elif score >= 5.5:
            sentiment, buy_advice = '中性偏强', '谨慎扫板'
        elif score >= 4:
            sentiment, buy_advice = '中性', '观望为主'
        elif score >= 2.5:
            sentiment, buy_advice = '弱势', '暂停扫板'
        else:
            sentiment, buy_advice = '极弱', '空仓等待'

        if (yesterday_first_perf > 2 and yesterday_limit_perf > 1
                and avg_index > 0.5):
            trend = '↑向好'
        elif (yesterday_first_perf < -2 and yesterday_limit_perf < -1
              and avg_index < -0.5):
            trend = '↓转弱'
        else:
            trend = '→平稳'

        score_details = (
            f'涨停数:{limit_up_count}只, 炸板率:{break_rate:.1%}, '
            f'昨日连板率:{avg_yesterday_rate:.1%}, '
            f'昨日表现:{avg_yesterday_perf:+.1f}%, 大盘均值:{avg_index:+.2f}%')
        index_details = (
            f'上证:{index_values[0]:+.2f}%, 沪深300:{index_values[1]:+.2f}%, '
            f'创业板:{index_values[2]:+.2f}%, 深成指:{index_values[3]:+.2f}%')
        key_msg = (
            f'【{current_time}】关键指标 - 市场情绪:{sentiment}{trend}, '
            f'评分:{score:.1f}/10 ({buy_advice}), 涨停数:{limit_up_count}, '
            f'炸板率:{break_rate:.1%}, 昨日首板连板:{yesterday_first_count}只, '
            f'昨日涨停连板:{yesterday_limit_count}只, '
            f'昨日首板表现:{yesterday_first_perf:+.2f}%, '
            f'昨日涨停表现:{yesterday_limit_perf:+.2f}%, 指数表现: {index_details}')
        logger.warning(key_msg)

        if abs(score - last_score) >= 1.0:
            logger.info(f'【市场情绪评分变化】{last_score:.1f} → {score:.1f}, {score_details}')
            send_email(
                '【市场情绪评分变化】',
                f'【市场情绪评分变化】{last_score:.1f} → {score:.1f}, '
                f'{score_details}\n{key_msg}',
            )
        if avg_index <= -2.0:
            logger.info(f'【大盘异常下跌警告】{index_details}，建议谨慎操作或空仓观望')
        elif avg_index >= 2.0:
            logger.info(f'【大盘强势上涨】{index_details}，市场情绪高涨，可适当加大仓位')
        logger.debug(f'指数分化标准差: {index_std:.3f}')
    except Exception as exc:
        logger.exception(f'【关键错误】记录关键市场指标发生错误: {exc}')
        send_email(
            '【关键错误】记录关键市场指标发生错误',
            f'记录关键市场指标发生错误: {exc}\n{traceback.format_exc()}',
        )
