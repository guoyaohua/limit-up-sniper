"""
打板策略每日自动复盘脚本（U9升级）

功能：
1. 读取截至目标日的明确成交账本与当日事件流
2. 跨日 FIFO 配对，计算目标日实现盈亏
3. 统计当日核心指标与决策漏斗
4. 输出 Markdown 格式的日度复盘报告

用法：
    python review_daily.py [--date YYYYMMDD]
"""

import argparse
import json
import os
import glob
import math
from datetime import datetime, date
from collections import defaultdict


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADE_LOG_DIR = os.path.join(ROOT_DIR, 'output', 'trade_logs')
REPORT_DIR = os.path.join(ROOT_DIR, 'output', 'review_reports')
EVENT_LOG_FILENAME = 'events.jsonl'
COMMISSION_RATE = float(os.getenv('LIMIT_UP_PAPER_COMMISSION_RATE', '0.0003'))
MIN_COMMISSION = float(os.getenv('LIMIT_UP_PAPER_MIN_COMMISSION', '5'))
STAMP_DUTY_RATE = float(
    os.getenv('LIMIT_UP_PAPER_STAMP_DUTY_RATE', '0.0005')
)
TRANSFER_FEE_RATE = float(
    os.getenv('LIMIT_UP_PAPER_TRANSFER_FEE_RATE', '0.00001')
)


def _normalize_date_str(date_str: str) -> str:
    """Validate a review date before it is used as a directory name."""
    parsed = datetime.strptime(str(date_str), '%Y%m%d')
    normalized = parsed.strftime('%Y%m%d')
    if normalized != str(date_str):
        raise ValueError(f'无效日期: {date_str!r}')
    return normalized


def _record_trade_date(record: dict) -> str:
    """Return YYYYMMDD from an explicit fill date, timestamp, or log dir."""
    for field in ('trade_date', '_log_date'):
        value = str(record.get(field, '')).strip()
        if len(value) == 8 and value.isdigit():
            try:
                return _normalize_date_str(value)
            except ValueError:
                pass
    timestamp = str(record.get('timestamp', '')).strip()
    if len(timestamp) >= 10:
        try:
            return datetime.strptime(timestamp[:10], '%Y-%m-%d').strftime(
                '%Y%m%d'
            )
        except ValueError:
            pass
    return ''


def _estimated_fees(side: str, price: float, volume: int) -> float:
    """Conservative fallback when broker fills omit explicit fees."""
    amount = price * volume
    commission_rate = (
        COMMISSION_RATE if math.isfinite(COMMISSION_RATE)
        and COMMISSION_RATE >= 0 else 0.0003
    )
    min_commission = (
        MIN_COMMISSION if math.isfinite(MIN_COMMISSION)
        and MIN_COMMISSION >= 0 else 5.0
    )
    transfer_rate = (
        TRANSFER_FEE_RATE if math.isfinite(TRANSFER_FEE_RATE)
        and TRANSFER_FEE_RATE >= 0 else 0.00001
    )
    stamp_rate = (
        STAMP_DUTY_RATE if math.isfinite(STAMP_DUTY_RATE)
        and STAMP_DUTY_RATE >= 0 else 0.0005
    )
    commission = max(min_commission, amount * commission_rate)
    transfer = amount * transfer_rate
    stamp = amount * stamp_rate if side == 'SELL' else 0.0
    return commission + transfer + stamp


def _fill_fees(fill: dict) -> float:
    value = fill.get('fees')
    if value is not None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = math.nan
        # Older QMT builds and some broker adapters expose a zero placeholder
        # before fees settle. Treat it as missing; otherwise daily net PnL is
        # silently overstated.
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    return _estimated_fees(
        str(fill.get('action', '')).upper(),
        float(fill.get('price', 0)),
        int(fill.get('volume', 0)),
    )


def load_trade_logs(date_str: str) -> list:
    """Load broker-confirmed fills recorded on one trading date."""
    date_str = _normalize_date_str(date_str)
    log_dir = os.path.join(TRADE_LOG_DIR, date_str)
    if not os.path.exists(log_dir):
        print(f'交易日志目录不存在: {log_dir}')
        return []

    logs = []
    filepaths = sorted(
        glob.glob(os.path.join(log_dir, 'trade_*.json'))
        + glob.glob(os.path.join(log_dir, 'fill_*.json'))
    )
    for filepath in filepaths:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                record = json.load(f)
                if isinstance(record, dict):
                    record = dict(record)
                    record['_log_date'] = date_str
                    logs.append(record)
        except Exception as e:
            print(f'读取文件失败 {filepath}: {e}')
    # Both an explicit fill type and FILLED status are required.  Legacy
    # "trade" files were order submissions and therefore cannot prove an
    # execution unless they also carry the explicit broker status.
    confirmed = []
    for log in logs:
        record_type = log.get('record_type')
        execution_status = log.get('execution_status')
        if (record_type in ('trade', 'fill')
                and execution_status == 'FILLED'):
            confirmed.append(log)
    return confirmed


