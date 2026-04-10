"""
monitor/dashboard.py - HTML 仪表盘邮件报告

从 打板策略_v2.4.py 提取的市场情绪汇总邮件生成函数。
"""

import json
import traceback
from datetime import datetime
from multiprocessing import Value

from loguru import logger

from config import VERSION, DEBUG_MODE, STRATEGY_NAME
from infra.common_enums import (OrderType, OrderStatus,
                                StockLimitStatusInt, StockOrderStatusInt)
from infra.data_helpers import is_trading_time, _conv_time
from infra.utils import send_email, send_html_email


def log_market_sentiment_summary_email(shared_data):
    """记录市场情绪汇总信息并发送详细邮件通知"""
    try:
        if not is_trading_time():
            logger.debug("当前不在交易时间，跳过市场情绪汇总记录")
            return

        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # ======================== 1. 市场情绪数据 ========================
        with shared_data['市场情绪_涨停板数量'].get_lock():
            limit_up_count = shared_data['市场情绪_涨停板数量'].value
        with shared_data['市场情绪_炸板数量'].get_lock():
            break_count = shared_data['市场情绪_炸板数量'].value
        with shared_data['市场情绪_炸板率'].get_lock():
            break_rate = shared_data['市场情绪_炸板率'].value
        with shared_data['市场情绪_昨日首板连板率'].get_lock():
            yesterday_first_rate = shared_data['市场情绪_昨日首板连板率'].value
        with shared_data['市场情绪_昨日涨停连板率'].get_lock():
            yesterday_limit_rate = shared_data['市场情绪_昨日涨停连板率'].value
        with shared_data['市场情绪_昨日首板表现'].get_lock():
            yesterday_first_perf = shared_data['市场情绪_昨日首板表现'].value
        with shared_data['市场情绪_昨日涨停表现'].get_lock():
            yesterday_limit_perf = shared_data['市场情绪_昨日涨停表现'].value
        with shared_data['市场情绪_评分'].get_lock():
            market_score = shared_data['市场情绪_评分'].value

        # 大盘指数
        with shared_data.get('上证指数涨跌幅', Value('d', 0.0)).get_lock():
            sh_index = shared_data.get('上证指数涨跌幅', Value('d', 0.0)).value
        with shared_data.get('沪深300涨跌幅', Value('d', 0.0)).get_lock():
            hs300_index = shared_data.get('沪深300涨跌幅', Value('d', 0.0)).value
        with shared_data.get('创业板指涨跌幅', Value('d', 0.0)).get_lock():
            cyb_index = shared_data.get('创业板指涨跌幅', Value('d', 0.0)).value
        with shared_data.get('深证成指涨跌幅', Value('d', 0.0)).get_lock():
            sz_index = shared_data.get('深证成指涨跌幅', Value('d', 0.0)).value

        # 大盘指数更新时间
        market_index_update_str = '<span style="color: #ff9800;">未知</span>'
        if '大盘指数更新时间' in shared_data:
            with shared_data['大盘指数更新时间'].get_lock():
                market_index_timestamp = shared_data['大盘指数更新时间'].value
            if market_index_timestamp > 0:
                market_index_update_time = datetime.fromtimestamp(
                    market_index_timestamp)
                time_diff = datetime.now() - market_index_update_time
                minutes_ago = int(time_diff.total_seconds() / 60)
                seconds_ago = int(time_diff.total_seconds() % 60)
                if minutes_ago > 0:
                    time_ago_str = f"{minutes_ago}分{seconds_ago}秒前"
                else:
                    time_ago_str = f"{seconds_ago}秒前"
                market_index_update_str = f"{market_index_update_time.strftime('%Y-%m-%d %H:%M:%S')} ({time_ago_str})"

        # 昨日涨停表现更新时间
        yesterday_limit_update_str = '<span style="color: #ff9800;">未知</span>'
        if '昨日涨停表现更新时间' in shared_data:
            with shared_data['昨日涨停表现更新时间'].get_lock():
                yesterday_limit_timestamp = shared_data['昨日涨停表现更新时间'].value
            if yesterday_limit_timestamp > 0:
                yesterday_limit_update_time = datetime.fromtimestamp(
                    yesterday_limit_timestamp)
                time_diff = datetime.now() - yesterday_limit_update_time
                minutes_ago = int(time_diff.total_seconds() / 60)
                seconds_ago = int(time_diff.total_seconds() % 60)
                if minutes_ago > 0:
                    time_ago_str = f"{minutes_ago}分{seconds_ago}秒前"
                else:
                    time_ago_str = f"{seconds_ago}秒前"
                yesterday_limit_update_str = f"{yesterday_limit_update_time.strftime('%Y-%m-%d %H:%M:%S')} ({time_ago_str})"

        # ======================== 2. 持仓状态 ========================
        positions_info = []
        total_market_value = 0
        total_profit = 0
        for stock_code, position_json in shared_data['持仓状态'].items():
            try:
                position = json.loads(position_json)
                stock_info = shared_data['股票信息'].get(stock_code, {})
                stock_name = stock_info.get('股票名称', '未知')

                # 计算持仓盈亏
                cost_price = position.get('成本价', 0)
                hold_quantity = position.get('持仓数量', 0)
                market_value = position.get('市值', cost_price * hold_quantity)
                current_price = market_value / hold_quantity if hold_quantity > 0 else 0
                profit_amount = market_value - (cost_price * hold_quantity)
                profit_rate = (current_price - cost_price
                               ) / cost_price * 100 if cost_price > 0 else 0

                total_market_value += market_value
                total_profit += profit_amount

                positions_info.append({
                    '股票代码': stock_code,
                    '股票名称': stock_name,
                    '持仓数量': hold_quantity,
                    '可用数量': position.get('可用数量', 0),
                    '成本价': cost_price,
                    '最新价': current_price,
                    '盈亏率': profit_rate,
                    '盈亏金额': profit_amount,
                    '市值': market_value,
                    '昨夜拥股': position.get('昨夜拥股', 0),
                })
            except Exception as e:
                logger.error(f"解析持仓信息失败 {stock_code}: {e}")

        # ======================== 3. 委托状态 ========================
        orders_info = []
        for stock_code, order_json in shared_data['委托状态'].items():
            try:
                orders = json.loads(order_json)
                stock_info = shared_data['股票信息'].get(stock_code, {})
                stock_name = stock_info.get('股票名称', '未知')

                for order in orders:
                    # 根据委托类型转换为买卖方向
                    order_type = order.get('委托类型', '')
                    try:
                        order_direction = OrderType(order_type).name
                    except ValueError:
                        order_direction = '未知'

                    # 委托状态
                    order_status = order.get('委托状态', '')
                    try:
                        order_status = OrderStatus(order_status).name
                    except ValueError:
                        order_status = '未知'

                    order_time = order.get('报单时间', '')
                    order_time = _conv_time(
                        int(order_time) *
                        1000, fmt='%H:%M:%S') if order_time else ''

                    orders_info.append({
                        '股票代码': stock_code,
                        '股票名称': stock_name,
                        '委托方向': order_direction,
                        '委托价格': order.get('委托价格', 0),
                        '委托数量': order.get('委托数量', 0),
                        '委托状态': order_status,
                        '成交数量': order.get('成交数量', 0),
                        '成交均价': order.get('成交均价', 0),
                        '委托时间': order_time,
                    })
            except Exception as e:
                logger.error(f"解析委托信息失败 {stock_code}: {e}")

        # ======================== 4. 涨停池分析 ========================
        limit_up_details = []
        for stock_code, time_str in shared_data['涨停池'].items():
            try:
                stock_info = shared_data['股票信息'].get(stock_code, {})
                stock_name = stock_info.get('股票名称', '未知')
                times = time_str.rstrip(',').split(',')
                first_time = _conv_time(int(times[0]),
                                        fmt='%H:%M:%S') if times else ''

                # 获取炸板信息
                break_times = []
                stock_break_count = shared_data.get('开板次数',
                                                    {}).get(stock_code, 0)
                if stock_code in shared_data['炸板池']:
                    break_time_str = shared_data['炸板池'][stock_code]
                    break_times = [
                        _conv_time(int(t), fmt='%H:%M:%S')
                        for t in break_time_str.rstrip(',').split(',') if t
                    ]

                # 获取最大开板回封时间
                max_rebound_time = shared_data.get('最大开板回封时间',
                                                   {}).get(stock_code, 0)

                # 获取股票状态
                if stock_code in shared_data['股票状态信号']:
                    stock_signals = shared_data['股票状态信号'][stock_code]

                    # 读取股票状态
                    with stock_signals['股票状态'].get_lock():
                        stock_status_value = stock_signals['股票状态'].value
                    # 转换为字符串显示
                    status_mapping = {
                        StockLimitStatusInt.NOT_LIMIT_UP: '未涨停',
                        StockLimitStatusInt.LIMIT_UP: '涨停',
                        StockLimitStatusInt.LIMIT_UP_BROKEN: '炸板',
                        StockLimitStatusInt.LIMIT_UP_REBOUND: '回封',
                    }
                    stock_status = status_mapping.get(stock_status_value, '未知')

                    # 读取下单状态
                    with stock_signals['下单状态'].get_lock():
                        order_status_value = stock_signals['下单状态'].value
                    # 转换为字符串显示
                    order_status_mapping = {
                        StockOrderStatusInt.NOT_ORDERED: '未下单',
                        StockOrderStatusInt.ORDERED_BUY: '已下单买入',
                        StockOrderStatusInt.ORDERED_SELL: '已下单卖出',
                        StockOrderStatusInt.CANCELLED: '已撤单',
                        StockOrderStatusInt.POSITION_HOLDING: '持仓中',
                        StockOrderStatusInt.PARTIALLY_FILLED: '部分成交',
                    }
                    order_status = order_status_mapping.get(
                        order_status_value, '未知')

                    # 读取封单金额
                    with stock_signals['封单金额'].get_lock():
                        limit_amount = stock_signals['封单金额'].value

                    # 读取封单金额变化率
                    with stock_signals['封单金额变化率'].get_lock():
                        limit_amount_change = stock_signals['封单金额变化率'].value
                else:
                    # 如果股票不在信号字典中，使用默认值
                    stock_status = '未知'
                    order_status = '未知'
                    limit_amount = 0
                    limit_amount_change = 0

                # 计算当天涨停次数（通过涨停池中的时间戳数量）
                limit_up_count_today = len(times)

                limit_up_details.append({
                    '股票代码':
                    stock_code,
                    '股票名称':
                    stock_name,
                    '是否黑名单':
                    True
                    if stock_code in shared_data.get('黑名单', {}) else False,
                    '首次涨停时间':
                    first_time,
                    '涨停次数':
                    limit_up_count_today,
                    '当前状态':
                    stock_status,
                    '下单状态':
                    order_status,
                    '封单金额':
                    f"{limit_amount / 10000:.2f}万"
                    if limit_amount > 0 else '无',
                    '封单金额变化率':
                    f"{limit_amount_change:.2%}"
                    if limit_amount_change else '无',
                    '炸板次数':
                    stock_break_count,
                    '炸板时间':
                    ', '.join(break_times) if break_times else '无',
                    '最大开板回封时间':
                    f"{max_rebound_time}秒" if max_rebound_time > 0 else '无'
                })
            except Exception as e:
                logger.error(f"解析涨停池信息失败 {stock_code}: {e}")

        # 按首次涨停时间排序
        limit_up_details.sort(key=lambda x: x['首次涨停时间'])

        # ======================== 5. 板块效应分析 ========================
        # 概念板块TOP10
        concept_sectors_data = {}
        for stock_code, sector_json in shared_data['概念板块效应'].items():
            try:
                # 板块效应数据是JSON格式的dataframe记录
                sectors = json.loads(sector_json)
                for sector in sectors:
                    sector_name = sector.get('板块名称', '')
                    sector_code = sector.get('板块代码', '')

                    if sector_name not in concept_sectors_data:
                        concept_sectors_data[sector_name] = {
                            '板块代码': sector_code,
                            '涨跌幅': sector.get('涨跌幅', 0),
                            '上涨家数': sector.get('上涨家数', 0),
                            '下跌家数': sector.get('下跌家数', 0),
                            '涨停家数': sector.get('涨停家数', 0),
                            '领涨股票': sector.get('领涨股票代码', ''),
                            '相关股票': []
                        }
                        concept_sectors_data[sector_name][
                            '相关股票'] = shared_data['概念板块成分股'].get(
                                sector_code, [])
            except Exception as e:
                logger.error(f"解析概念板块效应失败 {stock_code}: {e}")

        # 行业板块TOP10
        industry_sectors_data = {}
        for stock_code, sector_json in shared_data['行业板块效应'].items():
            try:
                sectors = json.loads(sector_json)
                for sector in sectors:
                    sector_name = sector.get('板块名称', '')
                    sector_code = sector.get('板块代码', '')

                    if sector_name not in industry_sectors_data:
                        industry_sectors_data[sector_name] = {
                            '板块代码': sector_code,
                            '涨跌幅': sector.get('涨跌幅', 0),
                            '上涨家数': sector.get('上涨家数', 0),
                            '下跌家数': sector.get('下跌家数', 0),
                            '涨停家数': sector.get('涨停家数', 0),
                            '领涨股票': sector.get('领涨股票代码', ''),
                            '相关股票': []
                        }
                        industry_sectors_data[sector_name][
                            '相关股票'] = shared_data['行业板块成分股'].get(
                                sector_code, [])
            except Exception as e:
                logger.error(f"解析行业板块效应失败 {stock_code}: {e}")

        # 排序取TOP10
        top_concept_sectors = sorted(concept_sectors_data.items(),
                                     key=lambda x: x[1]['涨跌幅'],
                                     reverse=True)[:10]

        top_industry_sectors = sorted(industry_sectors_data.items(),
                                      key=lambda x: x[1]['涨跌幅'],
                                      reverse=True)[:10]

        # ======================== 6. 黑名单详情 ========================
        blacklist_details = []
        for stock_code, reason in shared_data.get('黑名单', {}).items():
            stock_info = shared_data['股票信息'].get(stock_code, {})
            stock_name = stock_info.get('股票名称', '未知')
            blacklist_details.append({
                '股票代码': stock_code,
                '股票名称': stock_name,
                '加入原因': reason
            })

        # ======================== 6b. 观察名单详情 ========================
        watchlist_details = []
        for stock_code, entry in shared_data.get('观察名单', {}).items():
            stock_info = shared_data['股票信息'].get(stock_code, {})
            stock_name = stock_info.get('股票名称', '未知')
            watchlist_details.append({
                '股票代码': stock_code,
                '股票名称': stock_name,
                '信息': entry
            })

        # ======================== 构建邮件内容 ========================
        # 根据市场情绪评分确定背景色
        sentiment_color = '#4CAF50' if market_score >= 7 else '#FFC107' if market_score >= 4 else '#F44336'

        email_content = f"""
<html>
<head>
    <style>
        body {{
            font-family: 'Microsoft YaHei', Arial, sans-serif;
            line-height: 1.6;
            background-color: #f5f5f5;
            margin: 0;
            padding: 20px;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background-color: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #333;
            text-align: center;
            margin-bottom: 30px;
            font-size: 28px;
        }}
        h2 {{
            color: #333;
            border-bottom: 3px solid {sentiment_color};
            padding-bottom: 8px;
            margin-top: 30px;
            font-size: 20px;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 15px 0;
            font-size: 14px;
        }}
        th, td {{
            border: 1px solid #ddd;
            padding: 10px;
            text-align: left;
        }}
        th {{
            background-color: {sentiment_color};
            color: white;
            font-weight: bold;
        }}
        tr:nth-child(even) {{
            background-color: #f9f9f9;
        }}
        tr:hover {{
            background-color: #f5f5f5;
        }}
        .positive {{
            color: #d32f2f;
            font-weight: bold;
        }}
        .negative {{
            color: #388e3c;
            font-weight: bold;
        }}
        .neutral {{
            color: #666;
        }}
        .summary {{
            background-color: #e8f4f8;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            border-left: 5px solid {sentiment_color};
        }}
        .section {{
            margin: 25px 0;
            background-color: #fafafa;
            padding: 20px;
            border-radius: 8px;
        }}
        .metric-box {{
            display: inline-block;
            background-color: white;
            padding: 15px 25px;
            margin: 10px;
            border-radius: 5px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .metric-value {{
            font-size: 24px;
            font-weight: bold;
            color: {sentiment_color};
        }}
        .metric-label {{
            font-size: 14px;
            color: #666;
            margin-top: 5px;
        }}
        .score-box {{
            display: inline-block;
            padding: 10px 20px;
            border-radius: 25px;
            font-size: 18px;
            font-weight: bold;
            color: white;
            background-color: {sentiment_color};
        }}
        .scrollable-table {{
            max-height: 400px;
            overflow-y: auto;
            overflow-x: auto;
            border: 1px solid #ddd;
            margin: 15px 0;
            display: block;
        }}
        .scrollable-table table {{
            margin: 0;
            border: none;
            width: 100%;
            table-layout: fixed;
        }}
        .scrollable-table th {{
            position: sticky;
            top: 0;
            z-index: 10;
            background-color: {sentiment_color};
            white-space: nowrap;
        }}
        .scrollable-table td {{
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .scrollable-table td:nth-child(8) {{
            white-space: normal;
            word-wrap: break-word;
        }}
        .info-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 15px;
            margin: 15px 0;
        }}
        .info-item {{
            background-color: white;
            padding: 10px;
            border-radius: 5px;
            border-left: 3px solid {sentiment_color};
        }}
        .warning {{
            background-color: #fff3cd;
            border-color: #ffc107;
            color: #856404;
            padding: 10px;
            border-radius: 5px;
            margin: 10px 0;
        }}
        .footer {{
            text-align: center;
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            color: #666;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🎯 打板策略运行报告</h1>
        <p style="text-align: center; color: #666; margin-top: -20px;">{current_time}</p>

        <div class="summary">
            <h2 style="margin-top: 0;">📊 市场情绪总览</h2>
            <div style="text-align: center;">
                <div class="metric-box">
                    <div class="score-box">{market_score:.1f} 分</div>
                    <div class="metric-label">市场情绪评分</div>
                </div>
                <div class="metric-box">
                    <div class="metric-value">{limit_up_count}</div>
                    <div class="metric-label">涨停板数量</div>
                </div>
                <div class="metric-box">
                    <div class="metric-value">{break_count}</div>
                    <div class="metric-label">炸板数量</div>
                </div>
                <div class="metric-box">
                    <div class="metric-value">{break_rate:.1%}</div>
                    <div class="metric-label">炸板率</div>
                </div>
            </div>

            <div class="info-grid" style="margin-top: 20px;">
                <div class="info-item">
                    <strong>昨日首板连板率：</strong>{yesterday_first_rate:.1%}
                </div>
                <div class="info-item">
                    <strong>昨日涨停连板率：</strong>{yesterday_limit_rate:.1%}
                </div>
                <div class="info-item">
                    <strong>昨日首板表现：</strong>
                    <span class="{'positive' if yesterday_first_perf > 0 else 'negative'}">{yesterday_first_perf:+.2f}%</span>
                </div>
                <div class="info-item">
                    <strong>昨日涨停表现：</strong>
                    <span class="{'positive' if yesterday_limit_perf > 0 else 'negative'}">{yesterday_limit_perf:+.2f}%</span>
                </div>
            </div>
            <p style="color: #666; font-size: 12px; margin-top: 10px;">
                昨日涨停表现数据更新时间: {yesterday_limit_update_str}
            </p>
        </div>

        <div class="section">
            <h2>📈 大盘指数表现</h2>
            <p style="color: #666; font-size: 12px; margin-bottom: 10px;">
                数据更新时间: {market_index_update_str}
            </p>
            <table>
                <tr>
                    <th style="width: 25%;">指数名称</th>
                    <th style="width: 25%;">涨跌幅</th>
                    <th style="width: 50%;">涨跌幅图示</th>
                </tr>
                <tr>
                    <td><strong>上证指数</strong></td>
                    <td class="{'positive' if sh_index > 0 else 'negative' if sh_index < 0 else 'neutral'}">{sh_index:+.2f}%</td>
                    <td>
                        <div style="background-color: #e0e0e0; height: 20px; position: relative;">
                            <div style="background-color: {'#d32f2f' if sh_index > 0 else '#388e3c'};
                                        height: 100%;
                                        width: {min(abs(sh_index) * 20, 100)}%;
                                        {'float: right;' if sh_index < 0 else ''}">
                            </div>
                        </div>
                    </td>
                </tr>
                <tr>
                    <td><strong>沪深300</strong></td>
                    <td class="{'positive' if hs300_index > 0 else 'negative' if hs300_index < 0 else 'neutral'}">{hs300_index:+.2f}%</td>
                    <td>
                        <div style="background-color: #e0e0e0; height: 20px;">
                            <div style="background-color: {'#d32f2f' if hs300_index > 0 else '#388e3c'};
                                        height: 100%;
                                        width: {min(abs(hs300_index) * 20, 100)}%;
                                        {'float: right;' if hs300_index < 0 else ''}">
                            </div>
                        </div>
                    </td>
                </tr>
                <tr>
                    <td><strong>创业板指</strong></td>
                    <td class="{'positive' if cyb_index > 0 else 'negative' if cyb_index < 0 else 'neutral'}">{cyb_index:+.2f}%</td>
                    <td>
                        <div style="background-color: #e0e0e0; height: 20px;">
                            <div style="background-color: {'#d32f2f' if cyb_index > 0 else '#388e3c'};
                                        height: 100%;
                                        width: {min(abs(cyb_index) * 20, 100)}%;
                                        {'float: right;' if cyb_index < 0 else ''}">
                            </div>
                        </div>
                    </td>
                </tr>
                <tr>
                    <td><strong>深证成指</strong></td>
                    <td class="{'positive' if sz_index > 0 else 'negative' if sz_index < 0 else 'neutral'}">{sz_index:+.2f}%</td>
                    <td>
                        <div style="background-color: #e0e0e0; height: 20px;">
                            <div style="background-color: {'#d32f2f' if sz_index > 0 else '#388e3c'};
                                        height: 100%;
                                        width: {min(abs(sz_index) * 20, 100)}%;
                                        {'float: right;' if sz_index < 0 else ''}">
                            </div>
                        </div>
                    </td>
                </tr>
            </table>
        </div>

        <div class="section">
            <h2>💼 持仓状态</h2>
            <div style="margin-bottom: 15px;">
                <span class="metric-box" style="margin-left: 0;">
                    <div class="metric-value">{len(positions_info)}</div>
                    <div class="metric-label">持仓数量</div>
                </span>
                <span class="metric-box">
                    <div class="metric-value">¥{total_market_value:,.0f}</div>
                    <div class="metric-label">总市值</div>
                </span>
                <span class="metric-box">
                    <div class="metric-value {'positive' if total_profit > 0 else 'negative'}">
                        ¥{total_profit:+,.0f}
                    </div>
                    <div class="metric-label">总盈亏</div>
                </span>
            </div>
            {'<table><tr><th>股票</th><th>持仓/可用</th><th>成本价</th><th>最新价</th><th>盈亏率</th><th>盈亏金额</th><th>市值</th></tr>' if positions_info else '<p class="warning">当前无持仓</p>'}
            {''.join([f'<tr><td><strong>{p["股票代码"]}</strong><br/>{p["股票名称"]}</td><td>{p["持仓数量"]}/{p["可用数量"]}</td><td>{p["成本价"]:.2f}</td><td>{p["最新价"]:.2f}</td><td class="{"positive" if p["盈亏率"] > 0 else "negative" if p["盈亏率"] < 0 else "neutral"}">{p["盈亏率"]:+.2f}%</td><td class="{"positive" if p["盈亏金额"] > 0 else "negative" if p["盈亏金额"] < 0 else "neutral"}">{p["盈亏金额"]:+,.0f}</td><td>{p["市值"]:,.0f}</td></tr>' for p in positions_info])}
            {'</table>' if positions_info else ''}
        </div>

        <div class="section">
            <h2>📋 委托状态</h2>
            <div style="margin-bottom: 15px;">
                <span class="metric-box" style="margin-left: 0;">
                    <div class="metric-value">{len(orders_info)}</div>
                    <div class="metric-label">委托笔数</div>
                </span>
            </div>
            {'<table><tr><th>股票</th><th>方向</th><th>价格</th><th>数量</th><th>时间</th><th>状态</th></tr>' if orders_info else '<p class="warning">当前无委托</p>'}
            {''.join([f'<tr><td><strong>{o["股票代码"]}</strong><br/>{o["股票名称"]}</td><td>{o["委托方向"]}</td><td>{o["委托价格"]:.2f}</td><td>{o["委托数量"]}</td><td>{o["委托时间"]}</td><td>{o["委托状态"]}</td></tr>' for o in orders_info])}
            {'</table>' if orders_info else ''}
        </div>

        <div class="section">
            <h2>🚀 涨停池详情</h2>
            <div style="margin-bottom: 15px;">
                <span class="metric-box" style="margin-left: 0;">
                    <div class="metric-value">{len(limit_up_details)}</div>
                    <div class="metric-label">涨停池股票数</div>
                </span>
            </div>
            """

        # 构建涨停池表格
        if limit_up_details:
            limit_up_table_rows = []
            for d in limit_up_details:
                bg_style = 'background-color: #ffebee;' if d["是否黑名单"] else ''
                blacklist_tag = '<br/><span style="color: red; font-size: 12px;">黑名单</span>' if d[
                    "是否黑名单"] else ''

                row = f'''<tr style="{bg_style}">
                    <td><strong>{d["股票代码"]}</strong><br/>{d["股票名称"]}{blacklist_tag}</td>
                    <td>{d["首次涨停时间"]}</td>
                    <td style="text-align:center;">{d["涨停次数"]}</td>
                    <td>{d["当前状态"]}</td>
                    <td>{d["下单状态"]}</td>
                    <td style="text-align:right;">{d["封单金额"]}</td>
                    <td style="text-align:center;">{d["炸板次数"]}</td>
                    <td style="max-width: 200px; word-wrap: break-word;">{d["炸板时间"]}</td>
                    <td>{d["最大开板回封时间"]}</td>
                </tr>'''
                limit_up_table_rows.append(row)

            email_content += f'''
            <div class="scrollable-table">
                <table>
                    <tr>
                        <th style="width: 120px;">股票</th>
                        <th style="width: 80px;">首次涨停</th>
                        <th style="width: 60px;">涨停次数</th>
                        <th style="width: 80px;">当前状态</th>
                        <th style="width: 80px;">下单状态</th>
                        <th style="width: 80px;">封单金额</th>
                        <th style="width: 60px;">炸板次数</th>
                        <th style="width: 150px; max-width: 200px;">炸板时间</th>
                        <th style="width: 100px;">最大回封时间</th>
                    </tr>
                    {''.join(limit_up_table_rows)}
                </table>
            </div>
            '''
        else:
            email_content += '<p class="warning">当前涨停池为空</p>'

        email_content += """
        </div>
        """

        # 构建概念板块表格
        email_content += '<div class="section"><h2>🏷️ TOP10 概念板块</h2>'

        # 添加概念板块数据更新时间
        concept_sector_timestamp_value = shared_data.get('概念板块更新时间')
        if concept_sector_timestamp_value:
            with concept_sector_timestamp_value.get_lock():
                concept_sector_timestamp = concept_sector_timestamp_value.value if concept_sector_timestamp_value.value > 0 else None
        else:
            concept_sector_timestamp = None

        logger.debug(f"概念板块更新时间: {concept_sector_timestamp}")

        if concept_sector_timestamp:
            try:
                update_time = datetime.fromtimestamp(concept_sector_timestamp)
                time_diff = datetime.now() - update_time
                minutes_ago = int(time_diff.total_seconds() / 60)
                seconds_ago = int(time_diff.total_seconds() % 60)

                if minutes_ago > 0:
                    time_ago_str = f"{minutes_ago}分{seconds_ago}秒前"
                else:
                    time_ago_str = f"{seconds_ago}秒前"

                email_content += f'<p style="color: #666; margin-bottom: 10px;">数据更新时间: {update_time.strftime("%Y-%m-%d %H:%M:%S")} ({time_ago_str}) | 按涨停家数和涨跌幅排序</p>'
            except Exception as e:
                logger.error(f"解析概念板块时间戳失败: {e}")
                email_content += '<p style="color: #666; margin-bottom: 10px;">按涨停家数和涨跌幅排序的热门概念板块 (数据更新时间未知)</p>'
        else:
            email_content += '<p style="color: #666; margin-bottom: 10px;">按涨停家数和涨跌幅排序的热门概念板块 <span style="color: #ff9800;">(数据更新时间未知)</span></p>'

        if top_concept_sectors:
            email_content += '<table><tr><th width="60">排名</th><th>板块名称</th><th>涨跌幅</th><th>涨停数</th><th>上涨/下跌</th><th>领涨股票</th><th>相关股票数</th></tr>'
            for i, sector in enumerate(top_concept_sectors):
                change_class = "positive" if sector[1][
                    "涨跌幅"] > 0 else "negative" if sector[1][
                        "涨跌幅"] < 0 else "neutral"
                email_content += f'''<tr>
                    <td style="text-align:center;"><strong>{i+1}</strong></td>
                    <td>{sector[0]}</td>
                    <td class="{change_class}">{sector[1]["涨跌幅"]:+.2f}%</td>
                    <td style="text-align:center;"><strong>{sector[1]["涨停家数"]}</strong></td>
                    <td style="text-align:center;">{sector[1]["上涨家数"]}/{sector[1]["下跌家数"]}</td>
                    <td>{sector[1]["领涨股票"]}</td>
                    <td style="text-align:center;">{len(sector[1]["相关股票"])}</td>
                </tr>'''
            email_content += '</table>'
        else:
            email_content += '<p class="warning">暂无概念板块数据</p>'
        email_content += '</div>'

        # 构建行业板块表格
        email_content += '<div class="section"><h2>🏭 TOP10 行业板块</h2>'

        # 添加行业板块数据更新时间
        industry_sector_timestamp_value = shared_data.get('行业板块更新时间')
        if industry_sector_timestamp_value:
            with industry_sector_timestamp_value.get_lock():
                industry_sector_timestamp = industry_sector_timestamp_value.value if industry_sector_timestamp_value.value > 0 else None
        else:
            industry_sector_timestamp = None

        logger.debug(f"行业板块更新时间: {industry_sector_timestamp}")

        if industry_sector_timestamp:
            try:
                update_time = datetime.fromtimestamp(industry_sector_timestamp)
                time_diff = datetime.now() - update_time
                minutes_ago = int(time_diff.total_seconds() / 60)
                seconds_ago = int(time_diff.total_seconds() % 60)

                if minutes_ago > 0:
                    time_ago_str = f"{minutes_ago}分{seconds_ago}秒前"
                else:
                    time_ago_str = f"{seconds_ago}秒前"

                email_content += f'<p style="color: #666; margin-bottom: 10px;">数据更新时间: {update_time.strftime("%Y-%m-%d %H:%M:%S")} ({time_ago_str}) | 按涨停家数和涨跌幅排序</p>'
            except Exception as e:
                logger.error(f"解析行业板块时间戳失败: {e}")
                email_content += '<p style="color: #666; margin-bottom: 10px;">按涨停家数和涨跌幅排序的热门行业板块 (数据更新时间未知)</p>'
        else:
            email_content += '<p style="color: #666; margin-bottom: 10px;">按涨停家数和涨跌幅排序的热门行业板块 <span style="color: #ff9800;">(数据更新时间未知)</span></p>'

        if top_industry_sectors:
            email_content += '<table><tr><th width="60">排名</th><th>板块名称</th><th>涨跌幅</th><th>涨停数</th><th>上涨/下跌</th><th>领涨股票</th><th>相关股票数</th></tr>'
            for i, sector in enumerate(top_industry_sectors):
                change_class = "positive" if sector[1][
                    "涨跌幅"] > 0 else "negative" if sector[1][
                        "涨跌幅"] < 0 else "neutral"
                email_content += f'''<tr>
                    <td style="text-align:center;"><strong>{i+1}</strong></td>
                    <td>{sector[0]}</td>
                    <td class="{change_class}">{sector[1]["涨跌幅"]:+.2f}%</td>
                    <td style="text-align:center;"><strong>{sector[1]["涨停家数"]}</strong></td>
                    <td style="text-align:center;">{sector[1]["上涨家数"]}/{sector[1]["下跌家数"]}</td>
                    <td>{sector[1]["领涨股票"]}</td>
                    <td style="text-align:center;">{len(sector[1]["相关股票"])}</td>
                </tr>'''
            email_content += '</table>'
        else:
            email_content += '<p class="warning">暂无行业板块数据</p>'
        email_content += '</div>'

        # ======================== 7. TOP 20 个股资金流入 ========================
        stock_capital_inflow = []

        # 获取个股资金流入更新时间
        stock_capital_inflow_timestamp_value = shared_data.get('个股资金流入更新时间')
        if stock_capital_inflow_timestamp_value:
            with stock_capital_inflow_timestamp_value.get_lock():
                stock_capital_inflow_timestamp = stock_capital_inflow_timestamp_value.value if stock_capital_inflow_timestamp_value.value > 0 else None
        else:
            stock_capital_inflow_timestamp = None

        # 从原始数据中提取TOP 20
        if '个股资金流入_原始数据' in shared_data and shared_data['个股资金流入_原始数据']:
            try:
                raw_data = shared_data['个股资金流入_原始数据']
                # 按主力净流入排序并取前20
                top_inflow_data = sorted(raw_data,
                                         key=lambda x: x.get('主力净流入', 0),
                                         reverse=True)[:20]

                # 对TOP20按涨跌幅降序排序
                sorted_data = sorted(top_inflow_data,
                                     key=lambda x: x.get('涨跌幅', 0),
                                     reverse=True)

                for idx, item in enumerate(sorted_data, 1):
                    stock_code = item.get('股票代码', '')
                    stock_capital_inflow.append({
                        '排名':
                        idx,
                        '股票代码':
                        stock_code,
                        '股票名称':
                        item.get('股票名称', '未知'),
                        '资金流入':
                        item.get('主力净流入', 0),
                        '资金流入占比':
                        item.get('主力净流入占比', 0),
                        '涨跌幅':
                        item.get('涨跌幅', 0),
                        '成交额':
                        item.get('成交额', 0),
                    })
            except Exception as e:
                logger.error(f"解析个股资金流入数据失败: {e}")

        # 构建个股资金流入表格
        email_content += '<div class="section"><h2>💰 TOP 20 个股资金流入</h2>'

        # 添加数据更新时间信息
        logger.debug(f"个股资金流入更新时间: {stock_capital_inflow_timestamp}")

        if stock_capital_inflow_timestamp:
            try:
                update_time = datetime.fromtimestamp(
                    stock_capital_inflow_timestamp)
                time_diff = datetime.now() - update_time
                minutes_ago = int(time_diff.total_seconds() / 60)
                seconds_ago = int(time_diff.total_seconds() % 60)

                if minutes_ago > 0:
                    time_ago_str = f"{minutes_ago}分{seconds_ago}秒前"
                else:
                    time_ago_str = f"{seconds_ago}秒前"

                email_content += f'<p style="color: #666; margin-bottom: 10px;">数据更新时间: {update_time.strftime("%Y-%m-%d %H:%M:%S")} ({time_ago_str})</p>'
            except Exception as e:
                logger.error(f"解析资金流入时间戳失败: {e}")
                email_content += '<p style="color: #666; margin-bottom: 10px;"><span style="color: #ff9800;">数据更新时间未知</span></p>'
        else:
            email_content += '<p style="color: #666; margin-bottom: 10px;"><span style="color: #ff9800;">数据更新时间未知</span></p>'

        if stock_capital_inflow:
            email_content += '<table><tr><th width="60">排名</th><th>股票代码</th><th>股票名称</th><th>主力净流入</th><th>流入占比</th><th>涨跌幅</th><th>成交额</th></tr>'
            for item in stock_capital_inflow:
                inflow_class = "positive" if item[
                    "资金流入"] > 0 else "negative" if item[
                        "资金流入"] < 0 else "neutral"
                change_class = "positive" if item[
                    "涨跌幅"] > 0 else "negative" if item["涨跌幅"] < 0 else "neutral"

                # 格式化资金流入（转换为亿元）
                # 原始数据单位为万元，转换为亿元需除以10000
                inflow_yi = item["资金流入"] / 10000
                chengjiaoer_yi = item["成交额"] / 10000

                email_content += f'''<tr>
                    <td style="text-align:center;"><strong>{item["排名"]}</strong></td>
                    <td>{item["股票代码"]}</td>
                    <td>{item["股票名称"]}</td>
                    <td class="{inflow_class}" style="text-align:right;">{inflow_yi:+.2f}亿</td>
                    <td style="text-align:right;">{item["资金流入占比"]:.2f}%</td>
                    <td class="{change_class}" style="text-align:right;">{item["涨跌幅"]:+.2f}%</td>
                    <td style="text-align:right;">{chengjiaoer_yi:.2f}亿</td>
                </tr>'''
            email_content += '</table>'
        else:
            email_content += '<p class="warning">暂无个股资金流入数据</p>'
        email_content += '</div>'

        # 构建黑名单表格
        if blacklist_details:
            email_content += '<div class="section"><h2>🚫 黑名单详情</h2><table><tr><th>股票</th><th>加入原因</th></tr>'
            for b in blacklist_details:
                email_content += f'<tr><td><strong>{b["股票代码"]}</strong><br/>{b["股票名称"]}</td><td>{b["加入原因"]}</td></tr>'
            email_content += '</table></div>'

        # 获取撤单次数（使用锁）
        cancel_count_value = 0
        if '撤单次数' in shared_data:
            with shared_data['撤单次数'].get_lock():
                cancel_count_value = shared_data['撤单次数'].value

        email_content += f"""

        <div class="section">
            <h2>📊 策略运行统计</h2>
            <div class="info-grid">
                <div class="info-item">
                    <strong>撤单次数：</strong>{cancel_count_value} 次
                </div>
                <div class="info-item">
                    <strong>黑名单股票数：</strong>{len(shared_data.get('黑名单', {}))} 只
                </div>
                <div class="info-item">
                    <strong>观察名单股票数：</strong>{len(shared_data.get('观察名单', {}))} 只
                </div>
                <div class="info-item">
                    <strong>强势股票池数量：</strong>{len(shared_data.get('强势股票', []))} 只
                </div>
                <div class="info-item">
                    <strong>策略版本：</strong>{VERSION}
                </div>
                <div class="info-item">
                    <strong>调试模式：</strong>{'开启' if DEBUG_MODE else '关闭'}
                </div>
                <div class="info-item">
                    <strong>策略名称：</strong>{STRATEGY_NAME}
                </div>
            </div>
        </div>
        """

        # 构建策略建议部分
        sentiment_bg_color = '#e8f5e9' if market_score >= 7 else '#fff8e1' if market_score >= 4 else '#ffebee'
        sentiment_text = (
            '极强'
            if market_score >= 8 else '强势' if market_score >= 7 else '中性偏强'
            if market_score >= 6 else '中性' if market_score >= 5 else '中性偏弱'
            if market_score >= 4 else '弱势' if market_score >= 3 else '极弱')

        operation_advice = (
            '市场情绪高涨，可积极扫板参与涨停' if market_score >= 8 else '市场情绪良好，适度参与优质涨停标的'
            if market_score >= 7 else '市场情绪一般，谨慎选择强势股参与' if market_score >= 5
            else '市场情绪偏弱，控制仓位，优选防御' if market_score >= 3 else '市场情绪极差，建议空仓观望')

        position_advice = ('可满仓操作' if market_score >= 8 else
                           '仓位控制在70-80%' if market_score >= 7 else
                           '仓位控制在50-60%' if market_score >= 5 else
                           '仓位控制在30%以下' if market_score >= 3 else '空仓或极低仓位')

        risk_warning = ('市场过热，注意追高风险' if market_score >= 8 else
                        '保持理性，设好止损' if market_score >= 6 else
                        '严格止损，降低预期' if market_score >= 4 else '市场风险较大，保护本金为主')

        email_content += f"""
        <div class="section">
            <h2>🎯 策略建议</h2>
            <div style="background-color: {sentiment_bg_color}; padding: 20px; border-radius: 8px;">
                <h3 style="margin-top: 0; color: {sentiment_color};">当前市场情绪：{sentiment_text}</h3>
                <ul style="margin: 10px 0;">
                    <li><strong>操作建议：</strong>{operation_advice}</li>
                    <li><strong>仓位建议：</strong>{position_advice}</li>
                    <li><strong>风险提示：</strong>{risk_warning}</li>
                </ul>
            </div>
        </div>

        <div class="footer">
            <p>本报告由打板策略自动生成 | 生成时间：{current_time}</p>
            <p>注意：本报告仅供参考，不构成投资建议</p>
        </div>
    </div>
</body>
</html>
"""

        # 构建日志摘要
        summary_msg = (
            f"【{current_time}】市场情绪汇总 - "
            f"评分:{market_score:.1f}, 涨停:{limit_up_count}只, 炸板:{break_count}只({break_rate:.1%}), "
            f"持仓:{len(positions_info)}只(盈亏:{total_profit:+.0f}), "
            f"委托:{len(orders_info)}笔")

        logger.info(summary_msg)

        # 发送详细邮件
        send_html_email("打板策略运行报告", email_content)

    except Exception as e:
        logger.exception(f"【关键错误】记录市场情绪汇总信息发生错误: {e}")
        send_email("【关键错误】记录市场情绪汇总信息发生错误",
                   f"记录市场情绪汇总信息发生错误: {e}\n{traceback.format_exc()}")
