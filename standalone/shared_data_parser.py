"""
Shared Data Parser Module
用于解析策略共享数据的各种字段
（支持影子模式和实盘模式）
"""

import json
import traceback
from typing import Dict, List, Any, Optional, Union
from datetime import datetime
from loguru import logger


class SharedDataParser:
    """策略共享数据解析器"""
    
    @staticmethod
    def parse_stock_info(data: Dict) -> Dict:
        """
        解析股票信息
        
        Input format:
        {'600051.SH': {'涨停价': 7.98, '跌停价': 6.53, '流通股本': 310880000.0, ...}}
        """
        if not isinstance(data, dict):
            return {}

        # 直接返回，因为这个字段是标准dict格式
        return data

    @staticmethod
    def parse_positions(positions_raw: Any) -> Dict:
        """
        解析持仓状态
        
        Input format:
        {'_type_': 'Dict', '_value_': {'603728.SH': '{"证券代码": "603728.SH", "持仓数量": 1400, ...}'}}
        """
        try:
            # Check if it's the special format with _type_ and _value_
            if isinstance(
                    positions_raw, dict
            ) and '_type_' in positions_raw and '_value_' in positions_raw:
                actual_data = positions_raw['_value_']

                if isinstance(actual_data, dict):
                    result = {}
                    for stock_code, position_str in actual_data.items():
                        # Try to parse JSON string
                        if isinstance(position_str, str):
                            try:
                                position_dict = json.loads(position_str)
                                result[stock_code] = position_dict
                            except json.JSONDecodeError:
                                logger.warning(f"无法解析持仓JSON: {stock_code}")
                                result[stock_code] = position_str
                        else:
                            result[stock_code] = position_str
                    return result
                else:
                    return actual_data if isinstance(actual_data, dict) else {}
            else:
                # Regular format, return as is
                return positions_raw if isinstance(positions_raw, dict) else {}
        except Exception as e:
            logger.error(f"解析持仓数据失败: {e}")
            return {}

    @staticmethod
    def parse_orders(orders_raw: Any) -> Dict:
        """
        解析委托状态
        
        Input format:
        {'_type_': 'Dict', '_value_': {'301000.SZ': '[{"委托类型": 23, "证券代码": "301000.SZ", ...}]'}}
        """
        try:
            if isinstance(
                    orders_raw, dict
            ) and '_type_' in orders_raw and '_value_' in orders_raw:
                actual_data = orders_raw['_value_']

                if isinstance(actual_data, dict):
                    result = {}
                    for stock_code, orders_str in actual_data.items():
                        if isinstance(orders_str, str):
                            try:
                                orders_list = json.loads(orders_str)
                                result[stock_code] = orders_list
                            except json.JSONDecodeError:
                                logger.warning(f"无法解析委托JSON: {stock_code}")
                                result[stock_code] = orders_str
                        else:
                            result[stock_code] = orders_str
                    return result
                else:
                    return actual_data if isinstance(actual_data, dict) else {}
            else:
                return orders_raw if isinstance(orders_raw, dict) else {}
        except Exception as e:
            logger.error(f"解析委托数据失败: {e}")
            return {}

    @staticmethod
    def parse_stock_list(data: Any) -> List[str]:
        """
        解析股票列表（昨日首板股票、昨日涨停股票、强势股票等）
        
        Input format:
        ['600103.SH', '600367.SH', '300619.SZ', ...]
        """
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def parse_timestamp_dict(data: Any) -> Dict[str, List[datetime]]:
        """
        解析时间戳字典（涨停池、炸板池）
        
        Input format:
        {'_type_': 'Dict', '_value_': {'002150.SZ': '1757295000000,1757295453000,...'}}
        """
        try:
            result = {}

            if isinstance(data,
                          dict) and '_type_' in data and '_value_' in data:
                actual_data = data['_value_']

                if isinstance(actual_data, dict):
                    for stock_code, timestamp_str in actual_data.items():
                        if isinstance(timestamp_str, str):
                            # Parse comma-separated timestamps
                            timestamps = []
                            for ts_str in timestamp_str.split(','):
                                if ts_str.strip():
                                    try:
                                        # Convert millisecond timestamp to datetime
                                        ts = int(ts_str.strip())
                                        dt = datetime.fromtimestamp(ts / 1000)
                                        timestamps.append(dt)
                                    except (ValueError, OSError) as e:
                                        logger.debug(f"无法解析时间戳 {ts_str}: {e}")

                            if timestamps:
                                result[stock_code] = timestamps
                        elif isinstance(timestamp_str, list):
                            result[stock_code] = timestamp_str
                    return result
                else:
                    return actual_data if isinstance(actual_data, dict) else {}
            else:
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"解析时间戳字典失败: {e}")
            return {}

    @staticmethod
    def parse_number_dict(data: Any) -> Dict[str, Union[int, float]]:
        """
        解析数字字典（最大开板回封时间、开板次数）
        
        Input format:
        {'_type_': 'Dict', '_value_': {'002516.SZ': 114, '003036.SZ': 201, ...}}
        """
        try:
            if isinstance(data,
                          dict) and '_type_' in data and '_value_' in data:
                actual_data = data['_value_']
                return actual_data if isinstance(actual_data, dict) else {}
            else:
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"解析数字字典失败: {e}")
            return {}

    @staticmethod
    def parse_stock_signals(data: Any) -> Dict[str, Dict]:
        """
        解析股票状态信号
        
        Input format:
        {'600051.SH': {'股票状态': {'_type_': 'multiprocessing.Value', '_value_': 1, '_typecode_': 'l'}, ...}}
        """
        try:
            result = {}

            if not isinstance(data, dict):
                return {}

            for stock_code, signals in data.items():
                if not isinstance(signals, dict):
                    continue

                stock_result = {}
                for signal_name, signal_value in signals.items():
                    # Extract value from multiprocessing.Value format
                    if isinstance(signal_value,
                                  dict) and '_value_' in signal_value:
                        stock_result[signal_name] = signal_value['_value_']
                    else:
                        stock_result[signal_name] = signal_value

                result[stock_code] = stock_result

            return result
        except Exception as e:
            logger.error(f"解析股票状态信号失败: {e}")
            return {}

    @staticmethod
    def parse_blacklist(data: Any) -> Dict[str, str]:
        """
        解析黑名单，提取简洁的黑名单原因作为Tag
        
        Input format:
        {'_type_': 'Dict', '_value_': {'002251.SZ': '[黑名单] 002251.SZ 步步高 开板后股价下跌超过3%...'}}
        
        Output format:
        {'002251.SZ': '开板后下跌'}
        """
        try:
            result = {}

            # Extract actual data
            if isinstance(data,
                          dict) and '_type_' in data and '_value_' in data:
                actual_data = data['_value_']
            elif isinstance(data, dict):
                actual_data = data
            else:
                return {}

            # Process each blacklist entry
            for stock_code, blacklist_msg in actual_data.items():
                if not blacklist_msg:
                    continue

                # Extract reason tag from the message
                reason_tag = "未知原因"

                if isinstance(blacklist_msg, str):
                    # Check for different blacklist patterns and extract concise tags
                    if "换手率" in blacklist_msg:
                        reason_tag = "换手率过高"
                    elif "开板次数过多" in blacklist_msg:
                        reason_tag = "开板次数过多"
                    elif "开板时间过长" in blacklist_msg:
                        reason_tag = "开板时间过长"
                    elif "开板后股价下跌" in blacklist_msg or "开板后下跌" in blacklist_msg:
                        reason_tag = "开板后跌幅过大"
                    else:
                        # Try to extract a more specific reason if possible
                        # Look for text after stock name
                        import re
                        match = re.search(
                            r'\[黑名单\]\s+\S+\s+\S+\s+(.+?)(?:，|,|$)',
                            blacklist_msg)
                        if match:
                            raw_reason = match.group(1).strip()
                            # Simplify the reason to a concise tag
                            if len(raw_reason) <= 20:
                                reason_tag = raw_reason.split('，')[0].split(
                                    ',')[0]
                            else:
                                # Take first meaningful part
                                reason_tag = raw_reason[:20].strip()

                result[stock_code] = reason_tag

            return result

        except Exception as e:
            logger.error(f"解析黑名单失败: {e}")
            return {}

    @staticmethod
    def parse_shared_data(shared_data: Dict) -> Dict:
        """
        解析完整的策略共享数据
        
        Args:
            shared_data: 原始共享数据字典
            
        Returns:
            Dict: 解析后的数据字典
        """
        if not shared_data:
            return {}

        parsed = {}

        try:
            # Parse each field
            field_parsers = {
                '股票信息': SharedDataParser.parse_stock_info,
                '持仓状态': SharedDataParser.parse_positions,
                '委托状态': SharedDataParser.parse_orders,
                '昨日首板股票': SharedDataParser.parse_stock_list,
                '昨日涨停股票': SharedDataParser.parse_stock_list,
                '涨停池': SharedDataParser.parse_timestamp_dict,
                '炸板池': SharedDataParser.parse_timestamp_dict,
                '最大开板回封时间': SharedDataParser.parse_number_dict,
                '开板次数': SharedDataParser.parse_number_dict,
                '股票状态信号': SharedDataParser.parse_stock_signals,
                '黑名单': SharedDataParser.parse_blacklist,
                '强势股票': SharedDataParser.parse_stock_list,
            }

            for field_name, parser_func in field_parsers.items():
                if field_name in shared_data:
                    try:
                        parsed[field_name] = parser_func(
                            shared_data[field_name])
                        logger.debug(f"成功解析字段 {field_name}")
                    except Exception as e:
                        logger.error(f"解析字段 {field_name} 失败: {e}")
                        parsed[field_name] = shared_data.get(field_name, {})

            # Add any remaining fields that weren't explicitly parsed
            for key, value in shared_data.items():
                if key not in parsed:
                    parsed[key] = value

        except Exception as e:
            logger.error(f"解析共享数据失败: {e}")
            logger.error(traceback.format_exc())

        return parsed

    @staticmethod
    def extract_useful_info(parsed_data: Dict) -> Dict:
        """
        从解析后的数据中提取有用信息用于复盘报告
        
        Args:
            parsed_data: 已解析的共享数据
            
        Returns:
            Dict: 提取的有用信息
        """
        info = {
            'positions': {},
            'orders': {},
            'limit_up_stocks': {},  # 当日触及涨停的股票池，包括封板和炸板的股票
            'break_stocks': {},  # 炸板池，包括已回封的股票
            'yesterday_first_board': [],
            'yesterday_limit_up': [],
            'strong_stocks': [],
            'blacklist': {},
            'stock_features': {},
            'break_statistics': {},
        }

        try:
            # Extract positions
            positions = parsed_data.get('持仓状态', {})
            for stock_code, position in positions.items():
                if isinstance(position, dict):
                    info['positions'][stock_code] = {
                        '股票代码': position.get('证券代码', stock_code),
                        '持仓数量': position.get('持仓数量', 0),
                        '成本价': position.get('成本价', 0),
                        '市值': position.get('市值', 0),
                    }

            # Extract orders
            orders = parsed_data.get('委托状态', {})
            for stock_code, order_list in orders.items():
                if isinstance(order_list, list) and order_list:
                    info['orders'][stock_code] = order_list

            # Extract limit up/break pools with time
            limit_up_pool = parsed_data.get('涨停池', {})
            break_pool = parsed_data.get('炸板池', {})

            for stock_code, timestamps in limit_up_pool.items():
                if timestamps:
                    info['limit_up_stocks'][stock_code] = {
                        'times': timestamps,
                        'first_time': timestamps[0] if timestamps else None,
                        'last_time': timestamps[-1] if timestamps else None,
                        'count': len(timestamps)
                    }

            for stock_code, timestamps in break_pool.items():
                if timestamps:
                    info['break_stocks'][stock_code] = {
                        'times': timestamps,
                        'first_time': timestamps[0] if timestamps else None,
                        'last_time': timestamps[-1] if timestamps else None,
                        'count': len(timestamps)
                    }

            # Extract lists
            info['yesterday_first_board'] = parsed_data.get('昨日首板股票', [])
            info['yesterday_limit_up'] = parsed_data.get('昨日涨停股票', [])
            info['strong_stocks'] = parsed_data.get('强势股票', [])

            # Extract blacklist
            info['blacklist'] = parsed_data.get('黑名单', {})

            # Extract break statistics
            max_rebound_time = parsed_data.get('最大开板回封时间', {})
            break_count = parsed_data.get('开板次数', {})

            for stock_code in set(
                    list(max_rebound_time.keys()) + list(break_count.keys())):
                info['break_statistics'][stock_code] = {
                    '最大回封时间': max_rebound_time.get(stock_code, 0),
                    '开板次数': break_count.get(stock_code, 0)
                }

            # Extract stock features from stock info and signals
            stock_info = parsed_data.get('股票信息', {})
            stock_signals = parsed_data.get('股票状态信号', {})

            for stock_code in stock_info:
                features = stock_info[stock_code].copy()

                # Add signal info if available
                if stock_code in stock_signals:
                    signals = stock_signals[stock_code]
                    features['股票状态'] = signals.get('股票状态', 0)
                    features['下单状态'] = signals.get('下单状态', 0)
                    features['封单金额'] = signals.get('封单金额', 0)
                    features['最高价'] = signals.get('最高价', 0)

                info['stock_features'][stock_code] = features

        except Exception as e:
            logger.error(f"提取有用信息失败: {e}")
            logger.error(traceback.format_exc())

        return info


# 为向后兼容保留旧的类名作为别名
ShadowDataParser = SharedDataParser
