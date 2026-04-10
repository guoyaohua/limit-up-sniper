"""
共享内存环形缓冲区 + 多进程消费者

特点：
1. 生产者（回调）直接写入共享内存，无序列化开销
2. 多个消费者进程直接从共享内存读取，真正CPU并行
3. 无锁设计（单生产者多消费者场景需要简单同步）
"""

import multiprocessing as mp
from multiprocessing import shared_memory, Process, Value
import numpy as np
import time
import struct
from datetime import datetime


class SharedMemoryRingBuffer:
    """
    基于共享内存的环形缓冲区
    
    内存布局:
    [write_pos(8字节)] [read_pos(8字节)] [data_slot_0] [data_slot_1] ... [data_slot_n]
    
    每个data_slot:
    [valid_flag(1字节)] [timestamp(8字节)] [data_length(4字节)] [data_bytes(固定长度)]
    """
    
    HEADER_SIZE = 16  # write_pos(8) + read_pos(8)
    SLOT_HEADER_SIZE = 13  # valid(1) + timestamp(8) + length(4)
    
    def __init__(self, name: str, slot_count: int = 100000, slot_data_size: int = 512, create: bool = True):
        """
        初始化共享内存环形缓冲区
        
        Args:
            name: 共享内存名称
            slot_count: 槽位数量
            slot_data_size: 每个槽位的数据大小（字节）
            create: True=创建新的，False=连接已存在的
        """
        self.name = name
        self.slot_count = slot_count
        self.slot_data_size = slot_data_size
        self.slot_size = self.SLOT_HEADER_SIZE + slot_data_size
        self.total_size = self.HEADER_SIZE + (self.slot_size * slot_count)
        
        if create:
            # 创建共享内存
            try:
                # 先尝试删除已存在的
                existing = shared_memory.SharedMemory(name=name)
                existing.close()
                existing.unlink()
            except:
                pass
            self._shm = shared_memory.SharedMemory(name=name, create=True, size=self.total_size)
            # 初始化头部
            self._set_write_pos(0)
            self._set_read_pos(0)
        else:
            # 连接已存在的共享内存
            self._shm = shared_memory.SharedMemory(name=name, create=False)
            
        # 创建numpy视图方便操作
        self._buffer = np.ndarray((self.total_size,), dtype=np.uint8, buffer=self._shm.buf)
        
    def _get_write_pos(self) -> int:
        return struct.unpack('Q', self._shm.buf[0:8])[0]
    
    def _set_write_pos(self, pos: int):
        struct.pack_into('Q', self._shm.buf, 0, pos)
        
    def _get_read_pos(self) -> int:
        return struct.unpack('Q', self._shm.buf[8:16])[0]
    
    def _set_read_pos(self, pos: int):
        struct.pack_into('Q', self._shm.buf, 8, pos)
        
    def _get_slot_offset(self, index: int) -> int:
        return self.HEADER_SIZE + (index * self.slot_size)
    
    def put(self, data: bytes) -> bool:
        """
        写入数据（生产者调用）
        
        Args:
            data: 要写入的字节数据
            
        Returns:
            True=成功，False=数据过大
        """
        if len(data) > self.slot_data_size:
            return False
            
        write_pos = self._get_write_pos()
        slot_offset = self._get_slot_offset(write_pos)
        
        # 写入槽位
        # valid_flag = 0（写入中），完成后改为1
        self._shm.buf[slot_offset] = 0
        
        # timestamp
        timestamp = int(time.time() * 1000000)  # 微秒级时间戳
        struct.pack_into('Q', self._shm.buf, slot_offset + 1, timestamp)
        
        # data_length
        struct.pack_into('I', self._shm.buf, slot_offset + 9, len(data))
        
        # data
        data_offset = slot_offset + self.SLOT_HEADER_SIZE
        self._shm.buf[data_offset:data_offset + len(data)] = data
        
        # valid_flag = 1（写入完成）
        self._shm.buf[slot_offset] = 1
        
        # 移动写指针
        new_write_pos = (write_pos + 1) % self.slot_count
        self._set_write_pos(new_write_pos)
        
        return True
    
    def put_dict(self, data: dict) -> bool:
        """便捷方法：写入字典（自动序列化）"""
        import json
        return self.put(json.dumps(data).encode('utf-8'))
    
    def get(self, consumer_id: int = 0) -> tuple:
        """
        读取数据（消费者调用）
        
        注意：多消费者场景下，每个消费者需要维护自己的读取位置
        这里简化为使用全局read_pos，适合单消费者或分片消费
        
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
    
    def get_dict(self) -> tuple:
        """便捷方法：读取并解析为字典"""
        import json
        result = self.get()
        if result is None:
            return None
        timestamp, data = result
        return (timestamp, json.loads(data.decode('utf-8')))
    
    def available(self) -> int:
        """返回可读取的数据数量"""
        write_pos = self._get_write_pos()
        read_pos = self._get_read_pos()
        if write_pos >= read_pos:
            return write_pos - read_pos
        return self.slot_count - read_pos + write_pos
    
    def close(self):
        """关闭共享内存（不删除）"""
        self._shm.close()
        
    def unlink(self):
        """删除共享内存"""
        self._shm.close()
        self._shm.unlink()


class MultiConsumerSharedBuffer:
    """
    多消费者共享内存缓冲区
    
    每个消费者进程独立维护读取位置，实现并行消费
    """
    
    def __init__(self, name: str, slot_count: int = 100000, 
                 slot_data_size: int = 512, num_consumers: int = 4):
        self.name = name
        self.num_consumers = num_consumers
        self._buffer = None
        self._consumers = []
        self._running = mp.Value('b', False)
        self._stats = mp.Manager().dict({
            'produced': 0,
            'consumed': [0] * num_consumers
        })
        
        # 每个消费者的读取位置（独立）
        self._consumer_positions = [mp.Value('Q', 0) for _ in range(num_consumers)]
        
        # 创建共享内存缓冲区
        self._buffer = SharedMemoryRingBuffer(
            name=name, 
            slot_count=slot_count,
            slot_data_size=slot_data_size,
            create=True
        )
        self.slot_count = slot_count
        
    def start_consumers(self, process_func):
        """
        启动多个消费者进程
        
        Args:
            process_func: 处理函数，签名为 func(consumer_id, timestamp, data)
        """
        self._running.value = True
        
        for i in range(self.num_consumers):
            p = Process(
                target=self._consumer_worker,
                args=(i, process_func, self.name, self.slot_count, 
                      self._consumer_positions[i], self._running, self._stats)
            )
            p.start()
            self._consumers.append(p)
            
    @staticmethod
    def _consumer_worker(consumer_id, process_func, shm_name, slot_count,
                         read_pos_value, running, stats):
        """消费者进程工作函数"""
        import json
        
        # 连接共享内存
        buffer = SharedMemoryRingBuffer(
            name=shm_name,
            slot_count=slot_count,
            create=False
        )
        
        print(f"[Consumer-{consumer_id}] Started")
        
        while running.value:
            # 获取当前消费者的读取位置
            my_read_pos = read_pos_value.value
            write_pos = buffer._get_write_pos()
            
            if my_read_pos == write_pos:
                time.sleep(0.001)  # 无数据，短暂等待
                continue
                
            # 分片消费：consumer_id 决定处理哪些槽位
            # 简单策略：consumer_i 处理 slot % num_consumers == i 的数据
            slot_offset = buffer._get_slot_offset(my_read_pos)
            
            if buffer._shm.buf[slot_offset] == 1:  # 数据有效
                # 读取数据
                timestamp = struct.unpack('Q', buffer._shm.buf[slot_offset + 1:slot_offset + 9])[0]
                data_length = struct.unpack('I', buffer._shm.buf[slot_offset + 9:slot_offset + 13])[0]
                data_offset = slot_offset + buffer.SLOT_HEADER_SIZE
                data = bytes(buffer._shm.buf[data_offset:data_offset + data_length])
                
                try:
                    # 处理数据
                    data_dict = json.loads(data.decode('utf-8'))
                    process_func(consumer_id, timestamp, data_dict)
                    stats['consumed'][consumer_id] += 1
                except Exception as e:
                    print(f"[Consumer-{consumer_id}] Error: {e}")
                    
            # 移动读取位置
            read_pos_value.value = (my_read_pos + 1) % slot_count
            
        buffer.close()
        print(f"[Consumer-{consumer_id}] Stopped")
        
    def callback(self, data: dict):
        """
        生产者回调 - 直接写入共享内存
        
        这个函数设计为极快，可以直接用于 xtdata 回调
        """
        import json
        data_bytes = json.dumps(data).encode('utf-8')
        self._buffer.put(data_bytes)
        self._stats['produced'] += 1
        
    def stop(self):
        """停止所有消费者"""
        self._running.value = False
        for p in self._consumers:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
                
    def get_stats(self):
        """获取统计信息"""
        return dict(self._stats)
    
    def cleanup(self):
        """清理资源"""
        self.stop()
        self._buffer.unlink()


# ============================================================================
# 测试示例
# ============================================================================

def cpu_intensive_process(consumer_id, timestamp, data):
    """模拟CPU密集型处理"""
    # 模拟复杂计算
    result = sum(i * i for i in range(1000))
    
    recv_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    stock = data.get('stock', 'N/A')
    
    # 只打印部分信息避免刷屏
    if data.get('index', 0) % 100 == 0:
        print(f"[Consumer-{consumer_id}] {recv_time} 处理: {stock}, 结果: {result}")


def test_shared_memory_buffer():
    """测试共享内存环形缓冲区"""
    print("=" * 70)
    print("共享内存环形缓冲区 + 多进程消费者 测试")
    print("=" * 70)
    
    # 创建缓冲区，4个消费者进程
    buffer = MultiConsumerSharedBuffer(
        name="test_level2_buffer",
        slot_count=100000,
        slot_data_size=512,
        num_consumers=4
    )
    
    # 启动消费者进程
    buffer.start_consumers(cpu_intensive_process)
    
    # 模拟生产者（xtdata回调）
    print("\n开始生产数据...")
    start_time = time.time()
    
    DATA_COUNT = 10000
    for i in range(DATA_COUNT):
        mock_data = {
            "index": i,
            "stock": f"00000{i % 10}.SZ",
            "price": 10.0 + (i % 100) * 0.01,
            "volume": 1000 + i,
            "timestamp": time.time()
        }
        buffer.callback(mock_data)
        
    callback_time = time.time() - start_time
    
    print(f"\n生产 {DATA_COUNT} 条数据耗时: {callback_time*1000:.2f}ms")
    print(f"平均每次回调: {callback_time/DATA_COUNT*1000000:.2f}μs")
    
    # 等待消费完成
    time.sleep(2)
    
    stats = buffer.get_stats()
    print(f"\n统计信息: {stats}")
    
    # 清理
    buffer.cleanup()
    print("\n测试完成！")


def compare_with_queue():
    """对比多种方案的性能"""
    print("\n" + "=" * 70)
    print("性能对比: 多种写入方案")
    print("=" * 70)
    
    import json
    from multiprocessing import Queue as MPQueue
    from queue import Queue
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    
    DATA_COUNT = 10000
    test_data = {"stock": "000001.SZ", "price": 10.5, "volume": 1000}
    
    # 预序列化数据
    test_data_bytes = json.dumps(test_data).encode('utf-8')
    
    results = []
    
    # 测试1: 共享内存 (预序列化bytes)
    shm_buffer = SharedMemoryRingBuffer("perf_test", slot_count=100000, create=True)
    start = time.time()
    for i in range(DATA_COUNT):
        shm_buffer.put(test_data_bytes)
    shm_time_raw = time.time() - start
    shm_buffer.unlink()
    results.append(("共享内存(预序列化)", shm_time_raw))
    
    # 测试2: 共享内存 (含JSON序列化)
    shm_buffer2 = SharedMemoryRingBuffer("perf_test2", slot_count=100000, create=True)
    start = time.time()
    for i in range(DATA_COUNT):
        shm_buffer2.put(json.dumps(test_data).encode('utf-8'))
    shm_time_json = time.time() - start
    shm_buffer2.unlink()
    results.append(("共享内存(含JSON)", shm_time_json))
    
    # 测试3: multiprocessing.Queue
    mp_queue = MPQueue()
    start = time.time()
    for i in range(DATA_COUNT):
        mp_queue.put(test_data)
    mp_time = time.time() - start
    results.append(("multiprocessing.Queue", mp_time))
    
    # 测试4: queue.Queue (线程安全)
    normal_queue = Queue()
    start = time.time()
    for i in range(DATA_COUNT):
        normal_queue.put(test_data)
    queue_time = time.time() - start
    results.append(("queue.Queue", queue_time))
    
    # 测试5: collections.deque
    dq = deque(maxlen=100000)
    start = time.time()
    for i in range(DATA_COUNT):
        dq.append(test_data)
    deque_time = time.time() - start
    results.append(("collections.deque", deque_time))
    
    # 测试6: ThreadPoolExecutor.submit
    executor = ThreadPoolExecutor(max_workers=10)
    def dummy(x): pass
    start = time.time()
    for i in range(DATA_COUNT):
        executor.submit(dummy, test_data)
    submit_time = time.time() - start
    executor.shutdown(wait=False)
    results.append(("ThreadPoolExecutor.submit", submit_time))
    
    # 测试7: 纯数组赋值
    arr = [None] * 100000
    start = time.time()
    for i in range(DATA_COUNT):
        arr[i % 100000] = test_data
    array_time = time.time() - start
    results.append(("纯数组赋值", array_time))
    
    # 输出结果
    print(f"\n{DATA_COUNT} 次写入:")
    for name, t in results:
        print(f"  {name}: {t*1000:.2f}ms ({t/DATA_COUNT*1000000:.8f}μs/次)")
    
    print("\n分析:")
    print(f"  JSON序列化开销: {(shm_time_json - shm_time_raw)*1000:.8f}ms")
    print(f"  最快方案: {min(results, key=lambda x: x[1])[0]}")


if __name__ == "__main__":
    # 运行性能对比
    compare_with_queue()
    
    print("\n")
    
    # 运行完整测试
    test_shared_memory_buffer()