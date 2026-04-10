"""
monitor/sentiment_task.py - 市场情绪监控任务

从 打板策略_v2.4.py 提取的市场情绪监控定时任务及其安全包装函数。
包含：
- safe_calculate_market_sentiment_metrics: 市场情绪计算安全包装
- safe_ths_monitor_task: 同花顺监控安全包装
- safe_log_market_sentiment_summary_email: 邮件报告安全包装
- safe_log_market_sentiment_summary: 汇总报告安全包装
- safe_log_key_market_indicators: 关键指标安全包装
- safe_query_positions_and_orders: 持仓委托查询安全包装
- market_sentiment_monitor_task: 基于schedule的定时监控任务
- market_sentiment_monitor_task_manual: 手动时间控制版本的监控任务
"""

import os
import time
import traceback
from datetime import datetime

import schedule
from loguru import logger

from config import STOP_TIME, IP, PORT, STRATEGY_NAME
from infra.data_helpers import xtdata_connect, reconnect_xtdata
from infra.utils import send_email, run_with_timeout
from monitor.sentiment import calculate_market_sentiment_metrics
from monitor.dashboard import log_market_sentiment_summary_email
from monitor.indicators import log_key_market_indicators
from monitor.sector_monitor import ths_monitor_task


# ==================== 定时任务安全包装函数 ====================
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


def safe_calculate_market_sentiment_metrics(shared_data):
    """计算市场情绪指标的安全包装函数 (带超时与重连)"""
    try:
        # 设置 10 秒超时
        run_with_timeout(calculate_market_sentiment_metrics,
                         args=(shared_data, ),
                         timeout=60)
    except TimeoutError as e:
        logger.error(f"定时任务 calculate_market_sentiment_metrics 超时: {e}")
        send_email(
            '【定时任务超时】计算市场情绪指标',
            f'定时任务 calculate_market_sentiment_metrics 超时:\n{e}\n{traceback.format_exc()}'
        )
        # 超时后尝试重连
        reconnect_xtdata()
    except Exception as e:
        logger.exception(f"定时任务 calculate_market_sentiment_metrics 执行异常: {e}")
        send_email(
            '【定时任务异常】计算市场情绪指标',
            f'定时任务 calculate_market_sentiment_metrics 执行异常:\n{e}\n{traceback.format_exc()}'
        )


def safe_ths_monitor_task(shared_data):
    """同花顺监控任务的安全包装函数 (带超时与重连)"""
    try:
        # 设置 10 秒超时
        run_with_timeout(ths_monitor_task, args=(shared_data, ), timeout=60)
    except TimeoutError as e:
        logger.error(f"定时任务 ths_monitor_task 超时: {e}")
        send_email(
            '【定时任务超时】同花顺数据监控',
            f'定时任务 ths_monitor_task 超时:\n{e}\n{traceback.format_exc()}')

        # 超时后尝试重连
        reconnect_xtdata()
    except Exception as e:
        logger.exception(f"定时任务 ths_monitor_task 执行异常: {e}")
        send_email(
            '【定时任务异常】同花顺数据监控',
            f'定时任务 ths_monitor_task 执行异常:\n{e}\n{traceback.format_exc()}')


def safe_log_market_sentiment_summary_email(shared_data):
    """记录市场情绪汇总(旧版)的安全包装函数 (带超时与重连)"""
    try:
        # 设置 30 秒超时 (邮件发送可能较慢)
        run_with_timeout(log_market_sentiment_summary_email,
                         args=(shared_data, ),
                         timeout=60)
    except TimeoutError as e:
        logger.error(f"定时任务 log_market_sentiment_summary_email 超时: {e}")
        send_email(
            '【定时任务超时】记录市场情绪汇总(邮件通知)',
            f'定时任务 log_market_sentiment_summary_email 超时:\n{e}\n{traceback.format_exc()}'
        )
        reconnect_xtdata()
    except Exception as e:
        logger.exception(f"定时任务 log_market_sentiment_summary_email 执行异常: {e}")
        send_email(
            '【定时任务异常】记录市场情绪汇总(邮件通知)',
            f'定时任务 log_market_sentiment_summary_email 执行异常:\n{e}\n{traceback.format_exc()}'
        )


