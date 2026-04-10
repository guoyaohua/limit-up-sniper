"""
统一消费者工作器 - 重构版

新架构特点:
1. 使用统一的 Level2Calculator 处理所有数据类型
2. 每个线程处理一部分股票，并订阅自己负责的股票
3. 每个线程有独立的缓冲区和回调函数，无运行时股票查找
4. 最小化数据传输路径，回调直接写入本地缓冲区
"""

import logging
import time
import threading
from typing import Optional, Dict, List, Set
from collections import deque
from dataclasses import dataclass
from level2.calculators import Level2Calculator

logger = logging.getLogger(__name__)

@dataclass
class UnifiedDataPacket:
    """统一数据包 - 封装所有类型的回调数据"""
    data_type: str  # 'quote', 'order', 'trans'
    stock_code: str
    data: dict

class ThreadLocalBuffer:
    """
    线程本地缓冲区
    
    每个消费者线程有独立的缓冲区，避免数据竞争和丢失
    """
    
    def __init__(self, name: str, maxlen: int = 50_000_000):
        self.name = name
        self._queue = deque(maxlen=maxlen)
        self.maxlen = maxlen
        self._total_appended = 0
        self._overflow_count = 0
        self._callback_counts = {'quote': 0, 'order': 0, 'trans': 0}
    
    def put(self, packet: UnifiedDataPacket) -> bool:
        """写入数据包"""
        if len(self._queue) >= self.maxlen - 1:
            self._overflow_count += 1
        self._queue.append(packet)
        self._total_appended += 1
        return True
    
    def get(self) -> Optional[UnifiedDataPacket]:
        """读取数据包"""
        try:
            return self._queue.popleft()
        except IndexError:
            return None
    
    def qsize(self) -> int:
        return len(self._queue)
    
    def get_stats(self) -> Dict:
        return {
            'name': self.name,
            'qsize': len(self._queue),
            'maxlen': self.maxlen,
            'total_appended': self._total_appended,
            'overflow_count': self._overflow_count,
            'callbacks': self._callback_counts.copy()
        }
    
    def clear(self):
        self._queue.clear()
    
    # ========== 回调函数（直接写入，无任何查找） ==========
    
    def on_l2quote_callback(self, datas: dict):
        """l2quote 回调 - 直接写入本缓冲区"""
        self._callback_counts['quote'] += 1
        for stock_code, quote_data in datas.items():
            self._queue.append(UnifiedDataPacket('quote', stock_code, quote_data))
            self._total_appended += 1
    
    def on_l2order_callback(self, datas: dict):
        """l2order 回调 - 直接写入本缓冲区"""
        self._callback_counts['order'] += 1
        for stock_code, order_data in datas.items():
            self._queue.append(UnifiedDataPacket('order', stock_code, order_data))
            self._total_appended += 1
    
    def on_l2transaction_callback(self, datas: dict):
        """l2transaction 回调 - 直接写入本缓冲区"""
        self._callback_counts['trans'] += 1
        for stock_code, trans_data in datas.items():
            self._queue.append(UnifiedDataPacket('trans', stock_code, trans_data))
            self._total_appended += 1

