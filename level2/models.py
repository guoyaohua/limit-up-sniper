"""
数据模型定义

定义系统中使用的数据结构，包括委托信息、虚拟委托、资金流向统计等。
"""

from dataclasses import dataclass, field
from typing import Dict
from level2.enums import OrderSize, EntrustDirection


@dataclass
class OrderInfo:
    """委托信息"""
    entrust_no: int          # 委托号
    stock_code: str          # 股票代码
    direction: int           # 委托方向（1=买, 2=卖）
    total_volume: int        # 原始委托量（用于判定大单）
    price: float = 0.0       # 委托价格
    filled_volume: int = 0   # 已成交量
    filled_amount: float = 0.0  # 已成交金额
    timestamp: int = 0       # 时间戳（毫秒）
    last_order_size: OrderSize = OrderSize.SMALL  # 上次累计后的订单大小分类（用于沪市调整）
    
    @property
    def is_large_order(self) -> bool:
        """判定是否为大单（基于原始委托量）"""
        from level2.enums import OrderThreshold
        return self.total_volume >= OrderThreshold.LARGE_VOLUME
    
    @property
    def is_super_large_order(self) -> bool:
        """判定是否为超大单"""
        from level2.enums import OrderThreshold
        return self.total_volume >= OrderThreshold.SUPER_LARGE_VOLUME
    
    @property
    def order_size(self) -> OrderSize:
        """获取订单规模分类"""
        from level2.enums import classify_order_size
        # 使用委托量和估算金额进行分类
        estimated_amount = self.total_volume * self.price if self.price > 0 else 0
        return classify_order_size(self.total_volume, estimated_amount)
    
    @property
    def is_buy(self) -> bool:
        """是否为买入委托"""
        return self.direction == EntrustDirection.BUY
    
    @property
    def is_sell(self) -> bool:
        """是否为卖出委托"""
        return self.direction == EntrustDirection.SELL


@dataclass
class VirtualOrder:
    """
    虚拟委托（用于上交所缺失委托追踪）
    
    上交所可能省略已全成交的委托，通过成交数据聚合反推委托信息
    """
    entrust_no: int               # 委托号
    total_volume: int = 0         # 累计成交量
    total_amount: float = 0.0     # 累计成交金额
    last_update_time: int = 0     # 最后更新时间（毫秒）
    transaction_count: int = 0    # 成交次数
    last_order_size: OrderSize = OrderSize.SMALL  # 上次累计后的订单大小分类（用于调整）
    
    @property
    def avg_price(self) -> float:
        """平均成交价格"""
        if self.total_volume > 0:
            return self.total_amount / self.total_volume
        return 0.0
    
    @property
    def is_large_order(self) -> bool:
        """判定是否为大单"""
        from level2.enums import is_large_order
        return is_large_order(self.total_volume, self.total_amount)


@dataclass
class CapitalFlowStats:
    """资金流向统计"""
    stock_code: str
    super_large_buy: float = 0.0      # 超大单买入金额
    super_large_sell: float = 0.0     # 超大单卖出金额
    large_buy: float = 0.0            # 大单买入金额
    large_sell: float = 0.0           # 大单卖出金额
    medium_buy: float = 0.0           # 中单买入金额
    medium_sell: float = 0.0          # 中单卖出金额
    small_buy: float = 0.0            # 小单买入金额
    small_sell: float = 0.0           # 小单卖出金额
    
    # 成交笔数统计
    super_large_buy_count: int = 0
    super_large_sell_count: int = 0
    large_buy_count: int = 0
    large_sell_count: int = 0
    
    # 时间戳
    last_update_time: int = 0
    
    @property
    def net_super_large(self) -> float:
        """超大单净流入"""
        return self.super_large_buy - self.super_large_sell
    
    @property
    def net_large(self) -> float:
        """大单净流入"""
        return self.large_buy - self.large_sell
    
    @property
    def net_main(self) -> float:
        """主力净流入（超大单+大单）"""
        return self.net_super_large + self.net_large
    
    @property
    def net_retail(self) -> float:
        """散户净流入（中单+小单）"""
        return (self.medium_buy - self.medium_sell) + (self.small_buy - self.small_sell)
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'stock_code': self.stock_code,
            'super_large_buy': self.super_large_buy,
            'super_large_sell': self.super_large_sell,
            'large_buy': self.large_buy,
            'large_sell': self.large_sell,
            'medium_buy': self.medium_buy,
            'medium_sell': self.medium_sell,
            'small_buy': self.small_buy,
            'small_sell': self.small_sell,
            'net_super_large': self.net_super_large,
            'net_large': self.net_large,
            'net_main': self.net_main,
            'net_retail': self.net_retail,
            'last_update_time': self.last_update_time
        }