def safe_log_market_sentiment_summary(shared_data, strategy_name):
    """记录市场情绪汇总的安全包装函数 (带超时与重连)"""
    try:
        # 在包装函数内导入以避免循环导入
        from market_sentiment_report import log_market_sentiment_summary
        # 设置 30 秒超时
        run_with_timeout(log_market_sentiment_summary,
                         args=(shared_data, strategy_name),
                         timeout=60)
    except TimeoutError as e:
        logger.error(f"定时任务 log_market_sentiment_summary 超时: {e}")
        send_email(
            '【定时任务超时】记录市场情绪汇总',
            f'定时任务 log_market_sentiment_summary 超时:\n{e}\n{traceback.format_exc()}'
        )
        reconnect_xtdata()
    except Exception as e:
        logger.exception(f"定时任务 log_market_sentiment_summary 执行异常: {e}")
        send_email(
            '【定时任务异常】记录市场情绪汇总',
            f'定时任务 log_market_sentiment_summary 执行异常:\n{e}\n{traceback.format_exc()}'
        )


def safe_log_key_market_indicators(shared_data):
    """记录关键市场指标的安全包装函数 (带超时与重连)"""
    try:
        # 设置 5 秒超时
        run_with_timeout(log_key_market_indicators,
                         args=(shared_data, ),
                         timeout=60)
    except TimeoutError as e:
        logger.error(f"定时任务 log_key_market_indicators 超时: {e}")
        send_email(
            '【定时任务超时】记录关键市场指标',
            f'定时任务 log_key_market_indicators 超时:\n{e}\n{traceback.format_exc()}'
        )
        reconnect_xtdata()
    except Exception as e:
        logger.exception(f"定时任务 log_key_market_indicators 执行异常: {e}")
        send_email(
            '【定时任务异常】记录关键市场指标',
            f'定时任务 log_key_market_indicators 执行异常:\n{e}\n{traceback.format_exc()}'
        )


# ==================== 定时任务安全包装函数结束 ====================


def market_sentiment_monitor_task(shared_data):
    """市场情绪监控任务 - 使用schedule进行定时运行（简化版）"""

    try:
        logger.info(f'市场情绪监控任务已启动...')

        xtdata_connect(IP, PORT)

        # 设置定时任务 - 使用safe wrapper
        schedule.every(3).seconds.do(safe_calculate_market_sentiment_metrics,
                                     shared_data)
        schedule.every(3).seconds.do(safe_ths_monitor_task, shared_data)
        schedule.every(5).minutes.do(safe_log_market_sentiment_summary_email,
                                     shared_data)
        schedule.every(5).minutes.do(safe_log_market_sentiment_summary,
                                     shared_data, STRATEGY_NAME)
        schedule.every(5).seconds.do(safe_log_key_market_indicators,
                                     shared_data)

        while True:
            # 检查停止时间
            if datetime.now().time() >= STOP_TIME:
                logger.warning('【进程退出】市场情绪监控任务')
                break

            # 运行定时任务
            schedule.run_pending()

            # 短暂休眠
            time.sleep(1)

    except KeyboardInterrupt:
        logger.warning("用户中断，停止市场情绪监控任务")
    except Exception as e:
        logger.exception(f"【关键错误】市场情绪监控任务发生错误: {e}")
        send_email('【关键错误】市场情绪监控任务失败',
                   f'市场情绪监控任务时发生异常: {e}\n{traceback.format_exc()}')
    finally:
        # 清理定时任务
        schedule.clear()
        logger.warning('市场情绪监控任务已停止')


