"""
市场情绪报告生成模块

功能说明：
1. 生成市场情绪汇总报告（静态HTML邮件 + 动态网页）
2. 保存历史数据到JSON文件
3. 提供交互式网页查看历史数据
4. 支持时间滑块查看不同时间点的数据
"""
import sys
import json
import os
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
from multiprocessing import Value
import numpy as np
from decimal import Decimal
import ftplib
import traceback
from infra.utils import send_email
from loguru import logger
from infra.common_enums import StockLimitStatus, StockOrderStatus, OrderType, OrderStatus, StockLimitStatusInt, StockOrderStatusInt


class MarketSentimentReporter:
    """市场情绪报告生成器"""
    def __init__(self,
                 base_dir: str = "reports",
                 folder_name: str = 'default',
                 remote_dir: str = "htdocs/reports",
                 debug_mode: bool = False,
                 ftp_host: str = None,
                 ftp_username: str = None,
                 ftp_password: str = None):
        """
        初始化报告生成器
        
        Args:
            base_dir: 报告保存的基础目录
            debug_mode: 是否为调试模式
            ftp_host: FTP服务器地址
            ftp_username: FTP用户名
            ftp_password: FTP密码
        """
        self.base_dir = Path(base_dir)
        self.debug_mode = debug_mode
        self.templates_dir = self.base_dir / "templates"
        self.data_dir = self.base_dir / folder_name / "data"
        self.reports_dir = self.base_dir / folder_name
        self.remote_dir = remote_dir + '/' + folder_name

        # 创建必要的目录
        self._create_directories()

        # 停止时间配置
        self.STOP_TIME = dt_time(15, 00) if not debug_mode else dt_time(23, 59)

        # FTP同步配置
        self.ftp_sync = None
        if ftp_host and ftp_username and ftp_password:
            self.ftp_sync = FTPSyncUtility(ftp_host,
                                           ftp_username,
                                           ftp_password,
                                           remote_dir=self.remote_dir)

    def _create_directories(self):
        """创建必要的目录结构"""
        self.base_dir.mkdir(exist_ok=True)
        self.templates_dir.mkdir(exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def is_trading_time(self, now: Optional[datetime] = None) -> bool:
        """判断当前时间是否为交易时间"""
        if now is None:
            now = datetime.now()
        current_time = now.time()

        # 定义交易时间段
        morning_start = dt_time(9, 29)
        morning_end = dt_time(11, 31)
        afternoon_start = dt_time(12, 59)
        # 延长下午结束时间到15:05，确保能生成收盘后的最终报告
        afternoon_end = dt_time(15, 5) if not self.debug_mode else dt_time(23, 59)

        # 判断是否在交易时间内
        is_morning_session = morning_start <= current_time <= morning_end
        is_afternoon_session = afternoon_start <= current_time <= afternoon_end

        return is_morning_session or is_afternoon_session

    def _conv_time(self, ct: int, fmt: str = '%Y%m%d%H%M%S') -> str:
        """
        转换时间戳为字符串
        _conv_time(1476374400000) --> '20161014000000'
        """
        local_time = time.localtime(ct / 1000)
        data_head = time.strftime(fmt, local_time)
        return data_head

    def _extract_value(self, value: Any) -> Any:
        """从multiprocessing.Value对象中提取值"""
        return self._make_json_serializable(value)

    def _safe_extract_stock_signal_value(self, stock_signals: Dict[str, Any],
                                         key: str, default_value: Any) -> Any:
        """安全地从股票信号字典中提取Value类型的值"""
        value_obj = stock_signals.get(key, default_value)
        if hasattr(value_obj, 'get_lock'):
            with value_obj.get_lock():
                return value_obj.value
        return value_obj

    def _make_json_serializable(self, obj: Any) -> Any:
        """
        递归地将对象转换为JSON可序列化的格式
        处理multiprocessing.Value, Synchronized等对象
        """
        if obj is None:
            return None

        # 处理multiprocessing.Value和Synchronized对象
        if hasattr(obj, 'value') and hasattr(obj, 'get_lock'):
            # multiprocessing.Value对象，使用锁来安全访问
            with obj.get_lock():
                return self._make_json_serializable(obj.value)
        elif hasattr(obj, 'value'):
            # 其他有value属性的对象（如Synchronized）
            return self._make_json_serializable(obj.value)

        # 处理数字类型
        elif isinstance(obj, (int, float, bool)):
            return obj
        elif isinstance(obj, Decimal):
            return float(obj)
        elif isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.bool_):
            return bool(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()

        # 处理字符串
        elif isinstance(obj, str):
            return obj

        # 处理字典
        elif isinstance(obj, dict):
            return {
                key: self._make_json_serializable(value)
                for key, value in obj.items()
            }

        # 处理列表和元组
        elif isinstance(obj, (list, tuple)):
            return [self._make_json_serializable(item) for item in obj]

        # 处理枚举类型
        elif hasattr(obj, 'name') and hasattr(obj, 'value'):
            return obj.name if hasattr(obj, 'name') else str(obj)

        # 处理其他对象，尝试转换为字符串
        else:
            try:
                # 检查是否是类似Synchronized的对象
                if hasattr(obj, '__class__') and 'Synchronized' in str(
                        type(obj)):
                    # 尝试直接访问值
                    if hasattr(obj, '_value'):
                        return self._make_json_serializable(obj._value)
                    else:
                        return str(obj)
                return str(obj)
            except Exception:
                return str(type(obj))

    def _save_and_generate_daily_report(self, new_report_data: Dict[str, Any],
                                        now: datetime):
        """
        将新报告数据追加到日度数据文件，并重新生成完整的日度HTML报告。
        """
        date_str = now.strftime('%Y%m%d')
        daily_data_path = self.data_dir / f"{date_str}_reports.json"

        # 1. 读取或初始化日度数据
        daily_reports = []
        if daily_data_path.exists():
            try:
                with open(daily_data_path, 'r', encoding='utf-8') as f:
                    daily_reports = json.load(f)
            except json.JSONDecodeError:
                logger.warning(f"{daily_data_path} 文件损坏，将重新创建。")

        # 2. 追加新数据并排序
        daily_reports.append(new_report_data)
        # 使用get方法防止旧数据缺失timestamp导致KeyError
        daily_reports.sort(key=lambda x: x.get('timestamp', 0))

        # 3. 写回日度数据文件
        with open(daily_data_path, 'w', encoding='utf-8') as f:
            # 使用新的序列化方法确保所有数据都可以JSON序列化
            serializable_reports = self._make_json_serializable(daily_reports)
            json.dump(serializable_reports, f, ensure_ascii=False, indent=2)
        logger.info(f"日度报告数据已更新: {daily_data_path}")

        # 4. 加载HTML模板
        template_path = self.templates_dir / "market_sentiment_report.html"
        if template_path.exists():
            with open(template_path, 'r', encoding='utf-8') as f:
                template_html = f.read()

            # 5. 将所有日度数据嵌入模板
            placeholder = "/* REPORT_DATA_PLACEHOLDER */"
            embedded_data_script = f"<script>window.EMBEDDED_REPORT_DATA = {json.dumps(serializable_reports, ensure_ascii=False)};</script>"

            final_html = template_html.replace(placeholder,
                                               embedded_data_script)

            # 6. 保存最终的日度HTML报告
            output_report_path = self.reports_dir / f"daily_report_{date_str}.html"
            with open(output_report_path, 'w', encoding='utf-8') as f:
                f.write(final_html)
            logger.info(f"日度HTML报告已生成: {output_report_path}")

        # 7. 更新主索引页
        self._update_main_index_page()

        # 8. 同步到FTP服务器
        self._sync_to_ftp()

    def _update_main_index_page(self):
        """
        创建或更新 reports/index.html，列出所有可用的日度报告。
        """
        index_html_path = self.reports_dir / "index.html"
        report_files = sorted(self.reports_dir.glob("daily_report_*.html"),
                              reverse=True)

        links_html = ""
        if not report_files:
            links_html = "<p>暂无报告生成。</p>"
        else:
            for report_file in report_files:
                date_str = report_file.stem.split('_')[-1]
                formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                links_html += f'<li><a href="{report_file.name}">{formatted_date} 市场情绪报告</a></li>\n'

        main_index_content = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>打板策略 - 报告索引</title>
    <style>
        body {{ font-family: sans-serif; line-height: 1.6; padding: 2em; background: #f4f7f6; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 2em; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }}
        h1 {{ border-bottom: 2px solid #ddd; padding-bottom: 0.5em; }}
        ul {{ list-style: none; padding: 0; }}
        li {{ margin: 0.8em 0; }}
        a {{ text-decoration: none; color: #007bff; font-size: 1.1em; transition: color 0.2s; }}
        a:hover {{ color: #0056b3; text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📈 打板策略 - 报告索引</h1>
        <ul>
            {links_html}
        </ul>
    </div>
</body>
</html>
"""
        with open(index_html_path, 'w', encoding='utf-8') as f:
            f.write(main_index_content)
        logger.info(f"主索引页已更新: {index_html_path}")

    def _sync_to_ftp(self):
        """同步reports目录到FTP服务器"""
        if self.ftp_sync is None:
            logger.debug("FTP同步未配置，跳过同步")
            return

        try:
            logger.info("开始FTP同步...")
            success = self.ftp_sync.sync_reports_directory(
                str(self.reports_dir))
            if success:
                logger.info("FTP同步完成")
            else:
                logger.warning("FTP同步遇到错误")
        except Exception as e:
            logger.exception(f"FTP同步异常: {e}")

    def generate_report(self,
                        shared_data: Dict[str, Any],
                        now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        生成市场情绪报告
        
        Args:
            shared_data: 共享数据字典
            now: 指定报告的生成时间，用于测试。如果为None，则使用当前时间。
            
        Returns:
            报告数据字典
        """
        if now is None:
            now = datetime.now()

        if not self.is_trading_time(now=now) and not self.debug_mode:
            logger.debug("当前不在交易时间，跳过市场情绪汇总记录")
            return {}

        try:
            current_time = now
            current_time_str = current_time.strftime('%Y-%m-%d %H:%M:%S')

            # 提取市场情绪数据
            report_data = {
                'report_time': current_time_str,
                'timestamp': current_time.timestamp(),
                'market_sentiment':
                self._extract_market_sentiment(shared_data),
                'market_indices': self._extract_market_indices(shared_data),
                'positions': self._extract_positions(shared_data),
                'orders': self._extract_orders(shared_data),
                'limit_up_pool': self._extract_limit_up_pool(shared_data),
                'sector_analysis': self._extract_sector_analysis(shared_data),
                'blacklist': self._extract_blacklist(shared_data),
                'strategy_stats': self._extract_strategy_stats(shared_data),
                'strategy_advice': self._generate_strategy_advice(shared_data)
            }

            # 保存数据并生成日度报告
            self._save_and_generate_daily_report(report_data, now=current_time)

            logger.info(f"市场情绪报告生成成功: {current_time_str}")
            return report_data

        except Exception as e:
            logger.exception(f"生成市场情绪报告发生错误: {e}")
            return {}

    def _extract_market_sentiment(
            self, shared_data: Dict[str, Any]) -> Dict[str, Any]:
        """提取市场情绪数据"""
        return {
            'limit_up_count':
            self._extract_value(shared_data.get('市场情绪_涨停板数量', Value('i', 0))),
            'break_count':
            self._extract_value(shared_data.get('市场情绪_炸板数量', Value('i', 0))),
            'break_rate':
            self._extract_value(shared_data.get('市场情绪_炸板率', Value('d', 0.0))),
            'yesterday_first_rate':
            self._extract_value(
                shared_data.get('市场情绪_昨日首板连板率', Value('d', 0.0))),
            'yesterday_limit_rate':
            self._extract_value(
                shared_data.get('市场情绪_昨日涨停连板率', Value('d', 0.0))),
            'yesterday_first_perf':
            self._extract_value(shared_data.get('市场情绪_昨日首板表现', Value('d',
                                                                     0.0))),
            'yesterday_limit_perf':
            self._extract_value(shared_data.get('市场情绪_昨日涨停表现', Value('d',
                                                                     0.0))),
            'market_score':
            self._extract_value(shared_data.get('市场情绪_评分', Value('d', 5.0)))
        }

    def _extract_market_indices(self,
                                shared_data: Dict[str, Any]) -> Dict[str, Any]:
        """提取大盘指数数据"""
        return {
            'sh_index':
            self._extract_value(shared_data.get('上证指数涨跌幅', Value('d', 0.0))),
            'hs300_index':
            self._extract_value(shared_data.get('沪深300涨跌幅', Value('d', 0.0))),
            'cyb_index':
            self._extract_value(shared_data.get('创业板指涨跌幅', Value('d', 0.0))),
            'sz_index':
            self._extract_value(shared_data.get('深证成指涨跌幅', Value('d', 0.0)))
        }

    def _extract_positions(
            self, shared_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """提取持仓数据"""
        positions_info = []
        for stock_code, position_json in shared_data.get('持仓状态', {}).items():
            try:
                position = json.loads(position_json)
                stock_info = shared_data.get('股票信息', {}).get(stock_code, {})
                stock_name = stock_info.get('股票名称', '未知')

                cost_price = position.get('成本价', 0)
                hold_quantity = position.get('持仓数量', 0)
                market_value = position.get('市值', cost_price * hold_quantity)
                current_price = market_value / hold_quantity if hold_quantity > 0 else 0
                profit_amount = market_value - (cost_price * hold_quantity)
                profit_rate = (current_price - cost_price
                               ) / cost_price * 100 if cost_price > 0 else 0

                positions_info.append({
                    'stock_code':
                    stock_code,
                    'stock_name':
                    stock_name,
                    'hold_quantity':
                    hold_quantity,
                    'available_quantity':
                    position.get('可用数量', 0),
                    'cost_price':
                    cost_price,
                    'current_price':
                    current_price,
                    'profit_rate':
                    profit_rate,
                    'profit_amount':
                    profit_amount,
                    'market_value':
                    market_value,
                    'yesterday_hold':
                    position.get('昨夜拥股', 0),
                })
            except Exception as e:
                logger.exception(f"解析持仓信息失败 {stock_code}: {e}")

        return positions_info

    def _extract_orders(self, shared_data: Dict[str,
                                                Any]) -> List[Dict[str, Any]]:
        """提取委托数据"""
        orders_info = []
        for stock_code, order_json in shared_data.get('委托状态', {}).items():
            try:
                orders = json.loads(order_json)
                stock_info = shared_data.get('股票信息', {}).get(stock_code, {})
                stock_name = stock_info.get('股票名称', '未知')

                for order in orders:
                    order_type = order.get('委托类型', '')
                    try:
                        order_direction = OrderType(order_type).name
                    except:
                        order_direction = '未知'

                    order_status = order.get('委托状态', '')
                    try:
                        order_status_name = OrderStatus(order_status).name
                    except:
                        order_status_name = '未知'

                    order_time = order.get('报单时间', '')
                    order_time = self._conv_time(
                        int(order_time) *
                        1000, fmt='%H:%M:%S') if order_time else ''
                    orders_info.append({
                        'stock_code': stock_code,
                        'stock_name': stock_name,
                        'order_direction': order_direction,
                        'order_price': order.get('委托价格', 0),
                        'order_quantity': order.get('委托数量', 0),
                        'order_status': order_status_name,
                        'filled_quantity': order.get('成交数量', 0),
                        'avg_filled_price': order.get('成交均价', 0),
                        'order_time': order_time,
                    })
            except Exception as e:
                logger.exception(f"解析委托信息失败 {stock_code}: {e}")

        return orders_info

    def _extract_limit_up_pool(
            self, shared_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """提取涨停池数据"""
        limit_up_details = []
        for stock_code, time_str in shared_data.get('涨停池', {}).items():
            try:
                stock_info = shared_data.get('股票信息', {}).get(stock_code, {})
                stock_name = stock_info.get('股票名称', '未知')
                times = [t for t in time_str.rstrip(',').split(',') if t]
                first_time = self._conv_time(int(times[0]),
                                             fmt='%H:%M:%S') if times else ''

                break_times = []
                stock_break_count = shared_data.get('开板次数',
                                                    {}).get(stock_code, 0)
                if stock_code in shared_data.get('炸板池', {}):
                    break_time_str = shared_data['炸板池'][stock_code]
                    break_times = [
                        self._conv_time(int(t), fmt='%H:%M:%S')
                        for t in break_time_str.rstrip(',').split(',') if t
                    ]

                stock_signals = shared_data.get('股票状态信号',
                                                {}).get(stock_code, {})

                # 安全地读取股票状态（Value类型）
                stock_status_value = stock_signals.get('股票状态', '未知')
                if hasattr(stock_status_value, 'get_lock'):
                    with stock_status_value.get_lock():
                        stock_status = stock_status_value.value
                else:
                    stock_status = stock_status_value

                if isinstance(stock_status, StockLimitStatusInt):
                    stock_status = StockLimitStatus[stock_status.name].value
                elif isinstance(stock_status, str):
                    stock_status = stock_status.strip()
                elif isinstance(stock_status, int):
                    stock_status = StockLimitStatusInt(stock_status).name
                    stock_status = StockLimitStatus[stock_status].value
                else:
                    logger.error(
                        f"未知股票状态类型: {type(stock_status)}，值: {stock_status}")

                # 安全地读取下单状态（Value类型）
                order_status_value = stock_signals.get('下单状态', '未知')
                if hasattr(order_status_value, 'get_lock'):
                    with order_status_value.get_lock():
                        order_status = order_status_value.value
                else:
                    order_status = order_status_value

                if isinstance(order_status, StockOrderStatusInt):
                    order_status = StockOrderStatus[order_status.name].value
                elif isinstance(order_status, str):
                    order_status = order_status.strip()
                elif isinstance(order_status, int):
                    order_status = StockOrderStatusInt(order_status).name
                    order_status = StockOrderStatus[order_status].value
                else:
                    logger.error(
                        f"未知下单状态类型: {type(order_status)}，值: {order_status}")

                # 安全地读取封单金额（Value类型）
                limit_amount_value = stock_signals.get('封单金额', 0)
                if hasattr(limit_amount_value, 'get_lock'):
                    with limit_amount_value.get_lock():
                        limit_amount = limit_amount_value.value
                else:
                    limit_amount = limit_amount_value
                sectors = self._get_stock_sectors(stock_code, shared_data)

                limit_up_details.append({
                    'stock_code':
                    stock_code,
                    'stock_name':
                    stock_name,
                    'is_blacklist':
                    stock_code in shared_data.get('黑名单', {}),
                    'first_limit_time':
                    first_time,
                    'limit_count':
                    len(times),
                    'current_status':
                    stock_status,
                    'order_status':
                    order_status,
                    'limit_amount':
                    limit_amount,
                    'limit_amount_change':
                    self._safe_extract_stock_signal_value(
                        stock_signals, '封单金额变化率', 0),
                    'break_count':
                    stock_break_count,
                    'break_times':
                    break_times,
                    'max_rebound_time':
                    shared_data.get('最大开板回封时间', {}).get(stock_code, 0),
                    'sectors':
                    sectors
                })
            except Exception as e:
                logger.exception(f"解析涨停池信息失败 {stock_code}: {e}")

        limit_up_details.sort(key=lambda x: x['first_limit_time'])
        return limit_up_details

    def _get_stock_sectors(
            self, stock_code: str,
            shared_data: Dict[str, Any]) -> Dict[str, List[str]]:
        """获取股票所属板块信息"""
        sectors = {'concept': [], 'industry': []}

        concept_json = shared_data.get('概念板块效应', {}).get(stock_code, '[]')
        try:
            concept_sectors = json.loads(concept_json)
            sectors['concept'] = [
                s.get('板块名称', '') for s in concept_sectors[:3]
            ]
        except:
            pass

        industry_json = shared_data.get('行业板块效应', {}).get(stock_code, '[]')
        try:
            industry_sectors = json.loads(industry_json)
            sectors['industry'] = [
                s.get('板块名称', '') for s in industry_sectors[:3]
            ]
        except:
            pass

        return sectors

    def _extract_sector_analysis(
            self, shared_data: Dict[str, Any]) -> Dict[str, Any]:
        """提取板块分析数据"""
        concept_sectors_data = {}
        industry_sectors_data = {}

        # 概念板块分析
        for stock_code, sector_json in shared_data.get('概念板块效应', {}).items():
            try:
                sectors = json.loads(sector_json)
                for sector in sectors:
                    sector_name = sector.get('板块名称', '')
                    sector_code = sector.get('板块代码', '')
                    if sector_name and sector_name not in concept_sectors_data:
                        concept_sectors_data[sector_name] = {
                            'sector_code':
                            sector_code,
                            'change_rate':
                            sector.get('涨跌幅', 0),
                            'up_count':
                            sector.get('上涨家数', 0),
                            'down_count':
                            sector.get('下跌家数', 0),
                            'limit_up_count':
                            sector.get('涨停家数', 0),
                            'leading_stock':
                            sector.get('领涨股票代码', ''),
                            'stocks':
                            shared_data.get('概念板块成分股',
                                            {}).get(sector_code, [])[:10]
                        }
            except Exception as e:
                logger.exception(f"解析概念板块效应失败 {stock_code}: {e}")

        # 行业板块分析
        for stock_code, sector_json in shared_data.get('行业板块效应', {}).items():
            try:
                sectors = json.loads(sector_json)
                for sector in sectors:
                    sector_name = sector.get('板块名称', '')
                    sector_code = sector.get('板块代码', '')
                    if sector_name and sector_name not in industry_sectors_data:
                        industry_sectors_data[sector_name] = {
                            'sector_code':
                            sector_code,
                            'change_rate':
                            sector.get('涨跌幅', 0),
                            'up_count':
                            sector.get('上涨家数', 0),
                            'down_count':
                            sector.get('下跌家数', 0),
                            'limit_up_count':
                            sector.get('涨停家数', 0),
                            'leading_stock':
                            sector.get('领涨股票代码', ''),
                            'stocks':
                            shared_data.get('行业板块成分股',
                                            {}).get(sector_code, [])[:10]
                        }
            except Exception as e:
                logger.exception(f"解析行业板块效应失败 {stock_code}: {e}")

        # 排序取TOP10
        top_concept_sectors = sorted(
            concept_sectors_data.items(),
            key=lambda x: (x[1]['limit_up_count'], x[1]['change_rate']),
            reverse=True)[:10]

        top_industry_sectors = sorted(
            industry_sectors_data.items(),
            key=lambda x: (x[1]['limit_up_count'], x[1]['change_rate']),
            reverse=True)[:10]

        return {
            'concept_sectors': dict(top_concept_sectors),
            'industry_sectors': dict(top_industry_sectors)
        }

    def _extract_blacklist(
            self, shared_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """提取黑名单数据"""
        blacklist_details = []
        for stock_code, reason in shared_data.get('黑名单', {}).items():
            stock_info = shared_data.get('股票信息', {}).get(stock_code, {})
            stock_name = stock_info.get('股票名称', '未知')
            blacklist_details.append({
                'stock_code': stock_code,
                'stock_name': stock_name,
                'reason': reason
            })
        return blacklist_details

    def _extract_strategy_stats(self,
                                shared_data: Dict[str, Any]) -> Dict[str, Any]:
        """提取策略统计数据"""
        return {
            'cancel_count':
            self._extract_value(shared_data.get('撤单次数', Value('i', 0))),
            'blacklist_count':
            len(shared_data.get('黑名单', {})),
            'strong_stock_count':
            len(shared_data.get('强势股票', [])),
            'version':
            shared_data.get('VERSION', 'v1'),
            'debug_mode':
            shared_data.get('DEBUG_MODE', False),
            'strategy_name':
            shared_data.get('STRATEGY_NAME', '打板策略')
        }

    def _generate_strategy_advice(
            self, shared_data: Dict[str, Any]) -> Dict[str, Any]:
        """生成策略建议"""
        market_score = self._extract_value(
            shared_data.get('市场情绪_评分', Value('d', 5.0)))

        if market_score >= 8:
            sentiment = "极强"
            operation_advice = "市场情绪高涨，可积极扫板参与涨停"
            position_advice = "可满仓操作"
            risk_warning = "市场过热，注意追高风险"
        elif market_score >= 7:
            sentiment = "强势"
            operation_advice = "市场情绪良好，适度参与优质涨停标的"
            position_advice = "仓位控制在70-80%"
            risk_warning = "保持理性，设好止损"
        elif market_score >= 5:
            sentiment = "中性"
            operation_advice = "市场情绪一般，谨慎选择强势股参与"
            position_advice = "仓位控制在50-60%"
            risk_warning = "严格止损，降低预期"
        elif market_score >= 3:
            sentiment = "弱势"
            operation_advice = "市场情绪偏弱，控制仓位，优选防御"
            position_advice = "仓位控制在30%以下"
            risk_warning = "市场风险较大，保护本金为主"
        else:
            sentiment = "极弱"
            operation_advice = "市场情绪极差，建议空仓观望"
            position_advice = "空仓或极低仓位"
            risk_warning = "市场风险极大，耐心等待机会"

        return {
            'market_sentiment':
            sentiment,
            'operation_advice':
            operation_advice,
            'position_advice':
            position_advice,
            'risk_warning':
            risk_warning,
            'sentiment_color':
            '#4CAF50' if market_score >= 7 else
            '#FFC107' if market_score >= 4 else '#F44336'
        }


class FTPSyncUtility:
    """FTP同步工具类，用于将reports目录同步到FTP服务器"""
    def __init__(self,
                 host: str,
                 username: str,
                 password: str,
                 remote_dir: str = "reports"):
        """
        初始化FTP同步工具
        
        Args:
            host: FTP服务器地址
            username: FTP用户名
            password: FTP密码
            remote_dir: 远程目录路径
        """
        self.host = host
        self.username = username
        self.password = password
        self.remote_dir = remote_dir
        self.logger = logger

    def _get_file_size(self, file_path: Path) -> int:
        """获取文件大小"""
        try:
            return file_path.stat().st_size
        except Exception:
            return 0

    def _ensure_remote_dir(self, ftp_conn, remote_path: str):
        """确保远程目录存在，如果不存在则创建，支持'/'和'\'两种分隔符"""
        original_dir = None
        try:
            # 统一路径分隔符为'/'
            normalized_path = remote_path.replace('\\', '/').strip('/')
            if not normalized_path:
                return

            original_dir = ftp_conn.pwd()

            parts = normalized_path.split('/')
            for part in parts:
                if not part:
                    continue
                try:
                    ftp_conn.cwd(part)
                except ftplib.error_perm:
                    try:
                        ftp_conn.mkd(part)
                        self.logger.info(f"创建远程目录: {ftp_conn.pwd()}/{part}")
                        ftp_conn.cwd(part)
                    except ftplib.error_perm as e:
                        self.logger.exception(
                            f"创建或进入远程目录失败: {part}, error: {e}")
                        if original_dir:
                            ftp_conn.cwd(original_dir)
                        raise

            if original_dir:
                ftp_conn.cwd(original_dir)

        except Exception as e:
            self.logger.exception(f"确保远程目录失败 {remote_path}: {e}")
            try:
                if original_dir:
                    ftp_conn.cwd(original_dir)
            except Exception as restore_e:
                self.logger.error(f"恢复FTP原始目录失败: {restore_e}")
            raise

    def sync_reports_directory(self,
                               local_reports_dir: str = "reports") -> bool:
        """
        同步reports目录到FTP服务器
        
        Args:
            local_reports_dir: 本地reports目录路径
            
        Returns:
            True表示同步成功，False表示同步失败
        """
        local_dir = Path(local_reports_dir)
        if not local_dir.exists():
            self.logger.warning(f"本地reports目录不存在: {local_dir}")
            return False

        try:
            # 连接FTP服务器
            with ftplib.FTP_TLS(self.host) as ftp:
                ftp.login(self.username, self.password)
                self.logger.info(f"成功连接到FTP服务器: {self.host}")

                # 确保远程reports目录存在
                try:
                    ftp.cwd(self.remote_dir)
                    self.logger.info(
                        f"成功切换到远程目录: {self.remote_dir}, {ftp.pwd()}")
                except ftplib.error_perm:
                    # 目录不存在，创建它
                    ftp.mkd(self.remote_dir)
                    ftp.cwd(self.remote_dir)
                    self.logger.info(
                        f"创建并切换到远程目录: {self.remote_dir}, {ftp.pwd()}")

                # 统计信息
                uploaded_count = 0
                skipped_count = 0
                error_count = 0

                # 允许上传的文件扩展名
                allowed_extensions = {'.html'}

                # 遍历本地files
                for local_file in local_dir.rglob('*'):
                    if local_file.is_file():
                        # 计算相对路径
                        rel_path = local_file.relative_to(local_dir)
                        remote_filename = rel_path.as_posix()

                        # 检查文件扩展名
                        file_ext = local_file.suffix.lower()
                        if file_ext not in allowed_extensions:
                            self.logger.warning(
                                f"跳过禁止的文件类型 {file_ext}: {remote_filename}")
                            skipped_count += 1
                            continue

                        try:
                            # 检查是否需要上传
                            needs_upload = True
                            try:
                                # 简单检查：尝试获取远程文件大小
                                remote_size = ftp.size(remote_filename)
                                local_size = self._get_file_size(local_file)
                                if remote_size == local_size:
                                    needs_upload = False
                                    self.logger.debug(
                                        f"文件大小相同，跳过: {remote_filename}")
                            except (ftplib.error_perm, ftplib.error_temp):
                                # 远程文件不存在
                                needs_upload = True
                                self.logger.debug(
                                    f"远程文件不存在，需要上传: {remote_filename}")

                            if needs_upload:
                                # 确保远程目录存在
                                remote_dir = str(Path(remote_filename).parent)
                                if remote_dir != '.' and remote_dir != '':
                                    # 保存当前目录
                                    current_dir = ftp.pwd()
                                    try:
                                        # 确保子目录存在
                                        self._ensure_remote_dir(
                                            ftp, remote_dir)
                                        # 回到原目录
                                        ftp.cwd(current_dir)
                                    except Exception as dir_error:
                                        self.logger.exception(
                                            f"创建远程目录失败 {remote_dir}: {dir_error}"
                                        )
                                        # 回到原目录
                                        try:
                                            ftp.cwd(current_dir)
                                        except:
                                            pass
                                        raise dir_error

                                # 上传文件
                                with open(local_file, 'rb') as f:
                                    ftp.storbinary(f'STOR {remote_filename}',
                                                   f)
                                uploaded_count += 1
                                self.logger.info(f"上传文件: {remote_filename}")
                            else:
                                skipped_count += 1

                        except Exception as e:
                            if "Prohibited file extension" in str(e):
                                self.logger.warning(
                                    f"服务器禁止文件类型，跳过: {remote_filename}")
                                skipped_count += 1
                            else:
                                error_count += 1
                                self.logger.exception(
                                    f"上传文件失败 {rel_path}: {e}")

                # 记录同步结果
                self.logger.info(
                    f"FTP同步完成 - 上传: {uploaded_count}, 跳过: {skipped_count}, 错误: {error_count}"
                )
                return error_count == 0

        except Exception as e:
            self.logger.exception(f"FTP同步失败: {e}")
            return False


# 为了兼容原有代码，提供一个包装函数
def log_market_sentiment_summary(shared_data: Dict[str, Any],
                                 folder_name: str,
                                 now: Optional[datetime] = None):
    """
    记录市场情绪汇总信息并发送详细邮件通知
    
    这是对原有函数的包装，保持接口兼容性
    """
    try:
        # 从shared_data中提取必要的配置
        debug_mode = shared_data.get('DEBUG_MODE', False)

        # # FTP配置
        # ftp_host = '<redacted>'
        # ftp_username = '<redacted>'
        # ftp_password = '<redacted>'

        # # FTP配置 https://dash.infinityfree.com/
        # ftp_host = '<redacted>'
        # ftp_username = '<redacted>'
        # ftp_password = '<redacted>'

        # FTP配置 https://profreehost.com/
        # https://gyh168.liveblog365.com/
        # ftp_host = '<redacted>'
        # ftp_username = '<redacted>'
        # ftp_password = '<redacted>'

        # Azure FTPS 连接配置
        ftp_host = '<redacted>'  # 去掉协议前缀和路径
        ftp_username = '<redacted>'  # 用户名中的$需要转义
        ftp_password = '<redacted>'

        # 创建报告生成器实例，包含FTP配置
        reporter = MarketSentimentReporter(debug_mode=debug_mode,
                                           remote_dir="/site/wwwroot/reports",
                                           folder_name=folder_name,
                                           ftp_host=ftp_host,
                                           ftp_username=ftp_username,
                                           ftp_password=ftp_password)

        # 生成报告
        reporter.generate_report(shared_data, now=now)

    except Exception as e:
        # 记录详细的错误信息
        error_msg = f"市场情绪报告生成失败: {str(e)}"
        full_traceback = traceback.format_exc()
        logger.exception(error_msg)

        email_subject = "【错误通知】市场情绪报告生成失败"
        email_content = f"""
市场情绪报告生成过程中发生错误：

错误类型: {type(e).__name__}
错误信息: {str(e)}

完整堆栈跟踪:
{full_traceback}

请检查系统状态并及时处理。
"""
        send_email(email_subject, email_content)
