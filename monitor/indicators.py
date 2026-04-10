"""
monitor/indicators.py - 关键市场指标记录与市场情绪评分

从 打板策略_v2.4.py 提取的关键市场指标记录函数。
"""

import traceback
from datetime import datetime
from multiprocessing import Value

import numpy as np
from loguru import logger

from infra.data_helpers import is_trading_time
from infra.utils import send_email


def log_key_market_indicators(shared_data):
    """记录关键市场指标并计算市场情绪评分

    v2.4.1 优化：
        1. 降低大盘指数因子权重（从±2.0降到±1.0）
        2. 移除重复的"连板数量"因子（与连板率高度相关）
        3. 提高昨日连板率因子权重（从±1.0提升到±1.2）
        4. 降低指数分化度权重（从±0.5降到±0.3）

    优化后因子权重分布：
        - 涨停数量：±1.5（涨停生态核心）
        - 炸板率：±1.5（涨停生态核心）
        - 昨日连板率：±1.2（提高权重）
        - 昨日表现：±1.0（保持）
        - 大盘指数：±1.0（降低权重）
        - 指数分化度：±0.3（降低权重）

    理论评分范围：5 ± (1.5+1.5+1.2+1.0+1.0+0.3) = 5 ± 6.5 = [-1.5, 11.5] -> 限制在[1, 10]
    """
    try:
        if not is_trading_time():
            logger.debug("当前不在交易时间，跳过关键市场指标记录")
            return
        current_time = datetime.now().strftime('%H:%M:%S')
        with shared_data['市场情绪_涨停板数量'].get_lock():
            limit_up_count = shared_data['市场情绪_涨停板数量'].value
        with shared_data['市场情绪_炸板率'].get_lock():
            break_rate = shared_data['市场情绪_炸板率'].value
        with shared_data['市场情绪_昨日首板连板个数'].get_lock():
            yesterday_first_count = shared_data['市场情绪_昨日首板连板个数'].value
        with shared_data['市场情绪_昨日涨停连板个数'].get_lock():
            yesterday_limit_count = shared_data['市场情绪_昨日涨停连板个数'].value
        with shared_data['市场情绪_昨日首板表现'].get_lock():
            yesterday_first_perf = shared_data['市场情绪_昨日首板表现'].value
        with shared_data['市场情绪_昨日涨停表现'].get_lock():
            yesterday_limit_perf = shared_data['市场情绪_昨日涨停表现'].value
        with shared_data['市场情绪_昨日首板连板率'].get_lock():
            yesterday_first_rate = shared_data['市场情绪_昨日首板连板率'].value
        with shared_data['市场情绪_昨日涨停连板率'].get_lock():
            yesterday_limit_rate = shared_data['市场情绪_昨日涨停连板率'].value

        # 获取大盘指数涨跌幅
        with shared_data.get('上证指数涨跌幅', Value('d', 0.0)).get_lock():
            sh_index = shared_data.get('上证指数涨跌幅', Value('d', 0.0)).value
        with shared_data.get('沪深300涨跌幅', Value('d', 0.0)).get_lock():
            hs300_index = shared_data.get('沪深300涨跌幅', Value('d', 0.0)).value
        with shared_data.get('创业板指涨跌幅', Value('d', 0.0)).get_lock():
            cyb_index = shared_data.get('创业板指涨跌幅', Value('d', 0.0)).value
        with shared_data.get('深证成指涨跌幅', Value('d', 0.0)).get_lock():
            sz_index = shared_data.get('深证成指涨跌幅', Value('d', 0.0)).value

        # ==================== 计算市场情绪评分 (1-10分) ====================
        # v2.4.1 优化：调整因子权重，更聚焦于涨停生态
        score = 5.0  # 基础分5分

        # 1. 涨停数量评分 (±1.5分) - 涨停生态核心指标
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

        # 2. 炸板率评分 (±1.5分) - 涨停生态核心指标
        if break_rate <= 0.2:
            score += 1.5
        elif break_rate <= 0.3:
            score += 0.8
        elif break_rate <= 0.5:
            score += 0
        elif break_rate <= 0.7:
            score -= 0.8
        else:
            score -= 1.5

        # 3. 昨日连板率评分 (±1.2分) - 提高权重，反映市场持续性
        avg_yesterday_rate = (yesterday_first_rate + yesterday_limit_rate) / 2
        if avg_yesterday_rate >= 0.4:
            score += 1.2
        elif avg_yesterday_rate >= 0.3:
            score += 0.8
        elif avg_yesterday_rate >= 0.2:
            score += 0.4
        elif avg_yesterday_rate < 0.1:
            score -= 0.6

        # 4. 昨日表现评分 (±1.0分)
        avg_yesterday_perf = (yesterday_first_perf + yesterday_limit_perf) / 2
        if avg_yesterday_perf >= 0.03:
            score += 1.0
        elif avg_yesterday_perf >= 0.01:
            score += 0.5
        elif avg_yesterday_perf <= -0.03:
            score -= 1.0
        elif avg_yesterday_perf <= -0.01:
            score -= 0.5

        # 5. [已移除] 连板数量评分 - 与连板率高度相关，避免重复评分
        # 原逻辑: total_yesterday_count = yesterday_first_count + yesterday_limit_count
        # 连板数量信息保留用于日志输出，但不再影响评分

        # 6. 大盘指数评分 (±1.0分) - 降低权重，辅助参考
        avg_index = (sh_index + hs300_index + cyb_index + sz_index) / 4

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

        # 7. 指数分化度评分 (±0.3分) - 降低权重
        index_values = [sh_index, hs300_index, cyb_index, sz_index]
        index_std = np.std(index_values)

        if index_std <= 0.5:
            score += 0.3
        elif index_std >= 1.5:
            score -= 0.3

        # 确保评分在1-10之间
        score = max(1.0, min(10.0, score))

        # 上次市场情绪评分
        with shared_data['市场情绪_评分'].get_lock():
            last_score = shared_data['市场情绪_评分'].value

        # 保存评分到共享数据
        with shared_data['市场情绪_评分'].get_lock():
            shared_data['市场情绪_评分'].value = score

        # 根据评分判断市场情绪强弱
        if score >= 8:
            sentiment = "极强"
            buy_advice = "积极扫板"
        elif score >= 7:
            sentiment = "强势"
            buy_advice = "适度扫板"
        elif score >= 5.5:
            sentiment = "中性偏强"
            buy_advice = "谨慎扫板"
        elif score >= 4:
            sentiment = "中性"
            buy_advice = "观望为主"
        elif score >= 2.5:
            sentiment = "弱势"
            buy_advice = "暂停扫板"
        else:
            sentiment = "极弱"
            buy_advice = "空仓等待"

        # 添加市场情绪趋势判断 - 结合大盘走势
        trend = ""
        if yesterday_first_perf > 2 and yesterday_limit_perf > 1 and avg_index > 0.5:
            trend = "↑向好"
        elif yesterday_first_perf < -2 and yesterday_limit_perf < -1 and avg_index < -0.5:
            trend = "↓转弱"
        else:
            trend = "→平稳"

        # 构建详细的评分说明
        score_details = (f"涨停数:{limit_up_count}只, 炸板率:{break_rate:.1%}, "
                         f"昨日连板率:{avg_yesterday_rate:.1%}, "
                         f"昨日表现:{avg_yesterday_perf:+.1f}%, "
                         f"大盘均值:{avg_index:+.2f}%")

        # 构建指数详情
        index_details = (f"上证:{sh_index:+.2f}%, 沪深300:{hs300_index:+.2f}%, "
                         f"创业板:{cyb_index:+.2f}%, 深成指:{sz_index:+.2f}%")

        key_msg = (
            f"【{current_time}】关键指标 - "
            f"市场情绪:{sentiment}{trend}, 评分:{score:.1f}/10 ({buy_advice}), "
            f"涨停数:{limit_up_count}, "
            f"炸板率:{break_rate:.1%}, 昨日首板连板:{yesterday_first_count}只, "
            f"昨日涨停连板:{yesterday_limit_count}只, "
            f"昨日首板表现:{yesterday_first_perf:+.2f}%, "
            f"昨日涨停表现:{yesterday_limit_perf:+.2f}%, "
            f"指数表现: {index_details}")

        logger.warning(key_msg)  # 使用warning级别确保重要信息被记录

        # 当评分发生显著变化时，发送额外提醒
        if abs(score - last_score) >= 1.0:
            logger.info(
                f"【市场情绪评分变化】{last_score:.1f} → {score:.1f}, {score_details}")
            send_email(
                '【市场情绪评分变化】',
                f"【市场情绪评分变化】{last_score:.1f} → {score:.1f}, {score_details}\n{key_msg}"
            )

        # 当大盘指数异常时，发送额外警告
        if avg_index <= -2.0:
            logger.info(f"【大盘异常下跌警告】{index_details}，建议谨慎操作或空仓观望")
        elif avg_index >= 2.0:
            logger.info(f"【大盘强势上涨】{index_details}，市场情绪高涨，可适当加大仓位")
    except Exception as e:
        logger.exception(f"【关键错误】记录关键市场指标发生错误: {e}")
        send_email('【关键错误】记录关键市场指标发生错误',
                   f'记录关键市场指标发生错误: {e}\n{traceback.format_exc()}')
