"""
core/trailing_stop.py - 跟踪止盈止损价格计算

从 打板策略_v2.4.py 提取的 calculate_trailing_stop_prices() 函数。
"""

import json
import traceback

from loguru import logger

from config import STOP_LOSS_RATE
from infra.data_helpers import _round_price
from infra.utils import send_email


def calculate_trailing_stop_prices(highest_price: float,
                                   limit_down_price: float, stock_code: str,
                                   shared_data: dict):
    """
    计算跟踪止盈止损价格队列以及各卖出点位应该剩余的股票数量。

    优化策略（v2.4.2）：动态递进式止盈止损

    核心思想：
        当价格越靠近涨停价时，已获得的利润越丰厚，此时应该用更窄的止盈区间
        来保护利润，防止利润大幅回吐。反之，当价格距离涨停较远时，可以给予
        更大的波动容忍度。

    算法设计：
        1. 计算"利润空间比例" = (最高价 - 成本价) / 成本价
        2. 根据利润空间比例动态调整止盈档位的触发阈值和减仓力度
        3. 使用非线性递进的卖出区间：利润越高，区间越窄

    止盈档位设计（10档）：
        - 高利润区(利润>5%): 回撤0.5%即开始减仓，快速锁定利润
        - 中利润区(利润2-5%): 回撤1-2%开始减仓，平衡保护与波动
        - 低利润区(利润<2%): 回撤2-3%开始减仓，给予更多容忍度
        - 亏损区: 触及止损线(默认5%)则清仓止损

    v2.4.2 优化点：
        1. base_stop_loss_price 改为基于 highest_price 计算（跟踪止损核心逻辑）
        2. 改进止损价动态上移逻辑，高利润时保护更多利润
        3. 优化小仓位处理逻辑（前6档持有，后4档清仓）
        4. 增大不同距离涨停比例下的档位分布差异
        5. 增强详细日志记录，便于调试和回溯

    Args:
        highest_price (float): 区间最高价，用于计算价格列表的锚点价格。
        limit_down_price (float): 跌停价，止损价格不能低于此值。
        stock_code (str): 股票代码。
        shared_data (dict): 共享数据字典。
    """
    try:
        # ==================== 日志：函数入口参数 ====================
        logger.debug(
            f"[{stock_code}] [止盈止损计算-开始] 输入参数: "
            f"highest_price={highest_price:.3f}, limit_down_price={limit_down_price:.3f}"
        )

        position_str = shared_data['持仓状态'].get(stock_code)
        if not position_str:
            logger.warning(f"[{stock_code}] [止盈止损计算-失败] 未找到持仓信息，跳过计算。")
            return

        position_data = json.loads(position_str)
        available_volume = position_data.get('可用数量', 0)
        cost_price = position_data.get('成本价', 0)
        hold_volume = position_data.get('持仓数量', 0)

        # ==================== 日志：持仓信息 ====================
        logger.debug(
            f"[{stock_code}] [止盈止损计算-持仓] "
            f"成本价={cost_price:.3f}, 持仓数量={hold_volume}, 可用数量={available_volume}"
        )

        if available_volume <= 0:
            logger.warning(f"[{stock_code}] [止盈止损计算-失败] 可用数量为0，跳过计算。")
            return

        if cost_price <= 0:
            logger.warning(f"[{stock_code}] [止盈止损计算-失败] 成本价异常({cost_price})，跳过计算。")
            return

        # ==================== 1. 计算利润空间比例 ====================
        # 利润空间 = (最高价 - 成本价) / 成本价
        profit_ratio = (highest_price - cost_price) / cost_price if cost_price > 0 else 0

        # 获取涨停价用于计算距离涨停的比例
        stock_info = shared_data['股票信息'].get(stock_code, {})
        limit_up_price = stock_info.get('涨停价', highest_price * 1.1)

        # 距离涨停的比例 = (涨停价 - 最高价) / (涨停价 - 成本价)
        # 值越小表示越靠近涨停（0=已涨停，1=还在成本价）
        if limit_up_price > cost_price:
            distance_to_limit_ratio = (limit_up_price - highest_price) / (limit_up_price - cost_price)
            distance_to_limit_ratio = max(0, min(1, distance_to_limit_ratio))  # 限制在0-1之间
        else:
            distance_to_limit_ratio = 1.0  # 异常情况，使用最大值

        # ==================== 日志：利润空间计算结果 ====================
        logger.debug(
            f"[{stock_code}] [止盈止损计算-利润空间] "
            f"利润比例={profit_ratio:.2%}, 涨停价={limit_up_price:.3f}, "
            f"距涨停比例={distance_to_limit_ratio:.2%}"
        )

        # ==================== 2. 动态计算止损价格（v3.0优化：纯追踪止损+动态收窄） ====================
        # v3.0 核心改动：统一为纯追踪止损，止损百分比随盈利幅度动态收窄
        # 盈利越多 → 追踪止损越紧 → 保护更多利润
        # 盈利较少 → 追踪止损较松 → 给予波动空间
        if profit_ratio >= 0.10:
            trailing_rate = 0.025  # 盈利>10%: 从最高价回撤2.5%触发
            stop_loss_reason = "利润>=10%，紧追踪(2.5%回撤)"
        elif profit_ratio >= 0.05:
            trailing_rate = 0.03  # 盈利5-10%: 回撤3%
            stop_loss_reason = "利润5-10%，较紧追踪(3%回撤)"
        elif profit_ratio >= 0.02:
            trailing_rate = 0.04  # 盈利2-5%: 回撤4%
            stop_loss_reason = "利润2-5%，适度追踪(4%回撤)"
        else:
            trailing_rate = STOP_LOSS_RATE  # 盈利<2%: 回撤5%（默认）
            stop_loss_reason = f"利润<2%，标准追踪({STOP_LOSS_RATE:.0%}回撤)"

        trailing_stop = highest_price * (1 - trailing_rate)

        # 保底：止损价不低于成本价的95%（绝对底线）
        absolute_floor = cost_price * (1 - STOP_LOSS_RATE)

        stop_loss_price = _round_price(max(trailing_stop, absolute_floor, limit_down_price))

        # ==================== 日志：动态止损价格计算 ====================
        logger.debug(
            f"[{stock_code}] [止盈止损计算-动态追踪止损] "
            f"trailing_rate={trailing_rate:.1%}, trailing_stop={trailing_stop:.3f}, "
            f"absolute_floor={absolute_floor:.3f}, "
            f"最终stop_loss_price={stop_loss_price:.3f}, "
            f"原因: {stop_loss_reason}"
        )

        # ==================== 3. 动态计算止盈区间 ====================
        # 根据利润空间和距离涨停的比例，动态调整价格档位间距

        # 计算总的回撤空间
        total_drawdown_space = highest_price - stop_loss_price

        if total_drawdown_space <= 0:
            logger.warning(
                f"[{stock_code}] [止盈止损计算-警告] 回撤空间异常: "
                f"highest_price={highest_price:.3f}, stop_loss_price={stop_loss_price:.3f}, "
                f"total_drawdown_space={total_drawdown_space:.3f}，使用默认值"
            )
            total_drawdown_space = highest_price * STOP_LOSS_RATE  # 使用默认值

        # ==================== 日志：回撤空间计算 ====================
        logger.debug(
            f"[{stock_code}] [止盈止损计算-回撤空间] "
            f"total_drawdown_space={total_drawdown_space:.3f} "
            f"(highest_price={highest_price:.3f} - stop_loss_price={stop_loss_price:.3f})"
        )

        # ==================== 4. 非线性递进档位设计（优化：增大区间差异） ====================
        # 使用指数递进：靠近最高价的档位间距窄，远离的间距宽
        # 前几档（高利润区）间距窄，后几档（低利润/亏损区）间距相对宽

        # 根据距离涨停的比例动态调整档位分布
        if distance_to_limit_ratio <= 0.2:
            # 极度靠近涨停（利润丰厚），使用极窄的前档间距
            step_ratios = [0.02, 0.03, 0.05, 0.08, 0.12, 0.15, 0.15, 0.15, 0.13, 0.12]
            step_strategy = "极度靠近涨停(<=20%)"
        elif distance_to_limit_ratio <= 0.4:
            # 靠近涨停（盈利丰厚），使用较窄的前档间距
            step_ratios = [0.04, 0.06, 0.08, 0.10, 0.12, 0.13, 0.13, 0.12, 0.11, 0.11]
            step_strategy = "靠近涨停(20-40%)"
        elif distance_to_limit_ratio <= 0.6:
            # 中等距离，使用平衡的档位分布
            step_ratios = [0.06, 0.08, 0.10, 0.11, 0.12, 0.12, 0.12, 0.11, 0.10, 0.08]
            step_strategy = "中等距离(40-60%)"
        else:
            # 距离涨停较远（利润有限或微亏），使用较宽的前档间距
            step_ratios = [0.08, 0.09, 0.10, 0.11, 0.12, 0.12, 0.11, 0.10, 0.09, 0.08]
            step_strategy = "距离涨停较远(>60%)"

        # 确保比例总和为1
        total_ratio = sum(step_ratios)
        step_ratios = [r / total_ratio for r in step_ratios]

        # ==================== 日志：档位策略选择 ====================
        logger.debug(
            f"[{stock_code}] [止盈止损计算-档位策略] "
            f"选择策略: {step_strategy}, "
            f"step_ratios={[f'{r:.2%}' for r in step_ratios]}"
        )

        # 生成10个止盈止损价格点（从高到低），确保价格严格递减
        trailing_prices = []
        cumulative_drawdown = 0
        last_price = highest_price

        # ==================== 日志：开始生成价格档位 ====================
        logger.debug(f"[{stock_code}] [止盈止损计算-价格档位] 开始生成10个价格档位...")

        for i in range(10):
            cumulative_drawdown += step_ratios[i] * total_drawdown_space
            price = _round_price(highest_price - cumulative_drawdown)

            # 确保价格严格递减，至少相差0.01元
            if price >= last_price and i > 0:
                price = _round_price(last_price - 0.01)
                logger.debug(
                    f"[{stock_code}] [止盈止损计算-价格调整] "
                    f"档位{i+1}: 价格调整为 {price:.3f}（确保严格递减）"
                )

            # 价格不能低于跌停价
            if price < limit_down_price:
                price = limit_down_price
                logger.debug(
                    f"[{stock_code}] [止盈止损计算-价格调整] "
                    f"档位{i+1}: 价格调整为跌停价 {price:.3f}"
                )

            trailing_prices.append(price)
            last_price = price

        # ==================== 5. 动态计算目标剩余仓位 ====================
        # 根据利润空间调整减仓力度：利润越高，减仓越积极

        # 选择仓位比例策略
        if profit_ratio >= 0.05:
            # 高利润区（>=5%）：激进保护
            position_ratios = [1.0, 0.85, 0.70, 0.55, 0.40, 0.25, 0.15, 0.08, 0.03, 0.0]
            position_strategy = "高利润区(>=5%)-激进保护"
        elif profit_ratio >= 0.03:
            # 中高利润区（3-5%）：积极保护
            position_ratios = [1.0, 0.90, 0.80, 0.65, 0.50, 0.35, 0.20, 0.10, 0.03, 0.0]
            position_strategy = "中高利润区(3-5%)-积极保护"
        elif profit_ratio >= 0.02:
            # 中等利润区（2-3%）：适度保护
            position_ratios = [1.0, 1.0, 0.85, 0.70, 0.55, 0.40, 0.25, 0.12, 0.05, 0.0]
            position_strategy = "中等利润区(2-3%)-适度保护"
        elif profit_ratio >= 0.01:
            # 低利润区（1-2%）：保守保护
            position_ratios = [1.0, 1.0, 1.0, 0.80, 0.65, 0.45, 0.30, 0.15, 0.05, 0.0]
            position_strategy = "低利润区(1-2%)-保守保护"
        elif profit_ratio >= 0:
            # 微利/平盘区（0-1%）：宽容策略
            position_ratios = [1.0, 1.0, 1.0, 1.0, 0.75, 0.55, 0.35, 0.20, 0.08, 0.0]
            position_strategy = "微利/平盘区(0-1%)-宽容策略"
        else:
            # 亏损区：止损策略
            position_ratios = [1.0, 1.0, 1.0, 1.0, 1.0, 0.75, 0.55, 0.35, 0.15, 0.0]
            position_strategy = "亏损区-止损策略"

        # ==================== 日志：仓位策略选择 ====================
        logger.debug(
            f"[{stock_code}] [止盈止损计算-仓位策略] "
            f"选择策略: {position_strategy}, "
            f"position_ratios={[f'{r:.0%}' for r in position_ratios]}"
        )

        target_volumes = []

        for i, ratio in enumerate(position_ratios):
            if available_volume < 100:
                # 不足1手：前6档持有，后4档清仓
                vol = available_volume if i < 6 else 0
                if i == 0:
                    logger.debug(
                        f"[{stock_code}] [止盈止损计算-小仓位] "
                        f"可用数量{available_volume}<100股，前6档持有，后4档清仓"
                    )
            elif ratio >= 1.0:
                vol = available_volume
            elif ratio <= 0:
                vol = 0
            else:
                # 按比例计算，向下取整到100股
                vol = int(available_volume * ratio / 100) * 100
                # 修正：如果计算结果为0但目标比例>0，至少保留100股
                if vol == 0 and ratio > 0:
                    vol = 100

            if target_volumes:
                vol = min(vol, target_volumes[-1])
            vol = min(vol, available_volume)
            target_volumes.append(vol)

        # 确保最后一个仓位为0（止损清仓）
        target_volumes[-1] = 0

        # ==================== 6. 更新共享数据 ====================
        stock_status_signal = shared_data['股票状态信号'][stock_code]
        price_array = stock_status_signal['止盈止损价格列表']
        volume_array = stock_status_signal['目标剩余仓位']

        # 数据验证
        if len(target_volumes) != len(volume_array) or len(trailing_prices) != len(price_array):
            error_msg = (
                f'数据长度有误，target_volumes: {len(target_volumes)}, '
                f'trailing_prices: {len(trailing_prices)}, '
                f'expected: {len(volume_array)}'
            )
            logger.error(f"[{stock_code}] [止盈止损计算-错误] {error_msg}")
            raise Exception(error_msg)

        with price_array.get_lock(), volume_array.get_lock():
            for i in range(len(trailing_prices)):
                price_array[i] = trailing_prices[i]
            for i in range(len(target_volumes)):
                volume_array[i] = target_volumes[i]

        # ==================== 7. 详细日志记录（增强版） ====================
        # 构建档位详情表格
        level_details = []
        for i in range(10):
            drawdown_from_high = (highest_price - trailing_prices[i]) / highest_price * 100
            sell_volume = available_volume - target_volumes[i]
            level_details.append(
                f"档位{i+1}: 价格={trailing_prices[i]:.2f} "
                f"(回撤{drawdown_from_high:.1f}%), "
                f"剩余={target_volumes[i]}股, "
                f"卖出={sell_volume}股"
            )

        # INFO 级别：简要汇总
        logger.info(
            f"[{stock_code}] [止盈止损计算-完成] "
            f"最高价={highest_price:.2f}, 成本价={cost_price:.2f}, "
            f"利润={profit_ratio:.2%}, 止损价={stop_loss_price:.2f}"
        )

        # DEBUG 级别：详细参数
        logger.debug(
            f"[{stock_code}] [止盈止损计算-参数汇总] "
            f"涨停价={limit_up_price:.2f}, 跌停价={limit_down_price:.2f}, "
            f"距涨停={distance_to_limit_ratio:.2%}, "
            f"回撤空间={total_drawdown_space:.2f}"
        )

        # DEBUG 级别：策略选择
        logger.debug(
            f"[{stock_code}] [止盈止损计算-策略] "
            f"档位策略={step_strategy}, 仓位策略={position_strategy}"
        )

        # DEBUG 级别：价格和仓位列表
        logger.debug(
            f"[{stock_code}] [止盈止损计算-价格列表] "
            f"{[f'{p:.2f}' for p in trailing_prices]}"
        )
        logger.debug(
            f"[{stock_code}] [止盈止损计算-仓位列表] {target_volumes}"
        )

        # DEBUG 级别：各档位详情
        for detail in level_details:
            logger.debug(f"[{stock_code}] [止盈止损计算-档位] {detail}")

    except Exception as e:
        logger.exception(
            f"[{stock_code}] [止盈止损计算-异常] 计算止盈止损价格时发生异常: {e}\n"
            f"输入参数: highest_price={highest_price}, limit_down_price={limit_down_price}"
        )
        send_email(
            f'【关键错误】计算止盈止损价格失败: {stock_code}',
            f'计算止盈止损价格时发生异常: {e}\n'
            f'输入参数: highest_price={highest_price}, limit_down_price={limit_down_price}\n'
            f'{traceback.format_exc()}'
        )
