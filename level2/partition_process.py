"""
分区进程 - 重构版 Level 2 数据处理

新架构特点:
- 每个分区对股票再分块给多个线程
- 每个线程独立订阅自己负责的股票
- 回调直接写入线程本地缓冲区，无中间转发层
- 最小化数据传输路径，高性能设计
"""

import logging
import time
import signal
import sys
from datetime import datetime
from typing import List, Optional, Dict
from multiprocessing import Process, Value
from level2.consumers.unified_worker import UnifiedConsumerPool

logger = logging.getLogger(__name__)

class PartitionProcess:
    """
    分区进程 - 处理股票子集的 Level 2 数据
    
    新架构:
    - 每个线程独立订阅自己负责的股票
    - 回调直接写入线程本地缓冲区
    - 无中间转发层，高性能
    """
    
    def __init__(
        self,
        partition_id: int,
        stock_list: List[str],
        enable_limit_up_flow: bool = True,
        num_threads: int = 2,
        stock_info: Optional[Dict[str, float]] = None
    ):
        self.partition_id = partition_id
        self.stock_list = stock_list
        self.enable_limit_up_flow = enable_limit_up_flow
        self.num_threads = num_threads
        self.stock_info = stock_info
        
        self.consumer_pool: Optional[UnifiedConsumerPool] = None
        self.is_running = False
        
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        logger.info(f"[Partition-{self.partition_id}] Received signal {signum}, stopping...")
        self.stop()
        sys.exit(0)
    
    def start(self):
        """启动分区进程"""
        logger.info("=" * 70)
        logger.info(f"Partition Process {self.partition_id}")
        logger.info(f"Stocks: {len(self.stock_list)}, Threads: {self.num_threads}")
        logger.info(f"LimitUpFlow: {'Enabled' if self.enable_limit_up_flow else 'Disabled'}")
        logger.info("=" * 70)
        
        try:
            # 创建并启动消费者池（内部自动订阅）
            self.consumer_pool = UnifiedConsumerPool(
                partition_id=self.partition_id,
                stock_list=self.stock_list,
                num_threads=self.num_threads,
                stock_info=self.stock_info,
                enable_limit_up_flow=self.enable_limit_up_flow
            )
            self.consumer_pool.start(subscribe=True)
            
            self.is_running = True
            logger.info(f"[Partition-{self.partition_id}] Started successfully!")
            
            self._monitoring_loop()
        
        except Exception as e:
            logger.error(f"[Partition-{self.partition_id}] Failed: {e}", exc_info=True)
            self.stop()
            raise
    
    def _monitoring_loop(self):
        """监控循环"""
        last_stats_time = time.time()
        
        while self.is_running:
            try:
                time.sleep(1)
                if time.time() - last_stats_time > 60:
                    self._print_statistics()
                    last_stats_time = time.time()
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"[Partition-{self.partition_id}] Monitor error: {e}")
                time.sleep(5)
    
    def _print_statistics(self):
        """输出统计信息"""
        if not self.consumer_pool:
            return
        stats = self.consumer_pool.get_all_stats()
        total = sum(s['processed'] for s in stats.values())
        errors = sum(s['errors'] for s in stats.values())
        qsize = sum(s['buffer']['qsize'] for s in stats.values())
        logger.info(f"[Partition-{self.partition_id}] processed={total}, errors={errors}, qsize={qsize}")
    
    def stop(self):
        """停止分区进程"""
        if not self.is_running:
            return
        logger.info(f"[Partition-{self.partition_id}] Stopping...")
        self.is_running = False
        if self.consumer_pool:
            self.consumer_pool.stop()
        logger.info(f"[Partition-{self.partition_id}] Stopped")
    
    def get_results(self) -> Dict:
        if not self.consumer_pool:
            return {}
        return {
            'flow_stats': self.consumer_pool.get_all_flow_stats(),
            'limit_up_stats': self.consumer_pool.get_all_limit_up_stats(),
            'seal_info': self.consumer_pool.get_all_seal_info()
        }

def partition_process_worker(
    partition_id: int,
    stock_list: List[str],
    running_flag: Value,
    enable_limit_up_flow: bool = True,
    num_threads: int = 2,
    stock_info: Optional[Dict[str, float]] = None
):
    """分区进程工作函数"""
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s [P{partition_id}] %(levelname)s: %(message)s',
        handlers=[
            logging.FileHandler(f'level2_partition_{partition_id}_{datetime.now().strftime("%Y%m%d")}.log'),
            logging.StreamHandler()
        ]
    )
    
    partition = PartitionProcess(
        partition_id=partition_id,
        stock_list=stock_list,
        enable_limit_up_flow=enable_limit_up_flow,
        num_threads=num_threads,
        stock_info=stock_info
    )
    
    try:
        partition.start()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"[Partition-{partition_id}] Error: {e}", exc_info=True)
    finally:
        partition.stop()
        running_flag.value = False

def partition_stocks(stock_list: List[str], num_partitions: int) -> List[List[str]]:
    """将股票列表分成N个分区"""
    partitions = [[] for _ in range(num_partitions)]
    for stock in stock_list:
        partitions[hash(stock) % num_partitions].append(stock)
    logger.info("Stock Partitioning:")
    for i, p in enumerate(partitions):
        logger.info(f"  Partition {i}: {len(p)} stocks")
    return partitions

def create_partition_pool(
    stock_list: List[str],
    num_partitions: int = 16,
    enable_limit_up_flow: bool = True,
    num_threads: int = 2,
    stock_info: Optional[Dict[str, float]] = None
) -> tuple:
    """创建分区进程池"""
    partitions = partition_stocks(stock_list, num_partitions)
    
    processes = []
    running_flags = []
    
    for i, partition_stocks_list in enumerate(partitions):
        if not partition_stocks_list:
            continue
        
        running_flag = Value('b', True)
        running_flags.append(running_flag)
        
        partition_stock_info = None
        if stock_info:
            partition_stock_info = {k: v for k, v in stock_info.items() if k in partition_stocks_list}
        
        p = Process(
            target=partition_process_worker,
            args=(i, partition_stocks_list, running_flag, enable_limit_up_flow, num_threads, partition_stock_info),
            name=f'Partition-{i}'
        )
        p.start()
        processes.append(p)
        logger.info(f"Started partition {i}: {len(partition_stocks_list)} stocks, {num_threads} threads")
    
    return processes, running_flags

def stop_partition_pool(processes: List[Process], running_flags: List[Value], timeout: int = 10):
    """停止分区进程池"""
    logger.info("Stopping partition pool...")
    for flag in running_flags:
        flag.value = False
    for p in processes:
        p.join(timeout=timeout)
        if p.is_alive():
            logger.warning(f"Process {p.name} terminating...")
            p.terminate()
            p.join(timeout=2)
    logger.info("Partition pool stopped")

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    test_stocks = [f"60000{i}.SH" for i in range(100)]
    partitions = partition_stocks(test_stocks, num_partitions=8)
    for i, p in enumerate(partitions):
        print(f"Partition {i}: {len(p)} stocks")