def market_sentiment_monitor_task_manual(shared_data):
    """市场情绪监控任务 - 手动时间控制版本（替代schedule库）

    使用手动时间轮询机制，避免schedule库的潜在问题：
    1. 每个任务独立异常处理，互不影响
    2. 没有第三方库的状态管理问题
    3. 更可靠的错误隔离
    """

    try:
        logger.info(f'市场情绪监控任务已启动（手动时间控制版本）...')
        xtdata_connect(IP, PORT)
        logger.info('xtdata连接成功')

        # 记录上次执行时间
        last_sentiment_calc = datetime.now()
        last_ths_monitor = datetime.now()
        last_email_report = datetime.now()
        last_summary_report = datetime.now()
        last_key_indicators = datetime.now()

        # 心跳时间
        last_heartbeat = datetime.now()

        while True:
            current_time = datetime.now()

            # 检查停止时间
            if current_time.time() >= STOP_TIME:
                logger.warning('【进程退出】市场情绪监控任务')
                break

            # 每3秒执行：市场情绪计算
            if (current_time - last_sentiment_calc).total_seconds() >= 3:
                try:
                    logger.debug('[任务开始] 计算市场情绪指标')
                    safe_calculate_market_sentiment_metrics(shared_data)
                    logger.debug('[任务结束] 计算市场情绪指标')
                    last_sentiment_calc = current_time
                except Exception as e:
                    logger.exception(
                        f'[定时任务异常] market_sentiment_metrics执行失败: {e}')

            # 每3秒执行：同花顺监控
            if (current_time - last_ths_monitor).total_seconds() >= 3:
                try:
                    logger.debug('[任务开始] 同花顺数据监控')
                    safe_ths_monitor_task(shared_data)
                    logger.debug('[任务结束] 同花顺数据监控')
                    last_ths_monitor = current_time
                except Exception as e:
                    logger.exception(f'[定时任务异常] ths_monitor执行失败: {e}')

            # 每5分钟执行：邮件报告
            if (current_time - last_email_report).total_seconds() >= 300:
                try:
                    logger.debug('[任务开始] 发送市场情绪邮件报告')
                    safe_log_market_sentiment_summary_email(shared_data)
                    logger.debug('[任务结束] 发送市场情绪邮件报告')
                    last_email_report = current_time
                except Exception as e:
                    logger.exception(f'[定时任务异常] email_report执行失败: {e}')

            # # 每5分钟执行：汇总报告
            # if (current_time - last_summary_report).total_seconds() >= 300:
            #     try:
            #         logger.debug('[任务开始] 记录市场情绪汇总')
            #         safe_log_market_sentiment_summary(shared_data,
            #                                           STRATEGY_NAME)
            #         logger.debug('[任务结束] 记录市场情绪汇总')
            #         last_summary_report = current_time
            #     except Exception as e:
            #         logger.exception(f'[定时任务异常] summary_report执行失败: {e}')

            # 每5秒执行：关键指标
            if (current_time - last_key_indicators).total_seconds() >= 5:
                try:
                    logger.debug('[任务开始] 记录关键市场指标')
                    safe_log_key_market_indicators(shared_data)
                    logger.debug('[任务结束] 记录关键市场指标')
                    last_key_indicators = current_time
                except Exception as e:
                    logger.exception(f'[定时任务异常] key_indicators执行失败: {e}')

            # 每60秒输出一次心跳日志 (基于时间差，而非循环次数)
            if (current_time - last_heartbeat).total_seconds() >= 60:
                logger.info(f'[心跳] 市场情绪监控任务运行正常 (PID: {os.getpid()})')
                last_heartbeat = current_time

            # 短暂休眠
            time.sleep(1)

    except KeyboardInterrupt:
        logger.warning("用户中断，停止市场情绪监控任务")
    except Exception as e:
        logger.exception(f"【关键错误】市场情绪监控任务发生错误: {e}")
        send_email('【关键错误】市场情绪监控任务失败',
                   f'市场情绪监控任务时发生异常: {e}\n{traceback.format_exc()}')
    finally:
        logger.warning('市场情绪监控任务已停止')