class UnifiedConsumerThread:
    """
    统一消费者线程
    
    每个线程:
    1. 负责一部分股票
    2. 有独立的本地缓冲区
    3. 直接订阅自己负责的股票（回调直接写入本地缓冲区）
    """
    
    def __init__(
        self,
        thread_id: int,
        partition_id: int,
        stock_list: List[str],
        stock_info: Optional[Dict[str, float]] = None,
        enable_limit_up_flow: bool = True
    ):
        """
        初始化消费者线程
        
        Args:
            thread_id: 线程ID
            partition_id: 分区ID（用于日志）
            stock_list: 该线程负责处理的股票列表
            stock_info: 股票涨停价信息
            enable_limit_up_flow: 是否启用板上资金流向
        """
        self.thread_id = thread_id
        self.partition_id = partition_id
        self.stock_list = stock_list
        self.stock_set = set(stock_list)
        
        # 线程本地缓冲区
        self.local_buffer = ThreadLocalBuffer(name=f"P{partition_id}-T{thread_id}")
        
        # 创建统一计算器
        self.calculator = Level2Calculator(
            stock_info=stock_info,
            enable_limit_up_flow=enable_limit_up_flow
        )
        
        # XTDATA 订阅ID
        self.subscribe_ids = []
        
        # 线程控制
        self.running = False
        self.thread: Optional[threading.Thread] = None
        
        # 统计信息
        self.stats = {
            'processed': 0,
            'errors': 0,
            'empty_polls': 0,
            'quote_count': 0,
            'order_count': 0,
            'trans_count': 0
        }
        self.stats_lock = threading.Lock()
    
    def subscribe_xtdata(self, start_date: str = None):
        """
        订阅 XTDATA Level2 数据
        
        注意: subscribe_quote 只能单股订阅，需要循环对所有股票执行订阅
        
        Args:
            start_date: 开始日期，格式 'YYYYMMDD'，默认为今天
        """
        try:
            from xtquant import xtdata
        except ImportError:
            logger.warning(
                f"[P{self.partition_id}-T{self.thread_id}] "
                "xtdata module not found. Running in SIMULATION mode."
            )
            return
        
        if start_date is None:
            from datetime import datetime
            start_date = datetime.now().strftime('%Y%m%d')
        
        start_time = start_date + '000000'
        
        # 循环对每只股票进行订阅（subscribe_quote 只能单股订阅）
        for stock_code in self.stock_list:
            # 订阅 l2quote
            sub_id = xtdata.subscribe_quote(
                stock_code,
                period='l2quote',
                start_time=start_time,
                count=0,
                callback=self.local_buffer.on_l2quote_callback
            )
            self.subscribe_ids.append(sub_id)
            
            # 订阅 l2order
            sub_id = xtdata.subscribe_quote(
                stock_code,
                period='l2order',
                start_time=start_time,
                count=0,
                callback=self.local_buffer.on_l2order_callback
            )
            self.subscribe_ids.append(sub_id)
            
            # 订阅 l2transaction
            sub_id = xtdata.subscribe_quote(
                stock_code,
                period='l2transaction',
                start_time=start_time,
                count=0,
                callback=self.local_buffer.on_l2transaction_callback
            )
            self.subscribe_ids.append(sub_id)
        
        logger.info(
            f"[P{self.partition_id}-T{self.thread_id}] Subscribed {len(self.stock_list)} stocks, "
            f"{len(self.subscribe_ids)} subscriptions"
        )
    
    def unsubscribe_xtdata(self):
        """取消订阅"""
        if not self.subscribe_ids:
            return
        try:
            from xtquant import xtdata
            for sub_id in self.subscribe_ids:
                xtdata.unsubscribe_quote(sub_id)
            logger.info(f"[P{self.partition_id}-T{self.thread_id}] Unsubscribed all")
        except:
            pass
        self.subscribe_ids.clear()
    
    def start(self):
        """启动消费者线程"""
        self.running = True
        self.thread = threading.Thread(
            target=self._consume_loop,
            name=f'Consumer-P{self.partition_id}-T{self.thread_id}',
            daemon=True
        )
        self.thread.start()
        logger.info(
            f"[P{self.partition_id}-T{self.thread_id}] Started, "
            f"handling {len(self.stock_list)} stocks"
        )
    
    def stop(self, timeout: float = 5.0):
        """停止消费者线程"""
        self.running = False
        self.unsubscribe_xtdata()
        if self.thread:
            self.thread.join(timeout=timeout)
            if self.thread.is_alive():
                logger.warning(f"[P{self.partition_id}-T{self.thread_id}] Thread did not stop")
        logger.info(f"[P{self.partition_id}-T{self.thread_id}] Stopped. Stats: {self.stats}")
    
    def _consume_loop(self):
        """消费循环"""
        empty_count = 0
        
        while self.running:
            try:
                packet = self.local_buffer.get()
                
                if packet is None:
                    empty_count += 1
                    with self.stats_lock:
                        self.stats['empty_polls'] += 1
                    # 自适应睡眠
                    if empty_count < 10:
                        time.sleep(0.0001)
                    elif empty_count < 100:
                        time.sleep(0.001)
                    else:
                        time.sleep(0.01)
                    continue
                
                empty_count = 0
                self._process_packet(packet)
                with self.stats_lock:
                    self.stats['processed'] += 1
            
            except Exception as e:
                logger.error(f"[P{self.partition_id}-T{self.thread_id}] Error: {e}", exc_info=True)
                with self.stats_lock:
                    self.stats['errors'] += 1
                time.sleep(0.1)
    
    def _process_packet(self, packet: UnifiedDataPacket):
        """处理数据包"""
        if packet.data_type == 'quote':
            self.calculator.on_l2quote(packet.stock_code, packet.data)
            with self.stats_lock:
                self.stats['quote_count'] += 1
        elif packet.data_type == 'order':
            self.calculator.on_l2order(packet.stock_code, packet.data)
            with self.stats_lock:
                self.stats['order_count'] += 1
        elif packet.data_type == 'trans':
            self.calculator.on_l2transaction(packet.stock_code, packet.data)
            with self.stats_lock:
                self.stats['trans_count'] += 1
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self.stats_lock:
            stats = self.stats.copy()
        stats['buffer'] = self.local_buffer.get_stats()
        return stats
    
    def get_calculator(self) -> Level2Calculator:
        return self.calculator

