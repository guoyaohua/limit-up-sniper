"""
多线程消费者工作器 - 优化版

在单个进程内使用多线程处理不同类型的数据
每种数据类型由专门的线程池处理
"""

import logging
import time
import threading
from typing import Optional, Dict, List
from queue import Empty
from level2.buffers.deque_buffer import DequeBuffer, DataPacket
from level2.calculators import CapitalFlowCalculator, SealAmountCalculator

logger = logging.getLogger(__name__)


class ThreadedConsumer:
    """
    多线程消费者 - 处理单一类型的数据
    
    特点:
    - 专门处理一种数据类型（quote/order/trans）
    - 多个线程并行消费同一个deque
    - 无锁设计，利用deque的线程安全特性
    """
    
    def __init__(
        self,
        consumer_id: int,
        data_type: str,
        data_buffer: DequeBuffer,
        calculators: Dict,
        num_threads: int = 2
    ):
        """
        初始化线程消费者
        
        Args:
            consumer_id: 消费者ID（用于日志）
            data_type: 数据类型 ('quote', 'order', 'trans')
            data_buffer: 数据缓冲区（deque）
            calculators: 计算器字典 {'seal': SealAmountCalculator, 'flow': ...}
            num_threads: 线程数量
        """
        self.consumer_id = consumer_id
        self.data_type = data_type
        self.data_buffer = data_buffer
        self.calculators = calculators
        self.num_threads = num_threads
        
        # 线程控制
        self.running = False
        self.threads: List[threading.Thread] = []
        
        # 统计信息
        self.stats = {
            'processed': 0,
            'errors': 0,
            'empty_polls': 0
        }
        self.stats_lock = threading.Lock()
    
    def start(self):
        """启动所有消费者线程"""
        self.running = True
        
        for i in range(self.num_threads):
            thread = threading.Thread(
                target=self._consume_loop,
                args=(i,),
                name=f'Consumer-{self.consumer_id}-{self.data_type}-T{i}',
                daemon=True
            )
            thread.start()
            self.threads.append(thread)
        
        logger.info(
            f"[Consumer-{self.consumer_id}] Started {self.num_threads} threads "
            f"for {self.data_type}"
        )
    
    def stop(self, timeout: float = 5.0):
        """停止所有消费者线程"""
        self.running = False
        
        # 等待所有线程结束
        for thread in self.threads:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    f"[Consumer-{self.consumer_id}] Thread {thread.name} "
                    f"did not stop in time"
                )
        
        logger.info(
            f"[Consumer-{self.consumer_id}] Stopped {self.data_type} consumer. "
            f"Stats: {self.stats}"
        )
    
    def _consume_loop(self, thread_id: int):
        """
        消费循环 - 在独立线程中运行
        
        Args:
            thread_id: 线程ID
        """
        logger.debug(
            f"[Consumer-{self.consumer_id}-T{thread_id}] "
            f"Thread started for {self.data_type}"
        )
        
        empty_count = 0
        
        while self.running:
            try:
                # 从deque获取数据（线程安全）
                packet = self.data_buffer.get()
                
                if packet is None:
                    # 队列为空
                    empty_count += 1
                    with self.stats_lock:
                        self.stats['empty_polls'] += 1
                    
                    # 自适应睡眠时间
                    if empty_count < 10:
                        time.sleep(0.0001)  # 100μs
                    elif empty_count < 100:
                        time.sleep(0.001)   # 1ms
                    else:
                        time.sleep(0.01)    # 10ms
                    continue
                
                # 重置空计数
                empty_count = 0
                
                # 处理数据
                self._process_packet(packet)
                
                with self.stats_lock:
                    self.stats['processed'] += 1
            
            except Exception as e:
                logger.error(
                    f"[Consumer-{self.consumer_id}-T{thread_id}] "
                    f"Error in consume loop: {e}",
                    exc_info=True
                )
                with self.stats_lock:
                    self.stats['errors'] += 1
                time.sleep(0.1)  # 错误后休息
        
        logger.debug(
            f"[Consumer-{self.consumer_id}-T{thread_id}] "
            f"Thread stopped for {self.data_type}"
        )
    
    def _process_packet(self, packet: DataPacket):
        """
        处理数据包
        
        Args:
            packet: 数据包
        """
        stock_code = packet.stock_code
        data = packet.data
        
        try:
            if self.data_type == 'quote':
                # l2quote数据处理（由 flow_calc 统一处理；若开启板上能力，会自动同步 seal_calc）
                if 'flow' in self.calculators:
                    self.calculators['flow'].on_l2quote(stock_code, data)
                else:
                    self.calculators['seal'].on_l2quote(stock_code, data)
            
            elif self.data_type == 'order':
                # l2order数据处理
                if 'flow' in self.calculators:
                    self.calculators['flow'].on_l2order(stock_code, data)
            
            elif self.data_type == 'trans':
                # l2transaction数据处理
                if 'flow' in self.calculators:
                    self.calculators['flow'].on_l2transaction(stock_code, data)
        
        except Exception as e:
            logger.error(
                f"[Consumer-{self.consumer_id}] Error processing {self.data_type} "
                f"for {stock_code}: {e}"
            )
            raise
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self.stats_lock:
            return self.stats.copy()


