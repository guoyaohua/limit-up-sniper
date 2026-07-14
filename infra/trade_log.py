"""
infra/trade_log.py - 交易日志 & 涨停列表保存

从 打板策略_v2.4.py 提取的交易日志和涨停列表保存函数。
"""

import os
import json
import traceback
import hashlib
from datetime import datetime
from loguru import logger

from config import TRADE_LOG_DIR, TODAY, VERSION
from infra.common_enums import StockLimitStatusInt
from infra.utils import send_email


EVENT_LOG_FILENAME = 'events.jsonl'


def _json_default(obj):
    """JSON 序列化兜底。"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _get_trade_log_dir(date_str: str | None = None) -> str:
    """获取当日交易日志目录。"""
    if date_str is None:
        date_str = datetime.now().strftime('%Y%m%d')
    log_dir = os.path.join(TRADE_LOG_DIR, date_str)
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _build_tick_snapshot(snapshot: dict | None) -> dict:
    """提取适合落盘的 Tick 摘要。"""
    if not snapshot:
        return {}

    summary = {}
    for field in ('time', 'lastPrice', 'open', 'high', 'low', 'lastClose',
                  'amount', 'volume', 'pvolume', 'stockStatus',
                  'limitUpPrice', 'upperLimitPrice'):
        if field in snapshot:
            summary[field] = snapshot.get(field)

    for field in ('bidPrice', 'askPrice', 'bidVol', 'askVol'):
        values = snapshot.get(field)
        if values:
            summary[field] = values[0]

    return summary


def append_trade_event(event_record: dict, date_str: str | None = None):
    """追加结构化事件到当日日志 JSONL。"""
    try:
        filepath = os.path.join(
            _get_trade_log_dir(date_str), EVENT_LOG_FILENAME
        )
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(
                json.dumps(event_record,
                           ensure_ascii=False,
                           default=_json_default) + '\n')
    except Exception as e:
        logger.debug(f'保存事件日志失败: {e}')


def record_strategy_event(shared_data: dict,
                          event_type: str,
                          stock_code: str,
                          stock_name: str = '',
                          reason: str = '',
                          snapshot: dict | None = None,
                          extra: dict | None = None) -> dict:
    """记录统一的盘中结构化事件。"""
    timestamp_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
    event_record = {
        'event_type': event_type,
        'timestamp': timestamp_now,
        'stock_code': stock_code,
    }
    if shared_data:
        event_record['signal_source'] = shared_data.get('信号来源', 'primary')
    if stock_name:
        event_record['stock_name'] = stock_name
    if reason:
        event_record['reason'] = reason

    market_sentiment = shared_data.get('市场情绪_评分') if shared_data else None
    if hasattr(market_sentiment, 'value'):
        event_record['market_sentiment'] = market_sentiment.value

    snapshot_summary = _build_tick_snapshot(snapshot)
    if snapshot_summary:
        event_record['snapshot'] = snapshot_summary

    if extra:
        event_record.update(extra)

    if shared_data:
        intraday_snapshot = shared_data.get('盘中特征快照')
        if intraday_snapshot is not None and stock_code:
            intraday_snapshot[stock_code] = {
                'event_type': event_type,
                'timestamp': timestamp_now,
                'stock_name': stock_name,
                'reason': reason,
                **snapshot_summary,
            }

        event_buffer = shared_data.get('盘中事件流')
        if event_buffer is not None:
            event_buffer.append(event_record)

    append_trade_event(event_record)
    return event_record


def _save_trade_record(trade_record: dict, event_type: str):
    """Persist one normalized submission/fill record."""
    log_dir = _get_trade_log_dir()
    timestamp = datetime.now().strftime('%H%M%S_%f')
    stock_code = trade_record.get('stock_code', 'unknown')
    prefix = 'fill' if trade_record.get('record_type') == 'fill' else 'trade'
    filename = f'{prefix}_{timestamp}_{stock_code}.json'
    filepath = os.path.join(log_dir, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(trade_record, f, ensure_ascii=False, indent=2, default=str)
    append_trade_event({'event_type': event_type, **trade_record})


def save_trade_log(trade_record: dict):
    """Save an accepted order-attempt record for operational auditing."""
    try:
        # Submission is not execution.  Keep these files available for the
        # decision funnel, but label them explicitly so PnL review cannot pair
        # an unfilled limit-up queue order as if it were a broker fill.
        trade_record = {
            **trade_record,
            'record_type': 'order_submission',
            'execution_status': 'SUBMITTED_NOT_FILLED',
        }
        _save_trade_record(trade_record, 'order_submitted')
    except Exception as e:
        logger.debug(f'保存交易日志失败: {e}')


def save_trade_fill(fill_record: dict):
    """Persist a broker-confirmed fill for PnL review and deduplication."""
    try:
        record = {
            **fill_record,
            'record_type': 'fill',
            'execution_status': 'FILLED',
        }
        trade_id = str(record.get('trade_id', '')).strip()
        if not trade_id:
            raise ValueError('broker fill is missing trade_id')
        trade_date = str(record.get('trade_date', '')).strip()
        if trade_date:
            parsed_date = datetime.strptime(trade_date, '%Y%m%d')
            if parsed_date.strftime('%Y%m%d') != trade_date:
                raise ValueError(f'invalid broker trade_date: {trade_date!r}')
        else:
            trade_date = datetime.now().strftime('%Y%m%d')
            record['trade_date'] = trade_date
        # QMT may replay callbacks after reconnect.  One deterministic broker
        # trade file makes the fill idempotent across threads and restarts.
        log_dir = _get_trade_log_dir(trade_date)
        safe_trade_id = ''.join(
            char if char.isalnum() or char in ('-', '_') else '_'
            for char in trade_id
        )[:80]
        if not safe_trade_id:
            safe_trade_id = 'unknown'
        identity = '|'.join((
            str(record.get('account_id', '')),
            str(record.get('strategy_name', '')),
            trade_id,
        ))
        identity_hash = hashlib.sha256(
            identity.encode('utf-8')
        ).hexdigest()[:16]
        filepath = os.path.join(
            log_dir, f'fill_{safe_trade_id}_{identity_hash}.json'
        )
        if os.path.exists(filepath):
            return False
        temporary = f'{filepath}.{os.getpid()}.tmp'
        with open(temporary, 'x', encoding='utf-8') as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(temporary, filepath)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
        append_trade_event(
            {'event_type': 'trade_filled', **record}, date_str=trade_date
        )
        return True
    except Exception as e:
        logger.debug(f'保存成交回报失败: {e}')
        return False


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
