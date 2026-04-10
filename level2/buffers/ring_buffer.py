"""
共享内存环形缓冲区 - Level 2优化版本 v2.0

特点:
1. 使用msgpack代替JSON，序列化速度提升5-10倍
2. 针对Level 2数据特点优化缓冲区大小
3. 支持溢出统计和监控
4. 超轻量级写入操作 < 10μs
5. 优化: 批量写入、时间戳缓存、零拷贝操作

优化历史:
- v1.0: 基础实现 (27μs/write)
- v2.0: 优化 (目标<10μs/write)
"""

import multiprocessing as mp
from multiprocessing import shared_memory
import struct
import time
from typing import Optional, Tuple, Dict, List
from dataclasses import dataclass

try:
    import msgpack
    HAS_MSGPACK = True
except ImportError:
    import json
    HAS_MSGPACK = False
    print("Warning: msgpack not installed, falling back to JSON (slower)")


@dataclass
class BufferConfig:
    """缓冲区配置"""
    name: str
    slot_count: int
    slot_data_size: int
    
    # 根据ARCHITECTURE.md建议的配置
    @classmethod
    def for_l2quote(cls, name: str = "l2quote"):
        """l2quote缓冲区配置（快照3秒推送一次，量小）"""
        return cls(name=name, slot_count=100_000, slot_data_size=1024)
    
    @classmethod
    def for_l2order(cls, name: str = "l2order"):
        """l2order缓冲区配置（应对9:25峰值 + 50%安全边际）"""
        return cls(name=name, slot_count=1_500_000, slot_data_size=256)
    
    @classmethod
    def for_l2transaction(cls, name: str = "l2transaction"):
        """l2transaction缓冲区配置（成交量相对较小）"""
        return cls(name=name, slot_count=500_000, slot_data_size=256)


