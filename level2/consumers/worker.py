"""
消费者进程工作器

从共享内存缓冲区读取Level 2数据并进行计算处理
"""

import logging
import time
import multiprocessing as mp
from multiprocessing import Process, Value
from typing import List, Optional
from level2.buffers.ring_buffer import SharedMemoryRingBuffer, BufferConfig
from level2.calculators import CapitalFlowCalculator, SealAmountCalculator

logger = logging.getLogger(__name__)


class ConsumerWorker:
    """
    消费者工作器
    
    从共享内存缓冲区读取数据，调用计算器处理
    """
    
    def __init__(self, worker_id: int, enable_limit_up_flow: bool = True):
        """
        初始化消费者
        
        Args:
            worker_id: 工作器ID
            enable_limit_up_flow: 是否启用板上资金流向计算
        """
        self.worker_id = worker_id
        self.enable_limit_up_flow = enable_limit_up_flow
        
        # 连接共享内存缓冲区
        self.l2quote_buffer = None
        self.l2order_buffer = None
        self.l2transaction_buffer = None
        
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
        
        # 统计
        self.stats = {
            'l2quote_processed': 0,
            'l2order_processed': 0,
            'l2transaction_processed': 0,
            'errors': 0
        }
    
    def connect_buffers(self):
        """连接共享内存缓冲区"""
        try:
            self.l2quote_buffer = SharedMemoryRingBuffer(
                BufferConfig.for_l2quote(), create=False
            )
            self.l2order_buffer = SharedMemoryRingBuffer(
                BufferConfig.for_l2order(), create=False
            )
            self.l2transaction_buffer = SharedMemoryRingBuffer(
                BufferConfig.for_l2transaction(), create=False
            )
            logger.info(f"[Worker-{self.worker_id}] Connected to shared memory buffers")
        except Exception as e:
            logger.error(f"[Worker-{self.worker_id}] Failed to connect buffers: {e}")
            raise
    
    def process_l2quote(self, data_packet: dict):
        """
        处理l2quote数据
        
        Args:
            data_packet: {'type': 'l2quote', 'stock_code': str, 'data': dict}
        """
        try:
            stock_code = data_packet['stock_code']
            quote_data = data_packet['data']

            if self.enable_limit_up_flow:
                # flow_calc 内部会先同步 seal_calc，再做板上统计/盘口缓存
                self.flow_calc.on_l2quote(stock_code, quote_data)
            else:
                # 未启用板上资金流向时：保持旧行为（seal 单独处理），同时给 flow_calc 缓存盘口用于清理
                self.seal_calc.on_l2quote(stock_code, quote_data)
                self.flow_calc.on_l2quote(stock_code, quote_data)

            self.stats['l2quote_processed'] += 1
        except Exception as e:
            logger.error(f"[Worker-{self.worker_id}] Error processing l2quote: {e}")
            self.stats['errors'] += 1
    
    def process_l2order(self, data_packet: dict):
        """
        处理l2order数据
        
        Args:
            data_packet: {'type': 'l2order', 'stock_code': str, 'data': dict}
        """
        try:
            stock_code = data_packet['stock_code']
            order_data = data_packet['data']
            
            # 资金流向计算器处理
            self.flow_calc.on_l2order(stock_code, order_data)
            
            self.stats['l2order_processed'] += 1
        except Exception as e:
            logger.error(f"[Worker-{self.worker_id}] Error processing l2order: {e}")
            self.stats['errors'] += 1
    
    def process_l2transaction(self, data_packet: dict):
        """
        处理l2transaction数据
        
        Args:
            data_packet: {'type': 'l2transaction', 'stock_code': str, 'data': dict}
        """
        try:
            stock_code = data_packet['stock_code']
            trans_data = data_packet['data']
            
            # 资金流向计算器处理
            self.flow_calc.on_l2transaction(stock_code, trans_data)
            
            self.stats['l2transaction_processed'] += 1
        except Exception as e:
            logger.error(f"[Worker-{self.worker_id}] Error processing l2transaction: {e}")
            self.stats['errors'] += 1
    
    def run(self, running_flag: Value):
        """
        运行消费者循环
        
        Args:
            running_flag: 运行标志（共享变量）
        """
        logger.info(f"[Worker-{self.worker_id}] Starting...")
        
        # 连接缓冲区
        self.connect_buffers()
        
        last_cleanup_time = time.time()
        last_stats_time = time.time()
        
        while running_flag.value:
            try:
                # 处理l2quote
                result = self.l2quote_buffer.get_msgpack()
                if result:
                    timestamp, data_packet = result
                    self.process_l2quote(data_packet)
                
                # 处理l2order
                result = self.l2order_buffer.get_msgpack()
                if result:
                    timestamp, data_packet = result
                    self.process_l2order(data_packet)
                
                # 处理l2transaction
                result = self.l2transaction_buffer.get_msgpack()
                if result:
                    timestamp, data_packet = result
                    self.process_l2transaction(data_packet)
                
                # 如果没有数据，短暂休息
                if not result:
                    time.sleep(0.001)  # 1ms
                
                # 定期清理旧订单（每60秒）
                current_time = time.time()
                if current_time - last_cleanup_time > 60:
                    self.flow_calc.cleanup_old_orders(max_age_seconds=3600)
                    last_cleanup_time = current_time
                
                # 定期输出统计（每10秒）
                if current_time - last_stats_time > 10:
                    logger.info(
                        f"[Worker-{self.worker_id}] Stats: "
                        f"quote={self.stats['l2quote_processed']}, "
                        f"order={self.stats['l2order_processed']}, "
                        f"trans={self.stats['l2transaction_processed']}, "
                        f"errors={self.stats['errors']}"
                    )
                    last_stats_time = current_time
            
            except KeyboardInterrupt:
                logger.info(f"[Worker-{self.worker_id}] Interrupted by user")
                break
            except Exception as e:
                logger.error(f"[Worker-{self.worker_id}] Error in main loop: {e}")
                self.stats['errors'] += 1
                time.sleep(0.1)
        
        # 清理
        self.l2quote_buffer.close()
        self.l2order_buffer.close()
        self.l2transaction_buffer.close()
        
        logger.info(f"[Worker-{self.worker_id}] Stopped. Final stats: {self.stats}")


