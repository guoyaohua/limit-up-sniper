"""
市场统计数据计算器

基于 xtquant 的 full tick 数据计算：
- 涨跌家数
- 涨跌停数量
- 涨跌幅分布

取代从页面正则解析的不可靠方式
"""

from typing import Dict, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass, field

# 使用 loguru 进行日志管理
from logger_config import logger

# 尝试导入 xtquant
try:
    from xtquant import xtdata
    XTQUANT_AVAILABLE = True
except ImportError:
    XTQUANT_AVAILABLE = False
    logger.warning("xtquant 模块未安装，市场统计功能将不可用")


@dataclass
class MarketStats:
    """市场统计数据"""
    # 涨跌家数
    rise_count: int = 0       # 上涨家数
    fall_count: int = 0       # 下跌家数
    flat_count: int = 0       # 平盘家数
    
    # 涨跌停数量
    limit_up_count: int = 0   # 涨停数
    limit_down_count: int = 0 # 跌停数
    
    # 涨幅分布 {'0~3%': 1500, '3~5%': 200, '5~7%': 100, '7~10%': 50, '>10%': 20}
    rise_distribution: Dict[str, int] = field(default_factory=dict)
    
    # 跌幅分布 {'0~-3%': 800, '-3~-5%': 100, '-5~-7%': 50, '-7~-10%': 20, '<-10%': 5}
    fall_distribution: Dict[str, int] = field(default_factory=dict)
    
    # 统计时间
    calc_time: datetime = field(default_factory=datetime.now)
    
    # 统计的股票数量
    total_stocks: int = 0
    valid_stocks: int = 0  # 有效数据的股票数量
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'rise_count': self.rise_count,
            'fall_count': self.fall_count,
            'flat_count': self.flat_count,
            'limit_up_count': self.limit_up_count,
            'limit_down_count': self.limit_down_count,
            'rise_distribution': self.rise_distribution,
            'fall_distribution': self.fall_distribution,
            'calc_time': self.calc_time.isoformat(),
            'total_stocks': self.total_stocks,
            'valid_stocks': self.valid_stocks,
        }