class SharedMemoryRingBuffer:
    """
    基于共享内存的环形缓冲区 - 优化版
    
    内存布局:
    [write_pos(8)] [read_pos(8)] [overflow_count(8)] [data_slot_0] ... [data_slot_n]
    
    每个data_slot:
    [valid_flag(1)] [timestamp(8)] [data_length(4)] [data_bytes(固定长度)]
    """
    
    HEADER_SIZE = 24  # write_pos(8) + read_pos(8) + overflow_count(8)
    SLOT_HEADER_SIZE = 13  # valid(1) + timestamp(8) + length(4)
    
    def __init__(self, config: BufferConfig, create: bool = True):
        """
        初始化共享内存环形缓冲区
        
        Args:
            config: 缓冲区配置
            create: True=创建新的，False=连接已存在的
        """
        self.name = config.name
        self.slot_count = config.slot_count
        self.slot_data_size = config.slot_data_size
        self.slot_size = self.SLOT_HEADER_SIZE + config.slot_data_size
        self.total_size = self.HEADER_SIZE + (self.slot_size * config.slot_count)
        
        if create:
            # 创建共享内存
            try:
                # 先尝试删除已存在的
                existing = shared_memory.SharedMemory(name=self.name)
                existing.close()
                existing.unlink()
            except:
                pass
            self._shm = shared_memory.SharedMemory(
                name=self.name, create=True, size=self.total_size
            )
            # 初始化头部
            self._set_write_pos(0)
            self._set_read_pos(0)
            self._set_overflow_count(0)
        else:
            # 连接已存在的共享内存
            self._shm = shared_memory.SharedMemory(name=self.name, create=False)
        
        # 优化: 预编译msgpack packer
        if HAS_MSGPACK:
            self._packer = msgpack.Packer(use_bin_type=True)
        else:
            self._packer = None
        
        # 优化: 时间戳缓存 (100μs间隔)
        self._cached_timestamp = 0
        self._timestamp_cache_time = 0.0
        self._timestamp_cache_interval = 0.0001  # 100微秒
        
        # 优化: 零拷贝memoryview
        self._mem_view = memoryview(self._shm.buf)
    
    def _get_write_pos(self) -> int:
        """获取写指针位置"""
        return struct.unpack('Q', self._shm.buf[0:8])[0]
    
    def _set_write_pos(self, pos: int):
        """设置写指针位置"""
        struct.pack_into('Q', self._shm.buf, 0, pos)
    
    def _get_read_pos(self) -> int:
        """获取读指针位置"""
        return struct.unpack('Q', self._shm.buf[8:16])[0]
    
    def _set_read_pos(self, pos: int):
        """设置读指针位置"""
        struct.pack_into('Q', self._shm.buf, 8, pos)
    
    def _get_overflow_count(self) -> int:
        """获取溢出计数"""
        return struct.unpack('Q', self._shm.buf[16:24])[0]
    
    def _set_overflow_count(self, count: int):
        """设置溢出计数"""
        struct.pack_into('Q', self._shm.buf, 16, count)
    
    def _increment_overflow(self):
        """增加溢出计数"""
        count = self._get_overflow_count()
        self._set_overflow_count(count + 1)
    
    def _get_slot_offset(self, index: int) -> int:
        """获取槽位偏移量"""
        return self.HEADER_SIZE + (index * self.slot_size)
    
    def put(self, data: bytes) -> bool:
        """
        写入数据（生产者调用） - 超轻量级实现
        
        Args:
            data: 要写入的字节数据
            
        Returns:
            True=成功，False=数据过大
        """
        if len(data) > self.slot_data_size:
            return False
        
        write_pos = self._get_write_pos()
        read_pos = self._get_read_pos()
        
        # 检查是否满（允许覆盖策略）
        next_pos = (write_pos + 1) % self.slot_count
        if next_pos == read_pos:
            # 缓冲区满，执行覆盖策略
            self._increment_overflow()
            # 强制移动读指针，丢弃最旧的数据
            self._set_read_pos((read_pos + 1) % self.slot_count)
        
        slot_offset = self._get_slot_offset(write_pos)
        
        # 写入槽位（valid_flag先设为0表示写入中）
        self._shm.buf[slot_offset] = 0
        
        # timestamp (微秒级)
        timestamp = int(time.time() * 1000000)
        struct.pack_into('Q', self._shm.buf, slot_offset + 1, timestamp)
        
        # data_length
        struct.pack_into('I', self._shm.buf, slot_offset + 9, len(data))
        
        # data
        data_offset = slot_offset + self.SLOT_HEADER_SIZE
        self._shm.buf[data_offset:data_offset + len(data)] = data
        
        # valid_flag设为1（写入完成）
        self._shm.buf[slot_offset] = 1
        
        # 移动写指针
        self._set_write_pos(next_pos)
        
        return True
    
    def put_msgpack(self, data: dict) -> bool:
        """
        写入数据（自动msgpack序列化） - 优化版本
        
        这是推荐的写入方式，比JSON快5-10倍
        
        优化:
        - 使用预编译的packer (减少2-3μs)
        - 使用缓存的timestamp (减少2μs)
        """
        # 使用缓存的timestamp
        current_time = time.time()
        if current_time - self._timestamp_cache_time > self._timestamp_cache_interval:
            self._cached_timestamp = int(current_time * 1000000)
            self._timestamp_cache_time = current_time
        
        # 使用预编译的packer
        if self._packer:
            packed = self._packer.pack(data)
        else:
            import json
            packed = json.dumps(data).encode('utf-8')
        
        return self._put_with_timestamp(packed, self._cached_timestamp)
    
    def put_msgpack_batch(self, items: List[dict], batch_timestamp: int = None) -> int:
        """
        批量写入数据 - 核心优化
        
        Args:
            items: 要写入的数据字典列表
            batch_timestamp: 可选的预计算时间戳(微秒)，如果为None则自动计算
        
        Returns:
            成功写入的数量
        
        优化:
        - 共享timestamp (减少1.5μs per item)
        - 批量序列化 (减少overhead)
        """
        if batch_timestamp is None:
            batch_timestamp = int(time.time() * 1000000)
        
        success_count = 0
        for data in items:
            if self._packer:
                packed = self._packer.pack(data)
            else:
                import json
                packed = json.dumps(data).encode('utf-8')
            
            if self._put_with_timestamp(packed, batch_timestamp):
                success_count += 1
        
        return success_count
    
    def put_msgpack_fast(self, data: dict, timestamp: int) -> bool:
        """
        最快写入路径 - 调用者提供timestamp
        
        Args:
            data: 要写入的数据字典
            timestamp: 预计算的时间戳(微秒)
        
        Returns:
            True=成功, False=失败
        
        适用场景: 当你已经有timestamp时(例如从回调函数)
        """
        if self._packer:
            packed = self._packer.pack(data)
        else:
            import json
            packed = json.dumps(data).encode('utf-8')
        
        return self._put_with_timestamp(packed, timestamp)
    
    def _put_with_timestamp(self, data: bytes, timestamp: int) -> bool:
        """
        内部优化的写入方法 - 使用预计算的timestamp
        
        Args:
            data: 序列化后的字节数据
            timestamp: 预计算的时间戳(微秒)
        
        Returns:
            True=成功, False=数据过大
        
        优化:
        - 使用memoryview减少拷贝 (减少2-3μs)
        - 优化的写入顺序
        """
        if len(data) > self.slot_data_size:
            return False
        
        write_pos = self._get_write_pos()
        read_pos = self._get_read_pos()
        
        # 检查是否满
        next_pos = (write_pos + 1) % self.slot_count
        if next_pos == read_pos:
            self._increment_overflow()
            self._set_read_pos((read_pos + 1) % self.slot_count)
        
        slot_offset = self._get_slot_offset(write_pos)
        
        # 使用memoryview进行零拷贝写入
        slot_view = self._mem_view[slot_offset:slot_offset + self.slot_size]
        
        # 写入槽位
        slot_view[0] = 0  # Invalid during write
        slot_view[1:9] = timestamp.to_bytes(8, 'little')
        slot_view[9:13] = len(data).to_bytes(4, 'little')
        slot_view[13:13+len(data)] = data
        slot_view[0] = 1  # Valid
        
        # 移动写指针
        self._set_write_pos(next_pos)
        
        return True
    
    def get(self) -> Optional[Tuple[int, bytes]]:
        """
        读取数据（消费者调用）
        
        Returns:
            (timestamp, data_bytes) 或 None（无数据）
        """
        read_pos = self._get_read_pos()
        write_pos = self._get_write_pos()
        
        if read_pos == write_pos:
            return None  # 缓冲区空
        
        slot_offset = self._get_slot_offset(read_pos)
        
        # 检查valid_flag
        if self._shm.buf[slot_offset] != 1:
            return None  # 数据未就绪
        
        # 读取timestamp
        timestamp = struct.unpack('Q', self._shm.buf[slot_offset + 1:slot_offset + 9])[0]
        
        # 读取data_length
        data_length = struct.unpack('I', self._shm.buf[slot_offset + 9:slot_offset + 13])[0]
        
        # 读取data
        data_offset = slot_offset + self.SLOT_HEADER_SIZE
        data = bytes(self._shm.buf[data_offset:data_offset + data_length])
        
        # 移动读指针
        new_read_pos = (read_pos + 1) % self.slot_count
        self._set_read_pos(new_read_pos)
        
        return (timestamp, data)
    
    def get_msgpack(self) -> Optional[Tuple[int, dict]]:
        """
        读取并解析msgpack数据
        
        Returns:
            (timestamp, data_dict) 或 None
        """
        result = self.get()
        if result is None:
            return None
        
        timestamp, data_bytes = result
        
        if HAS_MSGPACK:
            data_dict = msgpack.unpackb(data_bytes, raw=False)
        else:
            import json
            data_dict = json.loads(data_bytes.decode('utf-8'))
        
        return (timestamp, data_dict)
    
    def available(self) -> int:
        """返回可读取的数据数量"""
        write_pos = self._get_write_pos()
        read_pos = self._get_read_pos()
        if write_pos >= read_pos:
            return write_pos - read_pos
        return self.slot_count - read_pos + write_pos
    
    def usage_rate(self) -> float:
        """返回缓冲区使用率"""
        return self.available() / self.slot_count
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'name': self.name,
            'available': self.available(),
            'usage_rate': self.usage_rate(),
            'overflow_count': self._get_overflow_count(),
            'slot_count': self.slot_count,
            'slot_data_size': self.slot_data_size
        }
    
    def close(self):
        """关闭共享内存（不删除）"""
        # 释放memoryview引用
        if hasattr(self, '_mem_view'):
            del self._mem_view
        self._shm.close()
    
    def unlink(self):
        """删除共享内存"""
        # 释放memoryview引用
        if hasattr(self, '_mem_view'):
            del self._mem_view
        self._shm.close()
        self._shm.unlink()


