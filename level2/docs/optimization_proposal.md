# Level2 数据处理系统优化方案

## 当前架构分析

### 现有架构（Shared Memory + Multi-Process Consumer）

```
主进程 (订阅全部股票 ~5000只)
    ↓
回调函数 (3种数据类型: l2quote, l2order, l2transaction)
    ├─→ msgpack序列化 (~15-20μs) ← 主要瓶颈
    ├─→ 写入共享内存 (~3-5μs)
    └─→ 共享内存缓冲区 (3个独立环形缓冲区)
        ↓
消费者进程池 (8-16个进程)
    ├─→ 进程1: 轮询读取 quote/order/trans
    ├─→ 进程2: 轮询读取 quote/order/trans
    └─→ ...
```

### 性能瓶颈

1. **序列化开销**: msgpack序列化占用15-20μs，在高频场景下(9:25集合竞价)成为主要瓶颈
2. **共享内存竞争**: 多个消费者进程竞争读取同一个缓冲区
3. **数据类型混合**: 每个进程轮询3种数据类型，可能导致某种数据处理滞后
4. **进程间通信**: 共享内存需要锁机制，增加延迟

---

## 优化方案：分区多进程 + 线程消费者

### 新架构设计

```
主进程 (初始化和协调)
    ↓
股票分区 (按股票代码哈希分成 N 个分区, N=8/16)
    ↓
分区进程池 (N个独立子进程)
    ├─→ 分区进程 1 (订阅股票子集 ~500只)
    │   ├─→ 回调函数 (无需序列化，直接写入内存)
    │   │   ├─→ l2quote → deque_quote (lock-free)
    │   │   ├─→ l2order → deque_order (lock-free)
    │   │   └─→ l2transaction → deque_trans (lock-free)
    │   │
    │   └─→ 线程池 (3*M个线程, M=2-4)
    │       ├─→ Quote线程1 → 处理 deque_quote
    │       ├─→ Quote线程2 → 处理 deque_quote
    │       ├─→ Order线程1 → 处理 deque_order
    │       ├─→ Order线程2 → 处理 deque_order
    │       ├─→ Trans线程1 → 处理 deque_trans
    │       └─→ Trans线程2 → 处理 deque_trans
    │
    ├─→ 分区进程 2 (订阅股票子集 ~500只)
    │   └─→ ...
    │
    └─→ 分区进程 N
        └─→ ...
            ↓
结果聚合 (通过共享内存)
    └─→ 定期合并各分区的计算结果
```

### 核心优化点

#### 1. **消除序列化开销**
- 回调数据直接存入Python对象（dict），无需msgpack序列化
- 节省：15-20μs → 0μs

#### 2. **Lock-Free队列**
- 使用`collections.deque`作为队列（线程安全，无GIL竞争）
- 每种数据类型独立队列，避免混合
- deque的`append()`和`popleft()`在CPython中是原子操作

#### 3. **减少进程间竞争**
- 股票分区后，每个进程只处理自己订阅的股票
- 各进程独立运行，无共享内存竞争

#### 4. **专用线程处理**
- 每种数据类型由专门的线程处理
- 避免不同类型数据相互影响
- 充分利用多核CPU

#### 5. **内存效率**
- 回调数据直接保留在进程内存中
- 避免跨进程内存拷贝

---

## 实现细节

### 1. 股票分区策略

```python
def partition_stocks(stock_list: List[str], num_partitions: int) -> List[List[str]]:
    """
    将股票列表分成N个分区
    使用哈希确保同一股票总是在同一分区
    """
    partitions = [[] for _ in range(num_partitions)]
    for stock in stock_list:
        partition_id = hash(stock) % num_partitions
        partitions[partition_id].append(stock)
    return partitions
```

### 2. Deque队列管理器

```python
from collections import deque
from threading import Thread
import queue

class DequeBufferManager:
    """使用deque的无锁队列管理器"""
    
    def __init__(self, maxlen: int = 100000):
        # 三个独立的deque，线程安全
        self.quote_queue = deque(maxlen=maxlen)
        self.order_queue = deque(maxlen=maxlen * 15)  # order数据量最大
        self.trans_queue = deque(maxlen=maxlen * 5)
        
    def on_l2quote_callback(self, datas: dict):
        """直接追加，无需序列化"""
        for stock_code, quote_data in datas.items():
            self.quote_queue.append({
                'stock_code': stock_code,
                'data': quote_data,
                'timestamp': time.time()
            })
    
    def on_l2order_callback(self, datas: dict):
        for stock_code, order_data in datas.items():
            self.order_queue.append({
                'stock_code': stock_code,
                'data': order_data,
                'timestamp': time.time()
            })
    
    def on_l2transaction_callback(self, datas: dict):
        for stock_code, trans_data in datas.items():
            self.trans_queue.append({
                'stock_code': stock_code,
                'data': trans_data,
                'timestamp': time.time()
            })
```

### 3. 多线程消费者

