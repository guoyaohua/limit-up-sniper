"""
infra/trade_log.py - 交易日志 & 涨停列表保存

从 打板策略_v2.4.py 提取的交易日志和涨停列表保存函数。
"""

import os
import json
import traceback
from datetime import datetime
from loguru import logger

from config import TRADE_LOG_DIR, TODAY, VERSION
from infra.common_enums import StockLimitStatusInt
from infra.utils import send_email


def save_trade_log(trade_record: dict):
    """U8升级：保存结构化交易日志到 JSON 文件"""
    try:
        date_str = datetime.now().strftime('%Y%m%d')
        log_dir = os.path.join(TRADE_LOG_DIR, date_str)
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%H%M%S_%f')
        stock_code = trade_record.get('stock_code', 'unknown')
        filename = f'trade_{timestamp}_{stock_code}.json'
        filepath = os.path.join(log_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(trade_record, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.debug(f'保存交易日志失败: {e}')


def save_daily_limit_up_list(shared_data):
    """
    保存当日涨停列表

    根据shared_data中记录的当日涨停数据，获取当日涨停股票列表，
    将今日涨停的股票分为涨停和首板涨停两个列表，分别保存到文件中

    Args:
        shared_data (dict): 共享数据字典，包含涨停池和昨日涨停股票信息
    """
    try:
        # 实际涨停池
        real_limit_up_pool = []
        # 炸板股票列表
        break_list = []
        for stock_code in shared_data['涨停池'].keys():
            with shared_data['股票状态信号'][stock_code]['股票状态'].get_lock():
                stock_status_value = shared_data['股票状态信号'][stock_code][
                    '股票状态'].value
            if stock_status_value == StockLimitStatusInt.LIMIT_UP:
                real_limit_up_pool.append(stock_code)
            else:
                break_list.append(stock_code)

        # 获取昨日涨停股票列表
        yesterday_limit_up_stocks = set(shared_data.get('昨日涨停股票', []))

        # 分类今日涨停股票
        today_limit_up_stocks = []  # 所有涨停股票（包括连板）
        first_limit_up_stocks = []  # 首次涨停股票

        for stock_code in real_limit_up_pool:
            today_limit_up_stocks.append(stock_code)
            # 如果不在昨日涨停股票中，则为首次涨停
            if stock_code not in yesterday_limit_up_stocks:
                first_limit_up_stocks.append(stock_code)

        # 确保输出目录存在
        output_dir = os.path.join('output', '涨停列表')
        os.makedirs(output_dir, exist_ok=True)

        # 保存所有涨停股票列表
        limit_up_file = os.path.join(output_dir, f'涨停_{TODAY}.txt')
        with open(limit_up_file, 'w', encoding='utf-8') as f:
            for stock_code in today_limit_up_stocks:
                f.write(f"{stock_code}\n")

        # 保存首次涨停股票列表
        first_limit_up_file = os.path.join(output_dir, f'首次涨停_{TODAY}.txt')
        with open(first_limit_up_file, 'w', encoding='utf-8') as f:
            for stock_code in first_limit_up_stocks:
                f.write(f"{stock_code}\n")

        # 保存炸板股票列表
        break_list_file = os.path.join(output_dir, f'炸板_{TODAY}.txt')
        with open(break_list_file, 'w', encoding='utf-8') as f:
            for stock_code in break_list:
                f.write(f"{stock_code}\n")

        # 记录日志
        logger.info(f"【涨停列表保存完成】")
        logger.info(f"  - 总涨停数量: {len(today_limit_up_stocks)}")
        logger.info(f"  - 首次涨停数量: {len(first_limit_up_stocks)}")
        logger.info(
            f"  - 连板数量: {len(today_limit_up_stocks) - len(first_limit_up_stocks)}"
        )
        logger.info(f"  - 涨停列表文件: {limit_up_file}")
        logger.info(f"  - 首次涨停列表文件: {first_limit_up_file}")
        logger.info(f"  - 炸板股票列表文件: {break_list_file}")

        if today_limit_up_stocks:
            logger.info(f"  - 涨停股票: {', '.join(today_limit_up_stocks)}")
        if first_limit_up_stocks:
            logger.info(f"  - 首次涨停股票: {', '.join(first_limit_up_stocks)}")
        if break_list:
            logger.info(f"  - 炸板股票: {', '.join(break_list)}")

        # 发送邮件通知
        email_subject = f"【{TODAY}】涨停列表保存完成"
        email_content = f"""
涨停列表保存完成通知

日期: {TODAY}
策略版本: {VERSION}

统计结果:
- 总涨停数量: {len(today_limit_up_stocks)}
- 首次涨停数量: {len(first_limit_up_stocks)}
- 连板数量: {len(today_limit_up_stocks) - len(first_limit_up_stocks)}

涨停股票列表:
{chr(10).join(today_limit_up_stocks) if today_limit_up_stocks else '无'}

首次涨停股票列表:
{chr(10).join(first_limit_up_stocks) if first_limit_up_stocks else '无'}

文件保存路径:
- 涨停列表: {limit_up_file}
- 首次涨停列表: {first_limit_up_file}
        """

        try:
            send_email(email_subject, email_content)
            logger.info("【邮件通知】涨停列表保存完成邮件发送成功")
        except Exception as e:
            logger.error(f"【邮件通知】涨停列表保存完成邮件发送失败: {e}")

    except Exception as e:
        logger.exception(f"【错误】保存当日涨停列表失败: {e}")
        try:
            send_email("【错误】保存当日涨停列表失败",
                       f"保存当日涨停列表时发生异常: {e}\n{traceback.format_exc()}")
        except:
            pass