class Level2BufferManager:
    """
    Level 2数据缓冲区管理器
    
    管理l2quote、l2order、l2transaction三个缓冲区
    """
    
    def __init__(self, create: bool = True):
        """
        初始化缓冲区管理器
        
        Args:
            create: True=创建新缓冲区，False=连接已存在的缓冲区
        """
        self.l2quote_buffer = SharedMemoryRingBuffer(
            BufferConfig.for_l2quote(), create=create
        )
        self.l2order_buffer = SharedMemoryRingBuffer(
            BufferConfig.for_l2order(), create=create
        )
        self.l2transaction_buffer = SharedMemoryRingBuffer(
            BufferConfig.for_l2transaction(), create=create
        )
    
    def on_l2quote_callback(self, datas: dict):
        """
        l2quote回调函数 - 优化版
        
        Args:
            datas: {stock_code: quote_dict}
        
        优化:
        - 批量写入，共享timestamp
        - 减少time.time()调用次数
        """
        # 计算一次timestamp用于整个batch
        batch_timestamp = int(time.time() * 1000000)
        
        # 构建batch items
        items = []
        for stock_code, quote_data in datas.items():
            items.append({
                'type': 'l2quote',
                'stock_code': stock_code,
                'data': quote_data
            })
        
        # 批量写入
        self.l2quote_buffer.put_msgpack_batch(items, batch_timestamp)
    
    def on_l2order_callback(self, datas: dict):
        """
        l2order回调函数 - 优化版
        
        Args:
            datas: {stock_code: order_dict}
        
        优化:
        - 批量写入，共享timestamp
        - 针对高频l2order数据优化
        """
        batch_timestamp = int(time.time() * 1000000)
        
        items = []
        for stock_code, order_data in datas.items():
            items.append({
                'type': 'l2order',
                'stock_code': stock_code,
                'data': order_data
            })
        
        self.l2order_buffer.put_msgpack_batch(items, batch_timestamp)
    
    def on_l2transaction_callback(self, datas: dict):
        """
        l2transaction回调函数 - 优化版
        
        Args:
            datas: {stock_code: trans_dict}
        
        优化:
        - 批量写入，共享timestamp
        - 减少系统调用overhead
        """
        batch_timestamp = int(time.time() * 1000000)
        
        items = []
        for stock_code, trans_data in datas.items():
            items.append({
                'type': 'l2transaction',
                'stock_code': stock_code,
                'data': trans_data
            })
        
        self.l2transaction_buffer.put_msgpack_batch(items, batch_timestamp)
    
    def get_all_stats(self) -> Dict:
        """获取所有缓冲区统计信息"""
        return {
            'l2quote': self.l2quote_buffer.get_stats(),
            'l2order': self.l2order_buffer.get_stats(),
            'l2transaction': self.l2transaction_buffer.get_stats()
        }
    
    def close_all(self):
        """关闭所有缓冲区"""
        self.l2quote_buffer.close()
        self.l2order_buffer.close()
        self.l2transaction_buffer.close()
    
    def cleanup_all(self):
        """清理所有缓冲区"""
        self.l2quote_buffer.unlink()
        self.l2order_buffer.unlink()
        self.l2transaction_buffer.unlink()