class MarketStatsCalculator:
    """
    市场统计数据计算器
    
    使用 xtquant 获取沪深A股全量 tick 数据，计算市场统计指标
    """
    
    # 沪深A股板块名称
    SECTOR_NAME = '沪深A股'
    
    def __init__(self):
        """初始化计算器"""
        self._stock_list = None
    
    def _get_stock_list(self) -> list:
        """
        获取沪深A股股票列表
        
        Returns:
            股票代码列表
        """
        if not XTQUANT_AVAILABLE:
            logger.error("xtquant 模块未安装")
            return []
        
        if self._stock_list is None:
            try:
                self._stock_list = xtdata.get_stock_list_in_sector(self.SECTOR_NAME)
                logger.info(f"获取 {self.SECTOR_NAME} 股票列表: {len(self._stock_list)} 只")
            except Exception as e:
                logger.exception(f"获取股票列表失败: {e}")
                self._stock_list = []
        
        return self._stock_list
    
    def _get_full_tick_data(self, stock_list: list) -> Dict:
        """
        获取股票列表的 full tick 数据
        
        Args:
            stock_list: 股票代码列表
            
        Returns:
            tick 数据字典 {stock_code: tick_data}
        """
        if not XTQUANT_AVAILABLE:
            return {}
        
        try:
            tick_data = xtdata.get_full_tick(code_list=stock_list)
            return tick_data if tick_data else {}
        except Exception as e:
            logger.exception(f"获取 tick 数据失败: {e}")
            return {}
    
    def _calculate_change_percent(self, last_price: float, last_close: float) -> Optional[float]:
        """
        计算涨跌幅
        
        Args:
            last_price: 当前价格
            last_close: 昨收价
            
        Returns:
            涨跌幅百分比，无效数据返回 None
        """
        if last_close <= 0 or last_price <= 0:
            return None
        return (last_price - last_close) / last_close * 100
    
    def _is_limit_up(self, tick_data: Dict) -> bool:
        """
        判断是否涨停
        
        通过盘口判断：
        1. 卖一价为0或卖一量为0（无卖盘）
        2. 同时买一价不为0且买一量不为0（有买盘，排除停牌情况）
        
        Args:
            tick_data: tick 数据
            
        Returns:
            是否涨停
        """
        if not tick_data:
            return False
        
        # 获取卖一价和卖一量（对手盘）
        ask_prices = tick_data.get('askPrice', [])
        ask_vols = tick_data.get('askVol', [])
        ask_price = ask_prices[0] if ask_prices else 0
        ask_vol = ask_vols[0] if ask_vols else 0
        
        # 获取买一价和买一量（本方盘）
        bid_prices = tick_data.get('bidPrice', [])
        bid_vols = tick_data.get('bidVol', [])
        bid_price = bid_prices[0] if bid_prices else 0
        bid_vol = bid_vols[0] if bid_vols else 0
        
        # 无卖盘（对手盘为空）且有买盘（本方盘不为空），才是涨停
        # 如果买卖盘都为空，则是停牌，不算涨停
        no_ask = (ask_price == 0 or ask_vol == 0)
        has_bid = (bid_price > 0 and bid_vol > 0)
        
        return no_ask and has_bid
    
    def _is_limit_down(self, tick_data: Dict) -> bool:
        """
        判断是否跌停
        
        通过盘口判断：
        1. 买一价为0或买一量为0（无买盘）
        2. 同时卖一价不为0且卖一量不为0（有卖盘，排除停牌情况）
        
        Args:
            tick_data: tick 数据
            
        Returns:
            是否跌停
        """
        if not tick_data:
            return False
        
        # 获取买一价和买一量（对手盘）
        bid_prices = tick_data.get('bidPrice', [])
        bid_vols = tick_data.get('bidVol', [])
        bid_price = bid_prices[0] if bid_prices else 0
        bid_vol = bid_vols[0] if bid_vols else 0
        
        # 获取卖一价和卖一量（本方盘）
        ask_prices = tick_data.get('askPrice', [])
        ask_vols = tick_data.get('askVol', [])
        ask_price = ask_prices[0] if ask_prices else 0
        ask_vol = ask_vols[0] if ask_vols else 0
        
        # 无买盘（对手盘为空）且有卖盘（本方盘不为空），才是跌停
        # 如果买卖盘都为空，则是停牌，不算跌停
        no_bid = (bid_price == 0 or bid_vol == 0)
        has_ask = (ask_price > 0 and ask_vol > 0)
        
        return no_bid and has_ask
    
    def _get_change_range(self, change_percent: float) -> Tuple[str, bool]:
        """
        根据涨跌幅确定所属区间
        
        Args:
            change_percent: 涨跌幅百分比
            
        Returns:
            (区间名称, 是否上涨区间)
        """
        if change_percent > 10:
            return ('>10%', True)
        elif change_percent > 7:
            return ('7~10%', True)
        elif change_percent > 5:
            return ('5~7%', True)
        elif change_percent > 3:
            return ('3~5%', True)
        elif change_percent > 0:
            return ('0~3%', True)
        elif change_percent == 0:
            return ('flat', False)  # 平盘
        elif change_percent > -3:
            return ('0~-3%', False)
        elif change_percent > -5:
            return ('-3~-5%', False)
        elif change_percent > -7:
            return ('-5~-7%', False)
        elif change_percent > -10:
            return ('-7~-10%', False)
        else:
            return ('<-10%', False)
    
    def calculate_market_stats(self) -> MarketStats:
        """
        计算市场统计数据
        
        获取沪深A股全量 tick 数据，统计：
        - 涨跌家数
        - 涨跌停数量
        - 涨跌幅分布
        
        Returns:
            MarketStats 统计结果
        """
        stats = MarketStats()
        stats.calc_time = datetime.now()
        
        if not XTQUANT_AVAILABLE:
            logger.warning("xtquant 模块未安装，无法计算市场统计")
            return stats
        
        # 获取股票列表
        stock_list = self._get_stock_list()
        if not stock_list:
            logger.warning("股票列表为空")
            return stats
        
        stats.total_stocks = len(stock_list)
        
        # 初始化分布字典
        stats.rise_distribution = {
            '0~3%': 0,
            '3~5%': 0,
            '5~7%': 0,
            '7~10%': 0,
            '>10%': 0,
        }
        stats.fall_distribution = {
            '0~-3%': 0,
            '-3~-5%': 0,
            '-5~-7%': 0,
            '-7~-10%': 0,
            '<-10%': 0,
        }
        
        # 获取 tick 数据
        logger.info(f"正在获取 {len(stock_list)} 只股票的 tick 数据...")
        tick_data = self._get_full_tick_data(stock_list)
        
        if not tick_data:
            logger.warning("未获取到 tick 数据")
            return stats
        
        # 遍历统计
        for stock_code in stock_list:
            if stock_code not in tick_data or not tick_data[stock_code]:
                continue
            
            data = tick_data[stock_code]
            
            # 获取当前价和昨收价
            last_price = data.get('lastPrice', 0)
            last_close = data.get('lastClose', 0)
            
            # 跳过无效数据
            if last_price <= 0 or last_close <= 0:
                continue
            
            stats.valid_stocks += 1
            
            # 计算涨跌幅
            change_percent = self._calculate_change_percent(last_price, last_close)
            if change_percent is None:
                continue
            
            # 统计涨跌家数
            if change_percent > 0:
                stats.rise_count += 1
            elif change_percent < 0:
                stats.fall_count += 1
            else:
                stats.flat_count += 1
            
            # 判断涨跌停
            if self._is_limit_up(data):
                stats.limit_up_count += 1
            elif self._is_limit_down(data):
                stats.limit_down_count += 1
            
            # 统计涨跌幅分布
            range_name, is_rise = self._get_change_range(change_percent)
            if range_name == 'flat':
                # 平盘已在上面统计
                pass
            elif is_rise:
                if range_name in stats.rise_distribution:
                    stats.rise_distribution[range_name] += 1
            else:
                if range_name in stats.fall_distribution:
                    stats.fall_distribution[range_name] += 1
        
        logger.info(f"市场统计完成: 总{stats.total_stocks}只, 有效{stats.valid_stocks}只, "
                   f"涨{stats.rise_count}, 跌{stats.fall_count}, 平{stats.flat_count}, "
                   f"涨停{stats.limit_up_count}, 跌停{stats.limit_down_count}")
        
        return stats


