"""
打板策略每日自动复盘脚本（U8升级）

功能：
1. 读取当日所有结构化交易日志
2. 计算每笔交易的盈亏（匹配买卖配对）
3. 统计当日核心指标：胜率、盈亏比、最大单笔亏损
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


def load_trade_logs(date_str: str) -> list:
    """加载指定日期的所有交易日志"""
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


def match_buy_sell_pairs(logs: list) -> list:
    """匹配买卖配对"""
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
            # 简单匹配：按时间顺序配对
            for i, buy in enumerate(buy_list):
                if i < len(sell_list):
                    sell = sell_list[i]
                    buy_price = float(buy.get('price', 0))
                    sell_price = float(sell.get('price', 0))
                    if buy_price > 0:
                        pnl_pct = (sell_price - buy_price) / buy_price * 100
                        pnl_amount = (sell_price - buy_price) * int(buy.get('volume', 0))
                    else:
                        pnl_pct = 0
                        pnl_amount = 0
                    pairs.append({
                        'stock_code': code,
                        'buy_price': buy_price,
                        'sell_price': sell_price,
                        'volume': int(buy.get('volume', 0)),
                        'pnl_pct': pnl_pct,
                        'pnl_amount': pnl_amount,
                        'buy_time': buy.get('timestamp', ''),
                        'sell_time': sell.get('timestamp', ''),
                        'buy_reason': buy.get('buy_reason', ''),
                        'sell_trigger': sell.get('sell_trigger', ''),
                    })
                else:
                    # 未平仓
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


def generate_report(date_str: str, logs: list, pairs: list) -> str:
    """生成 Markdown 复盘报告"""
    lines = []
    lines.append(f'# 打板策略日度复盘 — {date_str}\n')
    lines.append(f'> 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')

    # 统计
    buy_count = sum(1 for l in logs if l.get('action') == 'BUY')
    sell_count = sum(1 for l in logs if l.get('action') == 'SELL')
    completed = [p for p in pairs if p.get('pnl_pct') is not None]
    wins = [p for p in completed if p['pnl_pct'] > 0]
    losses = [p for p in completed if p['pnl_pct'] <= 0]

    lines.append('## 一、核心指标\n')
    lines.append(f'| 指标 | 数值 |')
    lines.append(f'|------|------|')
    lines.append(f'| 买入次数 | {buy_count} |')
    lines.append(f'| 卖出次数 | {sell_count} |')
    lines.append(f'| 完成交易笔数 | {len(completed)} |')

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
        lines.append(f'| 胜率 | N/A（无完成交易） |')

    lines.append('')

    # 交易明细
    lines.append('## 二、交易明细\n')
    lines.append('| 股票 | 买入价 | 卖出价 | 数量 | 盈亏% | 盈亏额 | 买入时间 | 卖出时间 |')
    lines.append('|------|--------|--------|------|-------|--------|---------|---------|')
    for p in pairs:
        stock = p['stock_code']
        bp = f"{p['buy_price']:.2f}" if p['buy_price'] else 'N/A'
        sp = f"{p['sell_price']:.2f}" if p['sell_price'] else '未平仓'
        vol = p['volume']
        pnl_p = f"{p['pnl_pct']:.2f}%" if p['pnl_pct'] is not None else 'N/A'
        pnl_a = f"{p['pnl_amount']:.0f}" if p['pnl_amount'] is not None else 'N/A'
        bt = p.get('buy_time', '')[:19] if p.get('buy_time') else ''
        st = p.get('sell_time', '')[:19] if p.get('sell_time') else ''
        lines.append(f'| {stock} | {bp} | {sp} | {vol} | {pnl_p} | {pnl_a} | {bt} | {st} |')

    lines.append('')

    # 买入原因分析
    lines.append('## 三、买入原因汇总\n')
    for p in pairs:
        if p.get('buy_reason'):
            lines.append(f'### {p["stock_code"]}')
            lines.append(f'```')
            lines.append(p['buy_reason'][:500])
            lines.append(f'```\n')

    # 卖出触发分析
    lines.append('## 四、卖出触发汇总\n')
    for p in pairs:
        if p.get('sell_trigger'):
            lines.append(f'### {p["stock_code"]}')
            lines.append(f'```')
            lines.append(p['sell_trigger'][:500])
            lines.append(f'```\n')

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='打板策略每日复盘')
    parser.add_argument('--date', type=str, default=date.today().strftime('%Y%m%d'),
                        help='复盘日期，格式 YYYYMMDD')
    args = parser.parse_args()
    date_str = args.date

    print(f'正在复盘 {date_str} ...')

    # 加载交易日志
    logs = load_trade_logs(date_str)
    if not logs:
        print(f'{date_str} 无交易日志，跳过复盘')
        return

    print(f'加载了 {len(logs)} 条交易记录')

    # 匹配买卖配对
    pairs = match_buy_sell_pairs(logs)
    print(f'匹配了 {len(pairs)} 笔交易（含未平仓）')

    # 生成报告
    report = generate_report(date_str, logs, pairs)

    # 保存报告
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f'daily_{date_str}.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f'复盘报告已保存: {report_path}')

    # 打印核心指标摘要
    completed = [p for p in pairs if p.get('pnl_pct') is not None]
    if completed:
        wins = [p for p in completed if p['pnl_pct'] > 0]
        win_rate = len(wins) / len(completed) * 100
        total_pnl = sum(p['pnl_amount'] for p in completed)
        print(f'\n=== 核心指标 ===')
        print(f'完成交易: {len(completed)} 笔')
        print(f'胜率: {win_rate:.1f}%')
        print(f'总盈亏: {total_pnl:.2f} 元')


if __name__ == '__main__':
    main()