class UnifiedConsumerPool:
    """
    统一消费者池
    
    管理多个消费者线程，每个线程独立订阅自己负责的股票
    """
    
    def __init__(
        self,
        partition_id: int,
        stock_list: List[str],
        num_threads: int = 2,
        stock_info: Optional[Dict[str, float]] = None,
        enable_limit_up_flow: bool = True
    ):
        self.partition_id = partition_id
        self.stock_list = stock_list
        self.num_threads = num_threads
        self.enable_limit_up_flow = enable_limit_up_flow
        
        # 将股票分配给各个线程
        self.stock_assignments = self._assign_stocks_to_threads(stock_list, num_threads)
        
        # 创建消费者线程
        self.consumers: List[UnifiedConsumerThread] = []
        for i, thread_stocks in enumerate(self.stock_assignments):
            thread_stock_info = None
            if stock_info:
                thread_stock_info = {
                    code: price for code, price in stock_info.items()
                    if code in thread_stocks
                }
            
            consumer = UnifiedConsumerThread(
                thread_id=i,
                partition_id=partition_id,
                stock_list=list(thread_stocks),
                stock_info=thread_stock_info,
                enable_limit_up_flow=enable_limit_up_flow
            )
            self.consumers.append(consumer)
        
        self.running = False
        self.stats_thread: Optional[threading.Thread] = None
    
    def _assign_stocks_to_threads(self, stock_list: List[str], num_threads: int) -> List[Set[str]]:
        """使用哈希将股票分配给各个线程"""
        assignments = [set() for _ in range(num_threads)]
        for stock in stock_list:
            thread_id = hash(stock) % num_threads
            assignments[thread_id].add(stock)
        logger.info(f"[Partition-{self.partition_id}] Stock assignment:")
        for i, stocks in enumerate(assignments):
            logger.info(f"  Thread {i}: {len(stocks)} stocks")
        return assignments
    
    def start(self, subscribe: bool = True):
        """
        启动所有消费者
        
        Args:
            subscribe: 是否自动订阅 XTDATA，默认 True
        """
        logger.info(f"[Partition-{self.partition_id}] Starting consumer pool...")
        self.running = True
        
        for consumer in self.consumers:
            if subscribe:
                consumer.subscribe_xtdata()
            consumer.start()
        
        self.stats_thread = threading.Thread(
            target=self._stats_loop,
            name=f'Stats-P{self.partition_id}',
            daemon=True
        )
        self.stats_thread.start()
        
        logger.info(f"[Partition-{self.partition_id}] Consumer pool started with {self.num_threads} threads")
    
    def stop(self, timeout: float = 5.0):
        """停止所有消费者"""
        logger.info(f"[Partition-{self.partition_id}] Stopping consumer pool...")
        self.running = False
        for consumer in self.consumers:
            consumer.stop(timeout=timeout)
        if self.stats_thread:
            self.stats_thread.join(timeout=1.0)
        logger.info(f"[Partition-{self.partition_id}] Consumer pool stopped")
    
    def _stats_loop(self):
        """统计循环"""
        while self.running:
            try:
                time.sleep(30)
                if not self.running:
                    break
                self._print_stats()
            except Exception as e:
                logger.error(f"Error in stats loop: {e}")
    
    def _print_stats(self):
        """打印统计信息"""
        total = {'processed': 0, 'errors': 0, 'quote': 0, 'order': 0, 'trans': 0, 'qsize': 0}
        for c in self.consumers:
            s = c.get_stats()
            total['processed'] += s['processed']
            total['errors'] += s['errors']
            total['quote'] += s['quote_count']
            total['order'] += s['order_count']
            total['trans'] += s['trans_count']
            total['qsize'] += s['buffer']['qsize']
        logger.info(
            f"[Partition-{self.partition_id}] Stats: "
            f"processed={total['processed']}, errors={total['errors']}, "
            f"quote={total['quote']}, order={total['order']}, trans={total['trans']}, "
            f"qsize={total['qsize']}"
        )
    
    def get_all_stats(self) -> Dict:
        return {f'thread_{i}': c.get_stats() for i, c in enumerate(self.consumers)}
    
    def get_calculators(self) -> Dict[int, Level2Calculator]:
        return {i: c.get_calculator() for i, c in enumerate(self.consumers)}
    
    def get_all_flow_stats(self) -> Dict:
        all_stats = {}
        for c in self.consumers:
            all_stats.update(c.get_calculator().get_all_stats())
        return all_stats
    
    def get_all_limit_up_stats(self) -> Dict:
        all_stats = {}
        for c in self.consumers:
            all_stats.update(c.get_calculator().get_all_limit_up_stats())
        return all_stats
    
    def get_all_seal_info(self) -> Dict:
        all_info = {}
        for c in self.consumers:
            calc = c.get_calculator()
            if calc.seal_calc:
                all_info.update(calc.seal_calc.seal_info)
        return all_info

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s')
    
    print("Testing UnifiedConsumerPool...")
    test_stocks = [f"60000{i}.SH" for i in range(100)]
    
    pool = UnifiedConsumerPool(partition_id=0, stock_list=test_stocks, num_threads=2, enable_limit_up_flow=True)
    pool.start(subscribe=False)  # 测试时不订阅
    
    # 模拟数据直接写入各线程的缓冲区
    for i in range(1000):
        stock = test_stocks[i % len(test_stocks)]
        thread_id = hash(stock) % 2
        pool.consumers[thread_id].local_buffer.on_l2quote_callback({
            stock: {'bidPrice': [10.0]*5, 'bidVol': [1000]*5, 'askPrice': [10.1]*5, 'askVol': [1000]*5, 'lastPrice': 10.05, 'time': 93000000}
        })
    
    time.sleep(2)
    total = sum(s['processed'] for s in pool.get_all_stats().values())
    print(f"Total processed: {total} / 1000")
    assert total == 1000, f"Data loss! Expected 1000, got {total}"
    pool.stop()
    print("Test completed!")