class MultiThreadedConsumerPool:
    """
    多线程消费者池 - 管理3种数据类型的消费者
    
    在单个进程内运行，为每种数据类型创建专门的线程池
    """
    
    def __init__(
        self,
        partition_id: int,
        quote_buffer: DequeBuffer,
        order_buffer: DequeBuffer,
        trans_buffer: DequeBuffer,
        enable_limit_up_flow: bool = True,
        num_quote_threads: int = 2,
        num_order_threads: int = 4,
        num_trans_threads: int = 2
    ):
        """
        初始化多线程消费者池
        
        Args:
            partition_id: 分区ID
            quote_buffer: quote数据缓冲区
            order_buffer: order数据缓冲区
            trans_buffer: transaction数据缓冲区
            enable_limit_up_flow: 是否启用板上资金流向
            num_quote_threads: quote线程数（默认2）
            num_order_threads: order线程数（默认4，因为数据量最大）
            num_trans_threads: transaction线程数（默认2）
        """
        self.partition_id = partition_id
        self.enable_limit_up_flow = enable_limit_up_flow
        
        # 初始化计算器
        self.seal_calc = SealAmountCalculator()

        if enable_limit_up_flow:
            # 仅使用 CapitalFlowCalculator，但开启“板上资金流向”能力（复用 seal_calc 判定涨停状态）
            self.flow_calc = CapitalFlowCalculator(
                seal_calculator=self.seal_calc,
                enable_limit_up_flow=True,
            )
        else:
            self.flow_calc = CapitalFlowCalculator()
        
        # 计算器字典
        calculators = {
            'seal': self.seal_calc,
            'flow': self.flow_calc
        }
        
        # 创建三个消费者（每种数据类型一个）
        self.quote_consumer = ThreadedConsumer(
            consumer_id=partition_id,
            data_type='quote',
            data_buffer=quote_buffer,
            calculators=calculators,
            num_threads=num_quote_threads
        )
        
        self.order_consumer = ThreadedConsumer(
            consumer_id=partition_id,
            data_type='order',
            data_buffer=order_buffer,
            calculators=calculators,
            num_threads=num_order_threads
        )
        
        self.trans_consumer = ThreadedConsumer(
            consumer_id=partition_id,
            data_type='trans',
            data_buffer=trans_buffer,
            calculators=calculators,
            num_threads=num_trans_threads
        )
        
        self.consumers = [
            self.quote_consumer,
            self.order_consumer,
            self.trans_consumer
        ]
        
        # 运行状态
        self.running = False
        
        # 统计线程
        self.stats_thread: Optional[threading.Thread] = None
        self.last_stats_time = time.time()
    
    def start(self):
        """启动所有消费者"""
        logger.info(f"[Partition-{self.partition_id}] Starting consumer pool...")
        
        self.running = True
        
        # 启动所有消费者
        for consumer in self.consumers:
            consumer.start()
        
        # 启动统计线程
        self.stats_thread = threading.Thread(
            target=self._stats_loop,
            name=f'Stats-{self.partition_id}',
            daemon=True
        )
        self.stats_thread.start()
        
        total_threads = sum(c.num_threads for c in self.consumers)
        logger.info(
            f"[Partition-{self.partition_id}] Consumer pool started with "
            f"{total_threads} threads"
        )
    
    def stop(self, timeout: float = 5.0):
        """停止所有消费者"""
        logger.info(f"[Partition-{self.partition_id}] Stopping consumer pool...")
        
        self.running = False
        
        # 停止所有消费者
        for consumer in self.consumers:
            consumer.stop(timeout=timeout)
        
        # 停止统计线程
        if self.stats_thread:
            self.stats_thread.join(timeout=1.0)
        
        logger.info(f"[Partition-{self.partition_id}] Consumer pool stopped")
    
    def _stats_loop(self):
        """统计循环 - 定期输出统计信息"""
        while self.running:
            try:
                time.sleep(30)  # 每30秒输出一次
                
                if not self.running:
                    break
                
                stats = self.get_all_stats()
                logger.info(
                    f"[Partition-{self.partition_id}] Stats:\n"
                    f"  Quote: processed={stats['quote']['processed']}, "
                    f"errors={stats['quote']['errors']}, "
                    f"empty={stats['quote']['empty_polls']}\n"
                    f"  Order: processed={stats['order']['processed']}, "
                    f"errors={stats['order']['errors']}, "
                    f"empty={stats['order']['empty_polls']}\n"
                    f"  Trans: processed={stats['trans']['processed']}, "
                    f"errors={stats['trans']['errors']}, "
                    f"empty={stats['trans']['empty_polls']}"
                )
            
            except Exception as e:
                logger.error(f"Error in stats loop: {e}")
    
    def get_all_stats(self) -> Dict:
        """获取所有消费者的统计信息"""
        return {
            'quote': self.quote_consumer.get_stats(),
            'order': self.order_consumer.get_stats(),
            'trans': self.trans_consumer.get_stats()
        }
    
    def get_calculators(self) -> Dict:
        """
        获取计算器（用于结果聚合）
        
        Returns:
            {'seal': SealAmountCalculator, 'flow': FlowCalculator}
        """
        return {
            'seal': self.seal_calc,
            'flow': self.flow_calc
        }


