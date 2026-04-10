"""
Deque缓冲区管理器 - 优化版Level 2数据处理

使用collections.deque实现无锁队列，避免共享内存和序列化开销

特点:
1. 无序列化开销 - 直接存储Python对象
2. Lock-free - deque的append()和popleft()是原子操作
3. 线程安全 - 适合多线程消费
4. 高性能 - 避免跨进程内存拷贝
"""

import time
import logging
from collections import deque
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DequeConfig:
    """Deque配置"""
    name: str
    maxlen: int
    
    @classmethod
    def for_l2quote(cls, name: str = "l2quote") -> "DequeConfig":
        """
        l2quote配置
        
        - 快照数据，3秒推送一次
        - 数据量相对较小
        """
        return cls(name=name, maxlen=100_000)
    
    @classmethod
    def for_l2order(cls, name: str = "l2order") -> "DequeConfig":
        """
        l2order配置
        
        - 高频数据，9:25集合竞价峰值
        - 需要较大缓冲区
        """
        return cls(name=name, maxlen=1_500_000)
    
    @classmethod
    def for_l2transaction(cls, name: str = "l2transaction") -> "DequeConfig":
        """
        l2transaction配置
        
        - 成交数据，频率中等
        """
        return cls(name=name, maxlen=500_000)


@dataclass
class DataPacket:
    """数据包 - 封装回调数据"""
    stock_code: str
    data: dict


class DequeBuffer:
    """
    基于deque的无锁缓冲区
    
    特点:
    - 线程安全的append和popleft操作
    - 无GIL竞争（原子操作）
    - 无序列化开销
    """
    
    def __init__(self, config: DequeConfig):
        """
        初始化deque缓冲区
        
        Args:
            config: 缓冲区配置
        """
        self.name = config.name
        self.maxlen = config.maxlen
        self._queue = deque(maxlen=config.maxlen)
        
        # 统计信息
        self._total_appended = 0
        self._total_popped = 0
        self._overflow_count = 0
        self._last_overflow_time = 0
    
    def put(self, data_packet: DataPacket) -> bool:
        """
        写入数据包（生产者调用）
        
        Args:
            data_packet: 数据包
            
        Returns:
            True=成功，False=失败（不会失败，除非异常）
        """
        try:
            # 检查是否会溢出
            if len(self._queue) >= self.maxlen - 1:
                self._overflow_count += 1
                self._last_overflow_time = time.time()
                # deque会自动丢弃最旧的数据
            
            # 原子操作，线程安全
            self._queue.append(data_packet)
            self._total_appended += 1
            return True
        except Exception as e:
            logger.error(f"Error putting data to {self.name}: {e}")
            return False
    
    def put_batch(self, data_packets: list) -> int:
        """
        批量写入数据包
        
        Args:
            data_packets: 数据包列表
            
        Returns:
            成功写入的数量
        """
        success_count = 0
        for packet in data_packets:
            if self.put(packet):
                success_count += 1
        return success_count
    
    def get(self) -> Optional[DataPacket]:
        """
        读取数据包（消费者调用）
        
        Returns:
            DataPacket或None（队列为空）
        """
        try:
            # 原子操作，线程安全
            packet = self._queue.popleft()
            self._total_popped += 1
            return packet
        except IndexError:
            # 队列为空
            return None
        except Exception as e:
            logger.error(f"Error getting data from {self.name}: {e}")
            return None
    
    def get_batch(self, max_count: int = 100) -> list:
        """
        批量读取数据包
        
        Args:
            max_count: 最多读取数量
            
        Returns:
            数据包列表
        """
        packets = []
        for _ in range(max_count):
            packet = self.get()
            if packet is None:
                break
            packets.append(packet)
        return packets
    
    def qsize(self) -> int:
        """返回队列当前大小"""
        return len(self._queue)
    
    def usage_rate(self) -> float:
        """返回队列使用率"""
        return len(self._queue) / self.maxlen if self.maxlen > 0 else 0.0
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'name': self.name,
            'qsize': len(self._queue),
            'maxlen': self.maxlen,
            'usage_rate': self.usage_rate(),
            'total_appended': self._total_appended,
            'total_popped': self._total_popped,
            'overflow_count': self._overflow_count,
            'pending': self._total_appended - self._total_popped
        }
    
    def clear(self):
        """清空队列"""
        self._queue.clear()