# 单例模式
_calculator_instance = None

def get_calculator() -> MarketStatsCalculator:
    """获取计算器单例"""
    global _calculator_instance
    if _calculator_instance is None:
        _calculator_instance = MarketStatsCalculator()
    return _calculator_instance


def calculate_market_stats() -> MarketStats:
    """
    计算市场统计数据（便捷函数）
    
    Returns:
        MarketStats 统计结果
    """
    calculator = get_calculator()
    return calculator.calculate_market_stats()


if __name__ == '__main__':
    # 测试代码
    import json
    
    logger.info("开始计算市场统计数据...")
    stats = calculate_market_stats()
    
    logger.info("=" * 50)
    logger.info(f"统计时间: {stats.calc_time}")
    logger.info(f"股票总数: {stats.total_stocks}, 有效数据: {stats.valid_stocks}")
    logger.info(f"涨跌家数: 涨 {stats.rise_count} / 跌 {stats.fall_count} / 平 {stats.flat_count}")
    logger.info(f"涨跌停: 涨停 {stats.limit_up_count} / 跌停 {stats.limit_down_count}")
    logger.info(f"涨幅分布: {json.dumps(stats.rise_distribution, ensure_ascii=False)}")
    logger.info(f"跌幅分布: {json.dumps(stats.fall_distribution, ensure_ascii=False)}")
    logger.info("=" * 50)