def load_trade_ledger(date_str: str) -> list:
    """Load all confirmed fills through ``date_str`` for FIFO inventory.

    A-share exits normally occur on a later trading date than their buys.  A
    single-day directory cannot reconstruct cost basis, so daily review uses
    every prior fill while still reporting only the target day's realization.
    """
    target = _normalize_date_str(date_str)
    if not os.path.isdir(TRADE_LOG_DIR):
        return []

    ledger = []
    seen = set()
    for entry in sorted(os.scandir(TRADE_LOG_DIR), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        try:
            trade_date = _normalize_date_str(entry.name)
        except ValueError:
            continue
        if trade_date > target:
            continue
        for fill in load_trade_logs(trade_date):
            effective_date = _record_trade_date(fill) or trade_date
            if effective_date > target:
                continue
            trade_id = str(fill.get('trade_id', '')).strip()
            identity = (
                effective_date,
                str(fill.get('account_id', '')),
                str(fill.get('strategy_name', '')),
                trade_id,
            )
            if trade_id and identity in seen:
                continue
            if trade_id:
                seen.add(identity)
            ledger.append(fill)
    return ledger


def load_trade_events(date_str: str) -> list:
    """加载指定日期的所有结构化事件。"""
    date_str = _normalize_date_str(date_str)
    log_dir = os.path.join(TRADE_LOG_DIR, date_str)
    filepath = os.path.join(log_dir, EVENT_LOG_FILENAME)
    if not os.path.exists(filepath):
        return []

    events = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception as e:
                    print(f'解析事件失败: {e}')
    except Exception as e:
        print(f'读取事件日志失败 {filepath}: {e}')

    return events


def match_buy_sell_pairs(logs: list) -> list:
    """FIFO-match confirmed fills, including partial executions and fees."""
    inventories = defaultdict(list)
    pairs = []
    ordered = sorted(logs, key=lambda item: (
        _record_trade_date(item), str(item.get('timestamp', '')),
        str(item.get('trade_id', ''))
    ))
    for fill in ordered:
        code = str(fill.get('stock_code', ''))
        side = str(fill.get('action', '')).upper()
        if not code or side not in ('BUY', 'SELL'):
            continue
        try:
            price = float(fill.get('price', 0))
            volume = int(fill.get('volume', 0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0 or volume <= 0:
            continue
        fee_per_share = _fill_fees(fill) / volume
        if not math.isfinite(fee_per_share) or fee_per_share < 0:
            continue
        inventory_key = (
            str(fill.get('account_id', '')),
            str(fill.get('strategy_name', '')),
            code,
        )
        if side == 'BUY':
            inventories[inventory_key].append({
                **fill, 'remaining': volume, 'fee_per_share': fee_per_share,
            })
            continue
        remaining_sell = volume
        sell_fee_per_share = fee_per_share
        while remaining_sell > 0 and inventories[inventory_key]:
            buy = inventories[inventory_key][0]
            matched = min(remaining_sell, int(buy['remaining']))
            buy_price = float(buy['price'])
            gross = (price - buy_price) * matched
            fees = (float(buy['fee_per_share']) + sell_fee_per_share) * matched
            pnl_amount = gross - fees
            cost = buy_price * matched + float(buy['fee_per_share']) * matched
            pairs.append({
                'stock_code': code, 'buy_price': buy_price,
                'sell_price': price, 'volume': matched,
                'pnl_pct': pnl_amount / cost * 100 if cost > 0 else None,
                'pnl_amount': pnl_amount, 'fees': fees,
                'buy_time': buy.get('timestamp', ''),
                'sell_time': fill.get('timestamp', ''),
                'buy_date': _record_trade_date(buy),
                'sell_date': _record_trade_date(fill),
                'account_id': fill.get('account_id', ''),
                'strategy_name': fill.get('strategy_name', ''),
                'buy_reason': buy.get('order_remark', ''),
                'sell_trigger': fill.get('order_remark', ''),
            })
            buy['remaining'] -= matched
            remaining_sell -= matched
            if buy['remaining'] <= 0:
                inventories[inventory_key].pop(0)
        if remaining_sell > 0:
            pairs.append({
                'stock_code': code, 'buy_price': None, 'sell_price': price,
                'volume': remaining_sell, 'pnl_pct': None, 'pnl_amount': None,
                'buy_time': None, 'sell_time': fill.get('timestamp', ''),
                'buy_date': None, 'sell_date': _record_trade_date(fill),
                'account_id': fill.get('account_id', ''),
                'strategy_name': fill.get('strategy_name', ''),
                'buy_reason': '', 'sell_trigger': fill.get('order_remark', ''),
                'status': '期初持仓或缺失买入成交',
            })
    for (account_id, strategy_name, code), lots in inventories.items():
        for buy in lots:
            pairs.append({
                'stock_code': code, 'buy_price': float(buy['price']),
                'sell_price': None, 'volume': int(buy['remaining']),
                'pnl_pct': None, 'pnl_amount': None,
                'buy_time': buy.get('timestamp', ''), 'sell_time': None,
                'buy_date': _record_trade_date(buy), 'sell_date': None,
                'account_id': account_id, 'strategy_name': strategy_name,
                'buy_reason': buy.get('order_remark', ''), 'sell_trigger': '',
                'status': '未平仓',
            })
    return sorted(pairs, key=lambda item: (
        item.get('pnl_amount') is None,
        str(item.get('sell_time') or item.get('buy_time') or ''),
        str(item.get('stock_code', '')),
    ))


def select_daily_pairs(pairs: list, date_str: str) -> list:
    """Keep exits realized on the report date and inventory still open."""
    date_str = _normalize_date_str(date_str)
    return [
        pair for pair in pairs
        if pair.get('sell_price') is None or pair.get('sell_date') == date_str
    ]


def build_event_summary(events: list) -> dict:
    """构建结构化事件摘要。"""
    summary = {
        'candidate_seen': 0,
        'buy_decision': 0,
        'cancel_decision': 0,
        'sell_decision': 0,
        'watchlist_enter': 0,
        'watchlist_release': 0,
        'blacklist_enter': 0,
        'break_episode_start': 0,
        'break_episode_end': 0,
        'limit_up': 0,
        'order_submitted': 0,
        'trade_filled': 0,
        'buy_types': defaultdict(int),
        'sell_remarks': defaultdict(int),
    }

    for event in events:
        event_type = event.get('event_type', '')
        if event_type in summary and isinstance(summary[event_type], int):
            summary[event_type] += 1

        if event_type == 'buy_decision':
            buy_type = event.get('buy_type', '未知')
            summary['buy_types'][buy_type] += 1
        elif event_type == 'sell_decision':
            remark = event.get('order_remark', '未知')
            summary['sell_remarks'][remark] += 1

    return summary


def _format_reason_block(lines: list, title: str, pairs: list, reason_key: str):
    """输出原因摘要块。"""
    lines.append(title)
    matched = False
    for pair in pairs:
        reason = pair.get(reason_key)
        if reason:
            matched = True
            lines.append(f'### {pair["stock_code"]}')
            lines.append('```text')
            lines.append(reason[:500])
            lines.append('```\n')
    if not matched:
        lines.append('无\n')


def generate_report(date_str: str, logs: list, events: list, pairs: list) -> str:
    """生成 Markdown 复盘报告。"""
    lines = []
    lines.append(f'# 打板策略日度复盘 — {date_str}\n')
    lines.append(f'> 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')

    buy_count = sum(1 for l in logs if l.get('action') == 'BUY')
    sell_count = sum(1 for l in logs if l.get('action') == 'SELL')
    completed = [p for p in pairs if p.get('pnl_pct') is not None]
    wins = [p for p in completed if p['pnl_pct'] > 0]
    losses = [p for p in completed if p['pnl_pct'] <= 0]
    event_summary = build_event_summary(events)

    lines.append('## 一、核心指标\n')
    lines.append('| 指标 | 数值 |')
    lines.append('|------|------|')
    lines.append(f'| 买入次数 | {buy_count} |')
    lines.append(f'| 卖出次数 | {sell_count} |')
    lines.append(f'| 完成交易笔数 | {len(completed)} |')
    lines.append(f'| 事件总数 | {len(events)} |')

    if completed:
        win_rate = len(wins) / len(completed) * 100
        avg_win = sum(p['pnl_pct'] for p in wins) / len(wins) if wins else 0
        avg_loss = sum(p['pnl_pct'] for p in losses) / len(losses) if losses else 0
        pnl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
        total_pnl = sum(p['pnl_amount'] for p in completed)
        max_win = max(p['pnl_pct'] for p in completed)
        max_loss = min(p['pnl_pct'] for p in completed)

        lines.append(f'| 胜率 | {win_rate:.1f}% |')
        lines.append(f'| 平均盈利 | {avg_win:.2f}% |')
        lines.append(f'| 平均亏损 | {avg_loss:.2f}% |')
        lines.append(f'| 盈亏比 | {pnl_ratio:.2f} |')
        lines.append(f'| 总盈亏 | {total_pnl:.2f} 元 |')
        lines.append(f'| 最大单笔盈利 | {max_win:.2f}% |')
        lines.append(f'| 最大单笔亏损 | {max_loss:.2f}% |')
    else:
        lines.append('| 胜率 | N/A（无完成交易） |')

    lines.append('')
    lines.append('## 二、决策漏斗\n')
    lines.append('| 漏斗节点 | 数量 |')
    lines.append('|----------|------|')
    lines.append(f'| 涨停事件 | {event_summary["limit_up"]} |')
    lines.append(f'| 买入决策 | {event_summary["buy_decision"]} |')
    lines.append(f'| 发单事件 | {event_summary["order_submitted"]} |')
    lines.append(f'| 真实成交回报 | {event_summary["trade_filled"]} |')
    lines.append(f'| 撤单决策 | {event_summary["cancel_decision"]} |')
    lines.append(f'| 卖出决策 | {event_summary["sell_decision"]} |')
    lines.append(f'| 观察名单加入 | {event_summary["watchlist_enter"]} |')
    lines.append(f'| 观察名单释放 | {event_summary["watchlist_release"]} |')
    lines.append(f'| 炸板开始 | {event_summary["break_episode_start"]} |')
    lines.append(f'| 炸板结束 | {event_summary["break_episode_end"]} |')
    lines.append(f'| 黑名单加入 | {event_summary["blacklist_enter"]} |')
    lines.append('')

    if event_summary['buy_types']:
        lines.append('### 买入类型分布\n')
        lines.append('| 类型 | 次数 |')
        lines.append('|------|------|')
        for buy_type, count in sorted(event_summary['buy_types'].items()):
            lines.append(f'| {buy_type} | {count} |')
        lines.append('')

    if event_summary['sell_remarks']:
        lines.append('### 卖出触发分布\n')
        lines.append('| 备注 | 次数 |')
        lines.append('|------|------|')
        for remark, count in sorted(event_summary['sell_remarks'].items()):
            lines.append(f'| {remark} | {count} |')
        lines.append('')

    lines.append('## 三、交易明细\n')
    lines.append('| 股票 | 买入价 | 卖出价 | 数量 | 盈亏% | 盈亏额 | 买入时间 | 卖出时间 |')
    lines.append('|------|--------|--------|------|-------|--------|---------|---------|')
    for pair in pairs:
        stock = pair['stock_code']
        bp = f"{pair['buy_price']:.2f}" if pair['buy_price'] else 'N/A'
        sp = f"{pair['sell_price']:.2f}" if pair['sell_price'] else '未平仓'
        vol = pair['volume']
        pnl_p = f"{pair['pnl_pct']:.2f}%" if pair['pnl_pct'] is not None else 'N/A'
        pnl_a = f"{pair['pnl_amount']:.0f}" if pair['pnl_amount'] is not None else 'N/A'
        bt = pair.get('buy_time', '')[:19] if pair.get('buy_time') else ''
        st = pair.get('sell_time', '')[:19] if pair.get('sell_time') else ''
        lines.append(f'| {stock} | {bp} | {sp} | {vol} | {pnl_p} | {pnl_a} | {bt} | {st} |')

    lines.append('')
    _format_reason_block(lines, '## 四、买入原因汇总\n', pairs, 'buy_reason')
    _format_reason_block(lines, '## 五、卖出触发汇总\n', pairs, 'sell_trigger')

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='打板策略每日复盘')
    parser.add_argument('--date', type=str, default=date.today().strftime('%Y%m%d'),
                        help='复盘日期，格式 YYYYMMDD')
    args = parser.parse_args()
    date_str = _normalize_date_str(args.date)

    print(f'正在复盘 {date_str} ...')

    logs = load_trade_logs(date_str)
    events = load_trade_events(date_str)
    # Match against the complete fill ledger through the target date.  Only
    # loading today's fills would lose the cost basis of normal T+1 exits.
    ledger = load_trade_ledger(date_str)
    pairs = select_daily_pairs(match_buy_sell_pairs(ledger), date_str)
    if not logs and not events and not pairs:
        print(f'{date_str} 无当日活动或未平仓成交，跳过复盘')
        return

    print(f'加载了 {len(logs)} 条当日成交记录')
    print(f'加载了 {len(ledger)} 条截至当日成交账本记录')
    print(f'加载了 {len(events)} 条事件记录')
    print(f'匹配了 {len(pairs)} 笔交易（含未平仓）')

    report = generate_report(date_str, logs, events, pairs)

    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f'daily_{date_str}.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f'复盘报告已保存: {report_path}')

    completed = [p for p in pairs if p.get('pnl_pct') is not None]
    if completed:
        wins = [p for p in completed if p['pnl_pct'] > 0]
        win_rate = len(wins) / len(completed) * 100
        total_pnl = sum(p['pnl_amount'] for p in completed)
        print('\n=== 核心指标 ===')
        print(f'完成交易: {len(completed)} 笔')
        print(f'胜率: {win_rate:.1f}%')
        print(f'总盈亏: {total_pnl:.2f} 元')


if __name__ == '__main__':
    main()
