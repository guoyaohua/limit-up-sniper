"""
A股 Level 2 数据实时接收处理系统 - 主入口

整合XTDATA订阅、共享内存缓冲区和多进程消费者
"""

import logging
import time
import signal
import sys
from datetime import datetime
from typing import List, Optional
from level2.buffers.ring_buffer import Level2BufferManager
from level2.consumers.worker import create_consumer_pool, stop_consumer_pool

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(f'level2_{datetime.now().strftime("%Y%m%d")}.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


class Level2DataSystem:
    """
    Level 2数据处理系统
    
    集成:
    1. XTDATA订阅管理
    2. 共享内存缓冲区
    3. 多进程消费者池
    """
    
    def __init__(
        self,
        stock_list: List[str],
        num_consumers: int = 8,
        enable_limit_up_flow: bool = True
    ):
        """
        初始化系统
        
        Args:
            stock_list: 股票列表，如 ['600000.SH', '000001.SZ', ...]
            num_consumers: 消费者进程数量（建议8个）
            enable_limit_up_flow: 是否启用板上资金流向计算
        """
        self.stock_list = stock_list
        self.num_consumers = num_consumers
        self.enable_limit_up_flow = enable_limit_up_flow
        
        # 缓冲区管理器
        self.buffer_manager: Optional[Level2BufferManager] = None
        
        # 消费者进程
        self.consumer_processes = None
        self.running_flag = None
        
        # XTDATA订阅ID
        self.subscribe_ids = []
        
        # 运行状态
        self.is_running = False
        
        # 注册信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """信号处理器"""
        logger.info(f"Received signal {signum}, stopping system...")
        self.stop()
        sys.exit(0)
    
    def start(self):
        """启动系统"""
        logger.info("=" * 70)
        logger.info("A股 Level 2 数据实时接收处理系统")
        logger.info("=" * 70)
        logger.info(f"股票数量: {len(self.stock_list)}")
        logger.info(f"消费者进程数: {self.num_consumers}")
        logger.info(f"板上资金流向: {'启用' if self.enable_limit_up_flow else '禁用'}")
        logger.info("=" * 70)
        
        try:
            # 1. 初始化缓冲区
            logger.info("Step 1: Initializing shared memory buffers...")
            self.buffer_manager = Level2BufferManager(create=True)
            logger.info("Buffers created successfully")
            
            # 2. 启动消费者进程池
            logger.info(f"Step 2: Starting {self.num_consumers} consumer processes...")
            self.consumer_processes, self.running_flag = create_consumer_pool(
                num_workers=self.num_consumers,
                enable_limit_up_flow=self.enable_limit_up_flow
            )
            logger.info("Consumer pool started")
            
            # 3. 订阅XTDATA
            logger.info("Step 3: Subscribing to XTDATA...")
            self._subscribe_xtdata()
            logger.info("XTDATA subscription completed")
            
            self.is_running = True
            logger.info("=" * 70)
            logger.info("System started successfully! Press Ctrl+C to stop.")
            logger.info("=" * 70)
            
            # 4. 监控循环
            self._monitoring_loop()
            
        except Exception as e:
            logger.error(f"Failed to start system: {e}", exc_info=True)
            self.stop()
            raise
    
    def _subscribe_xtdata(self):
        """订阅XTDATA Level2数据"""
        try:
            from xtquant import xtdata
        except ImportError:
            logger.warning(
                "xtdata module not found. "
                "Please install xtdata or run in simulation mode."
            )
            logger.info("Running in SIMULATION mode (no real data)")
            return
        
        today = datetime.now().strftime('%Y%m%d')
        
        # 订阅 l2quote
        subscribe_id_quote = xtdata.subscribe_quote(
            self.stock_list,
            period='l2quote',
            start_time=today + '000000',
            count=0,
            callback=self.buffer_manager.on_l2quote_callback
        )
        self.subscribe_ids.append(subscribe_id_quote)
        logger.info(f"Subscribed to l2quote (ID: {subscribe_id_quote})")
        
        # 订阅 l2order
        subscribe_id_order = xtdata.subscribe_quote(
            self.stock_list,
            period='l2order',
            start_time=today + '000000',
            count=0,
            callback=self.buffer_manager.on_l2order_callback
        )
        self.subscribe_ids.append(subscribe_id_order)
        logger.info(f"Subscribed to l2order (ID: {subscribe_id_order})")
        
        # 订阅 l2transaction
        subscribe_id_trans = xtdata.subscribe_quote(
            self.stock_list,
            period='l2transaction',
            start_time=today + '000000',
            count=0,
            callback=self.buffer_manager.on_l2transaction_callback
        )
        self.subscribe_ids.append(subscribe_id_trans)
        logger.info(f"Subscribed to l2transaction (ID: {subscribe_id_trans})")
    
    def _monitoring_loop(self):
        """监控循环 - 定期输出统计信息"""
        last_stats_time = time.time()
        
        while self.is_running:
            try:
                time.sleep(1)
                
                # 每30秒输出一次统计
                current_time = time.time()
                if current_time - last_stats_time > 30:
                    self._print_statistics()
                    last_stats_time = current_time
                
                # 检查消费者进程是否存活
                if self.consumer_processes:
                    alive_count = sum(1 for p in self.consumer_processes if p.is_alive())
                    if alive_count < self.num_consumers:
                        logger.warning(
                            f"Some consumer processes died! "
                            f"Alive: {alive_count}/{self.num_consumers}"
                        )
            
            except KeyboardInterrupt:
                logger.info("Monitoring loop interrupted")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(5)
    
    def _print_statistics(self):
        """输出统计信息"""
        if not self.buffer_manager:
            return
        
        stats = self.buffer_manager.get_all_stats()
        
        logger.info("=" * 70)
        logger.info("Buffer Statistics:")
        for buffer_name, buffer_stats in stats.items():
            logger.info(
                f"  {buffer_name}: "
                f"available={buffer_stats['available']}, "
                f"usage={buffer_stats['usage_rate']*100:.1f}%, "
                f"overflow={buffer_stats['overflow_count']}"
            )
        logger.info("=" * 70)
    
    def stop(self):
        """停止系统"""
        if not self.is_running:
            return
        
        logger.info("Stopping system...")
        self.is_running = False
        
        try:
            # 1. 取消XTDATA订阅
            if self.subscribe_ids:
                try:
                    from xtquant import xtdata
                    for subscribe_id in self.subscribe_ids:
                        xtdata.unsubscribe_quote(subscribe_id)
                    logger.info("Unsubscribed from XTDATA")
                except:
                    pass
            
            # 2. 停止消费者进程
            if self.consumer_processes and self.running_flag:
                stop_consumer_pool(self.consumer_processes, self.running_flag)
            
            # 3. 清理缓冲区
            if self.buffer_manager:
                logger.info("Cleaning up buffers...")
                self.buffer_manager.cleanup_all()
            
            logger.info("System stopped successfully")
        
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)


def main():
    """主函数 - 示例用法"""
    # 示例：订阅100只股票
    stock_list = [
        # 上交所
        '600000.SH', '600519.SH', '600036.SH', '601318.SH', '600276.SH',
        # 深交所
        '000001.SZ', '000002.SZ', '000858.SZ', '000333.SZ', '000725.SZ',
        # 科创板
        '688981.SH', '688111.SH', '688008.SH',
        # 创业板
        '300750.SZ', '300059.SZ', '300347.SZ',
    ]
    
    # 创建系统实例
    system = Level2DataSystem(
        stock_list=stock_list,
        num_consumers=8,  # 8个消费者进程
        enable_limit_up_flow=True  # 启用板上资金流向计算
    )
    
    # 启动系统
    try:
        system.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        system.stop()


if __name__ == '__main__':
    main()