@dataclass
class SealAmountInfo:
    """封板金额信息"""
    stock_code: str
    limit_price: float = 0.0          # 涨停价
    baseline_volume: int = 0          # 快照基线封单量
    baseline_time: int = 0            # 基线时间戳
    delta_buy: int = 0                # 增量买单量
    delta_consume: int = 0            # 消耗量（成交+撤单）
    is_limit_up: bool = False         # 是否涨停
    last_quote_time: int = 0          # 最后快照时间
    
    @property
    def current_volume(self) -> int:
        """当前实时封单量"""
        return max(0, self.baseline_volume + self.delta_buy - self.delta_consume)
    
    @property
    def seal_amount(self) -> float:
        """封板金额（元）"""
        return self.current_volume * self.limit_price
    
    @property
    def seal_amount_wan(self) -> float:
        """封板金额（万元）"""
        return self.seal_amount / 10000
    
    @property
    def is_weak_seal(self) -> bool:
        """是否弱封板（<2000万）"""
        from level2.enums import OrderThreshold
        return self.seal_amount < OrderThreshold.SEAL_ALERT_AMOUNT
    
    def reset_baseline(self, volume: int, timestamp: int):
        """重置基线（快照校准）"""
        self.baseline_volume = volume
        self.baseline_time = timestamp
        self.delta_buy = 0
        self.delta_consume = 0
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'stock_code': self.stock_code,
            'limit_price': self.limit_price,
            'seal_volume': self.current_volume,
            'seal_amount': self.seal_amount,
            'seal_amount_wan': self.seal_amount_wan,
            'is_limit_up': self.is_limit_up,
            'is_weak_seal': self.is_weak_seal,
            'baseline_volume': self.baseline_volume,
            'delta_buy': self.delta_buy,
            'delta_consume': self.delta_consume,
            'last_quote_time': self.last_quote_time
        }


@dataclass
class LimitUpPeriod:
    """涨停时段记录"""
    stock_code: str
    start_time: int          # 涨停开始时间
    end_time: int = 0        # 涨停结束时间（炸板时间，0表示未炸板）
    is_active: bool = True   # 是否当前涨停中
    
    @property
    def duration_ms(self) -> int:
        """涨停持续时长（毫秒）"""
        if self.end_time > 0:
            return self.end_time - self.start_time
        return 0
    
    @property
    def is_sealed(self) -> bool:
        """是否封板到收盘"""
        return self.is_active and self.end_time == 0


@dataclass
class BufferStats:
    """缓冲区统计信息"""
    buffer_name: str
    total_writes: int = 0      # 总写入次数
    total_reads: int = 0       # 总读取次数
    overflow_count: int = 0    # 溢出次数
    current_size: int = 0      # 当前使用量
    max_size: int = 0          # 最大容量
    
    @property
    def usage_rate(self) -> float:
        """使用率"""
        if self.max_size > 0:
            return self.current_size / self.max_size
        return 0.0
    
    @property
    def overflow_rate(self) -> float:
        """溢出率"""
        if self.total_writes > 0:
            return self.overflow_count / self.total_writes
        return 0.0
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'buffer_name': self.buffer_name,
            'total_writes': self.total_writes,
            'total_reads': self.total_reads,
            'overflow_count': self.overflow_count,
            'current_size': self.current_size,
            'max_size': self.max_size,
            'usage_rate': self.usage_rate,
            'overflow_rate': self.overflow_rate
        }