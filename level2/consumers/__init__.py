"""
消费者模块

提供多种消费者实现:
1. ConsumerWorker - 基于共享内存的多进程消费者（旧版）
2. MultiThreadedConsumerPool - 按数据类型分线程的消费者（旧版）
3. UnifiedConsumerPool - 按股票分线程的统一消费者（新版，推荐）
"""

from level2.consumers.worker import ConsumerWorker, create_consumer_pool
from level2.consumers.threaded_worker import (
    ThreadedConsumer,
    MultiThreadedConsumerPool
)
from level2.consumers.unified_worker import (
    UnifiedDataPacket,
    ThreadLocalBuffer,
    UnifiedConsumerThread,
    UnifiedConsumerPool
)

__all__ = [
    # 旧版 - 共享内存多进程
    'ConsumerWorker',
    'create_consumer_pool',
    # 旧版 - 按数据类型分线程
    'ThreadedConsumer',
    'MultiThreadedConsumerPool',
    # 新版 - 按股票分线程（推荐）
    'UnifiedDataPacket',
    'ThreadLocalBuffer',
    'UnifiedConsumerThread',
    'UnifiedConsumerPool',
]
