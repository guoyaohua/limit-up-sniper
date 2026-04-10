"""
A股 Level 2 数据实时接收处理系统 - 重构版主入口

重构后架构:
1. 多进程分区（默认16个进程）- 将股票列表分块
2. 每个进程内多线程（默认2个线程）- 按股票分工处理
3. 统一 Level2Calculator - 合并处理所有数据类型

性能特点:
- 无序列化开销：回调数据直接存入 deque
- 按股票分工：减少线程间数据竞争
- 统一计算器：避免重复计算，内存更紧凑
"""

import logging
import time
import signal
import sys
from datetime import datetime
from typing import List, Optional, Dict
from dataclasses import dataclass
from level2.partition_process import (
    create_partition_pool,
    stop_partition_pool,
    partition_stocks
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(f'level2_optimized_{datetime.now().strftime("%Y%m%d")}.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

@dataclass
class SystemConfig:
    """系统配置"""
    # 分区配置
    num_partitions: int = 16  # 分区数量（进程数）
    
    # 线程配置（每个分区）
    num_threads: int = 2  # 每个分区的消费者线程数
    
    # 功能开关
    enable_limit_up_flow: bool = True  # 板上资金流向
    
    @classmethod
    def small_scale(cls) -> "SystemConfig":
        """小规模配置（1000只股票）"""
        return cls(
            num_partitions=4,
            num_threads=2
        )
    
    @classmethod
    def medium_scale(cls) -> "SystemConfig":
        """中规模配置（3000只股票）"""
        return cls(
            num_partitions=8,
            num_threads=2
        )
    
    @classmethod
    def large_scale(cls) -> "SystemConfig":
        """大规模配置（5000只股票）"""
        return cls(
            num_partitions=16,
            num_threads=2
        )
    
    def total_threads(self) -> int:
        """系统总线程数"""
        return self.num_partitions * self.num_threads

class Level2System:
    """
    Level 2 数据处理系统 - 重构版
    
    架构特点:
    - 多进程分区：按股票哈希分配到不同进程
    - 进程内多线程：每个线程处理一部分股票的所有数据
    - 统一计算器：Level2Calculator 处理所有数据类型
    """
    
    def __init__(
        self,
        stock_list: List[str],
        config: Optional[SystemConfig] = None,
        stock_info: Optional[Dict[str, float]] = None
    ):
        """
        初始化系统
        
        Args:
            stock_list: 股票列表
            config: 配置，如果为 None 则根据股票数量自动选择
            stock_info: 股票涨停价信息 {stock_code: limit_price}
        """
        self.stock_list = stock_list
        self.stock_info = stock_info
        
        # 自动选择配置
        if config is None:
            if len(stock_list) < 1500:
                config = SystemConfig.small_scale()
            elif len(stock_list) < 4000:
                config = SystemConfig.medium_scale()
            else:
                config = SystemConfig.large_scale()
        
        self.config = config
        
        # 分区进程池
        self.partition_processes = None
        self.running_flags = None
        
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
        logger.info("A股 Level 2 数据实时处理系统 - 重构版")
        logger.info("=" * 70)
        logger.info(f"股票数量: {len(self.stock_list)}")
        logger.info(f"分区数量: {self.config.num_partitions}")
        logger.info(f"每分区线程: {self.config.num_threads}")
        logger.info(f"系统总线程: {self.config.total_threads()}")
        logger.info(f"板上资金流向: {'启用' if self.config.enable_limit_up_flow else '禁用'}")
        logger.info("=" * 70)
        
        try:
            # 显示分区信息
            partitions = partition_stocks(self.stock_list, self.config.num_partitions)
            logger.info("分区分布:")
            for i, partition in enumerate(partitions):
                logger.info(f"  分区 {i}: {len(partition)} 只股票")
            logger.info("=" * 70)
            
            # 启动分区进程池
            logger.info("正在启动分区进程池...")
            self.partition_processes, self.running_flags = create_partition_pool(
                stock_list=self.stock_list,
                num_partitions=self.config.num_partitions,
                enable_limit_up_flow=self.config.enable_limit_up_flow,
                num_threads=self.config.num_threads,
                stock_info=self.stock_info
            )
            
            self.is_running = True
            logger.info("=" * 70)
            logger.info("系统启动成功! 按 Ctrl+C 停止.")
            logger.info("=" * 70)
            
            # 监控循环
            self._monitoring_loop()
        
        except Exception as e:
            logger.error(f"系统启动失败: {e}", exc_info=True)
            self.stop()
            raise
    
    def _monitoring_loop(self):
        """监控循环 - 检查进程状态"""
        last_check_time = time.time()
        
        while self.is_running:
            try:
                time.sleep(5)
                
                current_time = time.time()
                
                # 每60秒检查一次进程状态
                if current_time - last_check_time > 60:
                    self._check_process_health()
                    last_check_time = current_time
            
            except KeyboardInterrupt:
                logger.info("监控循环被中断")
                break
            except Exception as e:
                logger.error(f"监控循环错误: {e}")
                time.sleep(10)
    
    def _check_process_health(self):
        """检查进程健康状态"""
        if not self.partition_processes:
            return
        
        alive_count = sum(1 for p in self.partition_processes if p.is_alive())
        total_count = len(self.partition_processes)
        
        logger.info(f"进程健康检查: {alive_count}/{total_count} 进程运行中")
        
        if alive_count < total_count:
            logger.warning(f"警告: {total_count - alive_count} 个进程已停止!")
            
            # 列出已停止的进程
            for i, p in enumerate(self.partition_processes):
                if not p.is_alive():
                    logger.warning(f"  进程 {i} ({p.name}) 已停止")
    
    def stop(self):
        """停止系统"""
        if not self.is_running:
            return
        
        logger.info("正在停止系统...")
        self.is_running = False
        
        try:
            # 停止分区进程池
            if self.partition_processes and self.running_flags:
                stop_partition_pool(
                    self.partition_processes,
                    self.running_flags,
                    timeout=10
                )
            
            logger.info("系统停止成功")
        
        except Exception as e:
            logger.error(f"停止系统时出错: {e}", exc_info=True)

def main():
    """主函数 - 示例用法"""
    
    # 获取股票列表
    stock_list = get_stock_list()
    
    logger.info(f"加载了 {len(stock_list)} 只股票")
    
    # 创建系统实例（自动选择配置）
    system = Level2System(stock_list=stock_list)
    
    # 启动系统
    try:
        system.start()
    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        system.stop()

def main_with_custom_config():
    """主函数 - 自定义配置"""
    
    stock_list = get_stock_list()
    
    # 自定义配置
    config = SystemConfig(
        num_partitions=12,          # 12个分区（进程）
        num_threads=4,              # 每分区4个线程
        enable_limit_up_flow=True   # 启用板上资金流向
    )
    
    logger.info(f"自定义配置: {config}")
    logger.info(f"系统总线程数: {config.total_threads()}")
    
    # 创建系统实例
    system = Level2System(
        stock_list=stock_list,
        config=config
    )
    
    # 启动系统
    try:
        system.start()
    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        system.stop()

def get_stock_list() -> List[str]:
    """
    获取股票列表
    
    TODO:
    实际使用时，可以:
    1. 从配置文件读取
    2. 从 API 获取全市场股票
    3. 从数据库查询
    """
    # 示例：返回测试股票列表
    stock_list = []
    
    # 上交所主板
    for i in range(600000, 600100):
        stock_list.append(f"{i}.SH")
    
    # 深交所主板
    for i in range(1, 100):
        stock_list.append(f"{i:06d}.SZ")
    
    # 科创板
    for i in range(688001, 688050):
        stock_list.append(f"{i}.SH")
    
    # 创业板
    for i in range(300001, 300100):
        stock_list.append(f"{i}.SZ")
    
    return stock_list

def compare_architectures():
    """
    架构对比说明
    """
    print("=" * 70)
    print("架构对比: 重构版 vs 旧版")
    print("=" * 70)
    
    print("\n旧架构 (按数据类型分线程):")
    print("  - 每个分区按 quote/order/trans 分多个线程")
    print("  - 使用 SealAmountCalculator + CapitalFlowCalculator")
    print("  - 线程间需要共享计算器状态")
    print("  - 问题: 同一股票的数据可能被不同线程处理")
    
    print("\n重构后架构 (按股票分线程):")
    print("  - 每个分区按股票分多个线程")
    print("  - 使用统一的 Level2Calculator")
    print("  - 每个线程有独立的计算器实例")
    print("  - 优势: 同一股票的数据由同一线程处理，无竞争")
    
    print("\n配置建议:")
    configs = {
        "小规模(1000只)": SystemConfig.small_scale(),
        "中规模(3000只)": SystemConfig.medium_scale(),
        "大规模(5000只)": SystemConfig.large_scale()
    }
    
    for name, cfg in configs.items():
        print(f"\n  {name}:")
        print(f"    分区数: {cfg.num_partitions}")
        print(f"    每分区线程: {cfg.num_threads}")
        print(f"    系统总线程: {cfg.total_threads()}")
    
    print("\n" + "=" * 70)

# ============ 兼容旧接口 ============
# 保留旧的类名作为别名，避免破坏现有代码
OptimizedConfig = SystemConfig
OptimizedLevel2System = Level2System

if __name__ == '__main__':
    # 显示架构对比
    # compare_architectures()
    
    print("\n启动系统...")
    
    # 启动重构版系统
    main()