def worker_process(worker_id: int, running_flag: Value, enable_limit_up_flow: bool = True):
    """
    消费者进程入口函数
    
    Args:
        worker_id: 工作器ID
        running_flag: 运行标志
        enable_limit_up_flow: 是否启用板上资金流向计算
    """
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s [Worker-{worker_id}] %(levelname)s: %(message)s'
    )
    
    worker = ConsumerWorker(worker_id, enable_limit_up_flow)
    worker.run(running_flag)


def create_consumer_pool(num_workers: int = 8, enable_limit_up_flow: bool = True) -> tuple:
    """
    创建消费者进程池
    
    Args:
        num_workers: 消费者进程数量（建议8个）
        enable_limit_up_flow: 是否启用板上资金流向计算
        
    Returns:
        (processes, running_flag) - 进程列表和运行标志
    """
    running_flag = Value('b', True)
    processes: List[Process] = []
    
    for i in range(num_workers):
        p = Process(
            target=worker_process,
            args=(i, running_flag, enable_limit_up_flow),
            name=f'Consumer-{i}'
        )
        p.start()
        processes.append(p)
        logger.info(f"Started consumer process {i}")
    
    return processes, running_flag


def stop_consumer_pool(processes: List[Process], running_flag: Value, timeout: int = 5):
    """
    停止消费者进程池
    
    Args:
        processes: 进程列表
        running_flag: 运行标志
        timeout: 超时时间（秒）
    """
    logger.info("Stopping consumer pool...")
    running_flag.value = False
    
    for p in processes:
        p.join(timeout=timeout)
        if p.is_alive():
            logger.warning(f"Process {p.name} did not stop gracefully, terminating...")
            p.terminate()
            p.join(timeout=1)
    
    logger.info("Consumer pool stopped")