class DequeBufferManager:
    """
    Deque缓冲区管理器
    
    管理l2quote、l2order、l2transaction三个独立队列
    用于单个分区进程内的数据缓冲
    """
    
    def __init__(self):
        """初始化缓冲区管理器"""
        self.quote_buffer = DequeBuffer(DequeConfig.for_l2quote())
        self.order_buffer = DequeBuffer(DequeConfig.for_l2order())
        self.trans_buffer = DequeBuffer(DequeConfig.for_l2transaction())
        
        # 记录回调次数
        self._callback_counts = {
            'quote': 0,
            'order': 0,
            'trans': 0
        }
    
    def on_l2quote_callback(self, datas: dict):
        """
        l2quote回调函数
        
        Args:
            datas: {stock_code: quote_dict, ...}
        """
        self._callback_counts['quote'] += 1
        
        # 批量构建数据包
        packets = [
            DataPacket(stock_code=stock_code, data=quote_data)
            for stock_code, quote_data in datas.items()
        ]
        
        # 批量写入
        self.quote_buffer.put_batch(packets)
    
    def on_l2order_callback(self, datas: dict):
        """
        l2order回调函数
        
        Args:
            datas: {stock_code: order_dict, ...}
        """
        self._callback_counts['order'] += 1
        
        packets = [
            DataPacket(stock_code=stock_code, data=order_data)
            for stock_code, order_data in datas.items()
        ]
        
        self.order_buffer.put_batch(packets)
    
    def on_l2transaction_callback(self, datas: dict):
        """
        l2transaction回调函数
        
        Args:
            datas: {stock_code: trans_dict, ...}
        """
        self._callback_counts['trans'] += 1
        
        packets = [
            DataPacket(stock_code=stock_code, data=trans_data)
            for stock_code, trans_data in datas.items()
        ]
        
        self.trans_buffer.put_batch(packets)
    
    def get_all_stats(self) -> Dict:
        """获取所有缓冲区统计信息"""
        return {
            'quote': self.quote_buffer.get_stats(),
            'order': self.order_buffer.get_stats(),
            'trans': self.trans_buffer.get_stats(),
            'callbacks': self._callback_counts.copy()
        }
    
    def clear_all(self):
        """清空所有队列"""
        self.quote_buffer.clear()
        self.order_buffer.clear()
        self.trans_buffer.clear()


def benchmark_deque_vs_msgpack():
    """
    性能基准测试：deque vs msgpack+shared_memory
    """
    import msgpack
    
    print("=" * 70)
    print("Performance Benchmark: Deque vs Msgpack")
    print("=" * 70)
    
    # 测试数据
    test_data = {
        'stock_code': '600000.SH',
        'data': {
            'bidPrice': [10.1, 10.0, 9.9, 9.8, 9.7],
            'bidVol': [1000, 2000, 3000, 4000, 5000],
            'askPrice': [10.2, 10.3, 10.4, 10.5, 10.6],
            'askVol': [1500, 2500, 3500, 4500, 5500],
            'lastPrice': 10.15,
            'volume': 1234567,
            'amount': 12345678.90
        }
    }
    
    num_iterations = 100000
    
    # 测试1: Deque (无序列化)
    print(f"\nTest 1: Deque (no serialization) - {num_iterations} iterations")
    buffer = DequeBuffer(DequeConfig(name="test", maxlen=num_iterations * 2))
    
    start_time = time.time()
    for _ in range(num_iterations):
        packet = DataPacket(
            stock_code=test_data['stock_code'],
            data=test_data['data']
        )
        buffer.put(packet)
    deque_write_time = time.time() - start_time
    
    start_time = time.time()
    for _ in range(num_iterations):
        buffer.get()
    deque_read_time = time.time() - start_time
    
    print(f"  Write: {deque_write_time:.4f}s ({deque_write_time/num_iterations*1e6:.2f}μs per op)")
    print(f"  Read:  {deque_read_time:.4f}s ({deque_read_time/num_iterations*1e6:.2f}μs per op)")
    
    # 测试2: Msgpack序列化（模拟共享内存场景）
    print(f"\nTest 2: Msgpack serialization - {num_iterations} iterations")
    packer = msgpack.Packer(use_bin_type=True)
    
    start_time = time.time()
    for _ in range(num_iterations):
        packed = packer.pack(test_data)
        # 模拟写入共享内存
    msgpack_write_time = time.time() - start_time
    
    # 序列化一次用于读取测试
    packed_data = packer.pack(test_data)
    start_time = time.time()
    for _ in range(num_iterations):
        unpacked = msgpack.unpackb(packed_data, raw=False)
    msgpack_read_time = time.time() - start_time
    
    print(f"  Write: {msgpack_write_time:.4f}s ({msgpack_write_time/num_iterations*1e6:.2f}μs per op)")
    print(f"  Read:  {msgpack_read_time:.4f}s ({msgpack_read_time/num_iterations*1e6:.2f}μs per op)")
    
    # 对比
    print("\n" + "=" * 70)
    print("Performance Comparison:")
    print(f"  Write speedup: {msgpack_write_time/deque_write_time:.2f}x faster with deque")
    print(f"  Read speedup:  {msgpack_read_time/deque_read_time:.2f}x faster with deque")
    print(f"  Total speedup: {(msgpack_write_time+msgpack_read_time)/(deque_write_time+deque_read_time):.2f}x faster with deque")
    print("=" * 70)


if __name__ == '__main__':
    # 运行基准测试
    benchmark_deque_vs_msgpack()
