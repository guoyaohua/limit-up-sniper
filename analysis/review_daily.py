"""
打板策略每日自动复盘脚本（U9升级）

功能：
1. 读取当日结构化交易日志与事件流
2. 计算买卖配对与盈亏
3. 统计当日核心指标与决策漏斗
4. 输出 Markdown 格式的日度复盘报告

用法：
    python review_daily.py [--date YYYYMMDD]
"""

import argparse
import json
import os
import glob
from datetime import datetime, date
from collections import defaultdict


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADE_LOG_DIR = os.path.join(ROOT_DIR, 'output', 'trade_logs')
REPORT_DIR = os.path.join(ROOT_DIR, 'output', 'review_reports')
EVENT_LOG_FILENAME = 'events.jsonl'


def load_trade_logs(date_str: str) -> list:
    """加载指定日期的所有交易日志。"""
    log_dir = os.path.join(TRADE_LOG_DIR, date_str)
    if not os.path.exists(log_dir):
        print(f'交易日志目录不存在: {log_dir}')
        return []

    logs = []
    for filepath in sorted(glob.glob(os.path.join(log_dir, 'trade_*.json'))):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                logs.append(json.load(f))
        except Exception as e:
            print(f'读取文件失败 {filepath}: {e}')
    return logs


def load_trade_events(date_str: str) -> list:
    """加载指定日期的所有结构化事件。"""
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
    """匹配买卖配对。"""
    buys = defaultdict(list)
    sells = defaultdict(list)

    for log in logs:
        code = log.get('stock_code', '')
        if log.get('action') == 'BUY':
            buys[code].append(log)
        elif log.get('action') == 'SELL':
            sells[code].append(log)

    pairs = []
    for code in set(list(buys.keys()) + list(sells.keys())):
        buy_list = buys.get(code, [])
        sell_list = sells.get(code, [])

        if buy_list and sell_list:
            for i, buy in enumerate(buy_list):
                if i < len(sell_list):
                    sell = sell_list[i]
                    buy_price = float(buy.get('price', 0))
                    sell_price = float(sell.get('price', 0))
                    buy_volume = int(buy.get('volume', 0))
                    sell_volume = int(sell.get('volume', buy_volume))
                    matched_volume = min(buy_volume, sell_volume) if sell_volume > 0 else buy_volume
                    if buy_price > 0 and sell_price > 0:
                        pnl_pct = (sell_price - buy_price) / buy_price * 100
                        pnl_amount = (sell_price - buy_price) * matched_volume
                    else:
                        pnl_pct = None
                        pnl_amount = None
                    pairs.append({
                        'stock_code': code,
                        'buy_price': buy_price,
                        'sell_price': sell_price,
                        'volume': matched_volume,
                        'pnl_pct': pnl_pct,
                        'pnl_amount': pnl_amount,
                        'buy_time': buy.get('timestamp', ''),
                        'sell_time': sell.get('timestamp', ''),
                        'buy_reason': buy.get('buy_reason', ''),
                        'sell_trigger': sell.get('sell_trigger', ''),
                    })
                else:
                    pairs.append({
                        'stock_code': code,
                        'buy_price': float(buy.get('price', 0)),
                        'sell_price': None,
                        'volume': int(buy.get('volume', 0)),
                        'pnl_pct': None,
                        'pnl_amount': None,
                        'buy_time': buy.get('timestamp', ''),
                        'sell_time': None,
                        'buy_reason': buy.get('buy_reason', ''),
                        'sell_trigger': '',
                        'status': '未平仓',
                    })
        elif buy_list:
            for buy in buy_list:
                pairs.append({
                    'stock_code': code,
                    'buy_price': float(buy.get('price', 0)),
                    'sell_price': None,
                    'volume': int(buy.get('volume', 0)),
                    'pnl_pct': None,
                    'pnl_amount': None,
                    'buy_time': buy.get('timestamp', ''),
                    'sell_time': None,
                    'buy_reason': buy.get('buy_reason', ''),
                    'sell_trigger': '',
                    'status': '未平仓',
                })
        elif sell_list:
            for sell in sell_list:
                pairs.append({
                    'stock_code': code,
                    'buy_price': None,
                    'sell_price': float(sell.get('price', 0)),
                    'volume': int(sell.get('volume', 0)),
                    'pnl_pct': None,
                    'pnl_amount': None,
                    'buy_time': None,
                    'sell_time': sell.get('timestamp', ''),
                    'buy_reason': '',
                    'sell_trigger': sell.get('sell_trigger', ''),
                    'status': '盘前持仓卖出',
                })

    return pairs


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
    date_str = args.date

    print(f'正在复盘 {date_str} ...')

    logs = load_trade_logs(date_str)
    events = load_trade_events(date_str)
    if not logs and not events:
        print(f'{date_str} 无交易日志和事件日志，跳过复盘')
        return

    print(f'加载了 {len(logs)} 条交易记录')
    print(f'加载了 {len(events)} 条事件记录')

    pairs = match_buy_sell_pairs(logs)
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