def benchmark_thread_scalability():
    """
    性能测试：线程数量对性能的影响
    """
    from level2.buffers.deque_buffer import DequeBuffer, DequeConfig, DataPacket
    
    print("=" * 70)
    print("Thread Scalability Benchmark")
    print("=" * 70)
    
    num_items = 50000
    
    # 测试不同线程数
    for num_threads in [1, 2, 4, 8]:
        print(f"\nTesting with {num_threads} thread(s)...")
        
        # 创建测试数据缓冲区
        test_buffer = DequeBuffer(DequeConfig(name="test", maxlen=100000))
        
        # 填充测试数据 - 使用更完整的数据格式
        for i in range(num_items):
            packet = DataPacket(
                stock_code=f"60000{i%10}.SH",
                data={
                    'bidPrice': [10.0 + i * 0.01] * 5,
                    'askPrice': [10.1 + i * 0.01] * 5,
                    'bidVol': [1000] * 5,
                    'askVol': [1000] * 5,
                    'lastPrice': 10.05 + i * 0.01,
                    'volume': 1000 + i,
                    'amount': (10.0 + i * 0.01) * (1000 + i)
                }
            )
            test_buffer.put(packet)
        
        # 创建消费者 - 使用简单的计算器避免初始化问题
        # 这里只测试吞吐量，不关心计算结果
        class DummyCalculator:
            """虚拟计算器用于性能测试"""
            def on_l2quote(self, stock_code, data):
                pass  # 什么都不做，只测试队列性能
            def on_l2order(self, stock_code, data):
                pass
            def on_l2transaction(self, stock_code, data):
                pass
        
        calculators = {
            'seal': SealAmountCalculator(),
            'flow': CapitalFlowCalculator()
        }
        
        consumer = ThreadedConsumer(
            consumer_id=0,
            data_type='quote',
            data_buffer=test_buffer,
            calculators=calculators,
            num_threads=num_threads
        )
        
        # 启动并测试
        start_time = time.time()
        consumer.start()
        
        # 等待处理完成
        while test_buffer.qsize() > 0:
            time.sleep(0.01)
        
        # 再等一小段时间确保所有数据都处理完
        time.sleep(0.1)
        
        elapsed = time.time() - start_time
        consumer.stop(timeout=1.0)
        
        stats = consumer.get_stats()
        throughput = stats['processed'] / elapsed if elapsed > 0 else 0
        
        print(f"  Elapsed: {elapsed:.2f}s")
        print(f"  Processed: {stats['processed']}")
        print(f"  Throughput: {throughput:.0f} items/sec")
        if num_threads == 1:
            baseline_throughput = throughput
        else:
            speedup = throughput / baseline_throughput if baseline_throughput > 0 else 0
            print(f"  Speedup vs 1 thread: {speedup:.2f}x")


if __name__ == '__main__':
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s'
    )
    
    # 运行基准测试
    benchmark_thread_scalability()
    
    print("ThreadedConsumer module loaded successfully")