```python
class ThreadedConsumer:
    """线程消费者"""
    
    def __init__(self, data_type: str, data_queue: deque, 
                 calculators: dict, num_threads: int = 2):
        self.data_type = data_type
        self.data_queue = data_queue
        self.calculators = calculators
        self.num_threads = num_threads
        self.running = True
        self.threads = []
        
    def start(self):
        """启动消费者线程"""
        for i in range(self.num_threads):
            t = Thread(target=self._consume_loop, 
                      args=(i,), daemon=True)
            t.start()
            self.threads.append(t)
    
    def _consume_loop(self, thread_id: int):
        """消费循环"""
        while self.running:
            try:
                # 从deque左侧弹出（FIFO）
                data_packet = self.data_queue.popleft()
                self._process_data(data_packet)
            except IndexError:
                # 队列为空，短暂休息
                time.sleep(0.0001)  # 100μs
            except Exception as e:
                logger.error(f"Error in thread {thread_id}: {e}")
    
    def _process_data(self, data_packet: dict):
        """处理数据"""
        stock_code = data_packet['stock_code']
        data = data_packet['data']
        
        if self.data_type == 'quote':
            self.calculators['seal'].on_l2quote(stock_code, data)
            self.calculators['flow'].on_l2quote(stock_code, data)
        elif self.data_type == 'order':
            self.calculators['flow'].on_l2order(stock_code, data)
        elif self.data_type == 'trans':
            self.calculators['flow'].on_l2transaction(stock_code, data)
```

### 4. 分区进程

```python
class PartitionProcess:
    """分区进程 - 订阅股票子集并使用线程处理"""
    
    def __init__(self, partition_id: int, stock_list: List[str], 
                 enable_limit_up_flow: bool = True):
        self.partition_id = partition_id
        self.stock_list = stock_list
        self.enable_limit_up_flow = enable_limit_up_flow
        
        # Deque队列管理器
        self.buffer_manager = DequeBufferManager()
        
        # 初始化计算器
        self.seal_calc = SealAmountCalculator()
        if enable_limit_up_flow:
            self.flow_calc = LimitUpFlowCalculator(self.seal_calc)
        else:
            self.flow_calc = CapitalFlowCalculator()
        
        # 线程消费者
        self.consumers = []
        
    def start(self):
        """启动分区进程"""
        # 订阅XTDATA
        self._subscribe_xtdata()
        
        # 启动消费者线程
        calculators = {
            'seal': self.seal_calc,
            'flow': self.flow_calc
        }
        
        # 3种数据类型，每种2-4个线程
        self.consumers.append(
            ThreadedConsumer('quote', self.buffer_manager.quote_queue, 
                           calculators, num_threads=2)
        )
        self.consumers.append(
            ThreadedConsumer('order', self.buffer_manager.order_queue, 
                           calculators, num_threads=4)
        )
        self.consumers.append(
            ThreadedConsumer('trans', self.buffer_manager.trans_queue, 
                           calculators, num_threads=2)
        )
        
        for consumer in self.consumers:
            consumer.start()
        
        # 监控循环
        self._monitoring_loop()
```

### 5. 结果聚合

```python
from multiprocessing import shared_memory
import numpy as np

class ResultAggregator:
    """结果聚合器 - 使用共享内存收集各分区结果"""
    
    def __init__(self, num_partitions: int):
        self.num_partitions = num_partitions
        # 创建共享内存用于结果聚合
        # 可以使用numpy数组或自定义结构
        
    def aggregate_results(self) -> dict:
        """聚合所有分区的结果"""
        # 从各分区收集结果并合并
        pass
```

---

## 性能对比

| 指标 | 当前架构 | 优化架构 | 提升 |
|------|---------|---------|------|
| 序列化时间 | 15-20μs | 0μs | ✅ 100% |
| 写入延迟 | 3-5μs | <1μs | ✅ 70% |
| 跨进程通信 | 需要 | 不需要 | ✅ 消除 |
| 数据处理并行度 | 低（混合轮询） | 高（专用线程） | ✅ 3x |
| 内存拷贝 | 多次 | 最少 | ✅ 减少 |
| 锁竞争 | 有 | 无（lock-free deque） | ✅ 消除 |

### 预期性能提升

- **延迟降低**: 20-30μs → 5-10μs (60-70%提升)
- **吞吐量提升**: 2-3x
- **CPU利用率**: 更均衡，避免单点瓶颈
- **可扩展性**: 线性扩展（增加分区数）

---

## 实施步骤

1. ✅ **分析现有架构** - 识别瓶颈
2. **创建DequeBufferManager** - 替代SharedMemoryRingBuffer
3. **实现ThreadedConsumer** - 多线程消费者
4. **实现PartitionProcess** - 分区进程
5. **更新main.py** - 新架构入口
6. **实现ResultAggregator** - 结果聚合
7. **性能测试** - 对比新旧架构
8. **文档更新** - 使用说明

---

## 配置建议

### 小规模（1000只股票）
- 分区数: 4-8
- 每分区线程: Quote(2) + Order(2) + Trans(2) = 6线程
- 总线程数: 4分区 × 6线程 = 24线程

### 中规模（3000只股票）
- 分区数: 8-12
- 每分区线程: Quote(2) + Order(4) + Trans(2) = 8线程
- 总线程数: 8分区 × 8线程 = 64线程

### 大规模（5000只股票）
- 分区数: 16
- 每分区线程: Quote(2) + Order(4) + Trans(2) = 8线程
- 总线程数: 16分区 × 8线程 = 128线程

---

## 注意事项

1. **GIL影响**: Python GIL会影响多线程性能，但数据处理主要是I/O密集型，影响有限
2. **内存使用**: deque会占用更多内存（无序列化），需要合理设置maxlen
3. **监控**: 需要监控各分区的队列深度，避免积压
4. **故障隔离**: 单个分区进程崩溃不影响其他分区

---

## 后续优化方向

1. **Cython优化**: 将计算密集型部分用Cython重写
2. **NumPy向量化**: 批量计算时使用NumPy
3. **GPU加速**: 对于大规模数据聚合，考虑GPU加速
4. **异步I/O**: 使用asyncio优化I/O操作