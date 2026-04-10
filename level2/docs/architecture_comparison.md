# Level 2 架构方案对比分析

## 当前架构 vs 股票池分区架构

### 测试结果总结

**当前优化后性能:**
- 单条写入: 23.77μs (1.14x improvement)
- 批量写入: 21.61μs (1.25x improvement)
- 快速写入: 21.38μs (1.27x improvement)
- **瓶颈**: msgpack序列化仍占用~15-20μs

---

## 方案对比

### 当前架构 (Shared Memory + Multi-Process Consumer)

```
主进程 (订阅全部股票)
    ↓
    └─→ 回调函数 (3种数据类型)
        ├─→ msgpack序列化 (~15-20μs) ← 主要瓶颈
        ├─→ 写入共享内存 (~3-5μs)
        └─→ 共享内存缓冲区
            ↓
        消费者进程池 (8-16个进程)
            ├─→ 进程1: 轮询读取 quote/order/trans
            ├─→ 进程2: 轮询读取 quote/order/trans
            └─→ ...
```

**优点:**
- ✅ 架构简单
- ✅ 动态负载均衡
- ✅ 易于扩展消费者数量

**缺点:**
- ❌ msgpack序列化开销大 (15-20μs)
- ❌ 共享内存写入开销 (3-5μs)
- ❌ 进程间通信开销
- ❌ 轮询读取3种数据类型,可能不均衡
- ❌ Python GIL限制单进程性能

---

### 提议架构 (Stock Pool Partitioning + Multi-Threading)

```
股票池分区
    ├─→ 分区1 (股票1-125)
    │   └─→ 进程1 (只订阅125只股票)
    │       ├─→ 线程1: l2quote回调 → 直接处理 (无序列化)
    │       ├─→ 线程2: l2order回调 → 直接处理 (无序列化)
    │       └─→ 线程3: l2transaction回调 → 直接处理 (无序列化)
    │
    ├─→ 分区2 (股票126-250)
    │   └─→ 进程2 (只订阅125只股票)
    │       ├─→ 线程1: l2quote
    │       ├─→ 线程2: l2order
    │       └─→ 线程3: l2transaction
    │
    └─→ ... (总共8个分区/进程)
        
结果汇总
    └─→ 共享内存 (仅用于结果汇总,而非原始数据)
```

**优点:**
- ✅ **零序列化开销**: 回调数据直接在进程内存中处理
- ✅ **零共享内存写入开销**: 无需频繁IPC
- ✅ **数据类型并行**: 3个线程独立处理3种数据
- ✅ **负载自然分散**: 每个进程只处理部分股票
- ✅ **GIL影响小**: 多进程 + I/O密集型线程

**缺点:**
- ⚠️ 架构复杂度增加
- ⚠️ 股票分区需要预规划
- ⚠️ 结果汇总逻辑需要设计
- ⚠️ XTDATA API是否支持进程级订阅需确认（支持）

---

## 性能对比估算

### 当前架构性能

| 操作 | 时间消耗 | 说明 |
|-----|---------|------|
| msgpack序列化 | 15-20μs | 主要瓶颈 |
| 共享内存写入 | 3-5μs | struct操作+内存拷贝 |
| 消费者读取 | 2-3μs | 反序列化 |
| 业务处理 | 3-8μs | 计算器逻辑 |
| **总计** | **23-36μs** | 端到端延迟 |

**吞吐量**: ~42K ops/sec per writer

### 新架构性能 (估算)

| 操作 | 时间消耗 | 说明 |
|-----|---------|------|
| ~~msgpack序列化~~ | **0μs** | ✅ 无需序列化 |
| ~~共享内存写入~~ | **0μs** | ✅ 直接内存访问 |
| 业务处理 | 3-8μs | 计算器逻辑 |
| **总计** | **3-8μs** | 端到端延迟 |

**吞吐量**: ~125K-330K ops/sec per worker

**性能提升**: **3-10x** 🚀

---

## 详细设计方案

### 1. 股票池分区策略

```python
class StockPartitioner:
    """股票池分区器"""
    
    @staticmethod
    def partition_stocks(stock_list: List[str], num_partitions: int = 8) -> List[List[str]]:
        """
        将股票池均分为N个分区
        
        策略:
        1. 按股票代码排序 (确保可重现)
        2. 轮询分配 (而非连续分配,避免市场偏差)
        
        Args:
            stock_list: 股票代码列表
            num_partitions: 分区数量 (建议: CPU核心数)
        
        Returns:
            分区列表
        """
        sorted_stocks = sorted(stock_list)
        partitions = [[] for _ in range(num_partitions)]
        
        for idx, stock in enumerate(sorted_stocks):
            partition_idx = idx % num_partitions
            partitions[partition_idx].append(stock)
        
        return partitions


# 使用示例
stock_pool = ['600000.SH', '000001.SZ', ...]  # 1000只股票
partitions = StockPartitioner.partition_stocks(stock_pool, num_partitions=8)

# 结果:
# partitions[0] = [125只股票]
# partitions[1] = [125只股票]
# ...
```

### 2. 进程架构设计

```python
import multiprocessing as mp
import threading
from queue import Queue
from typing import Dict, List


class PartitionedWorker(mp.Process):
    """分区工作进程"""
    
    def __init__(self, 
                 partition_id: int,
                 stock_list: List[str],
                 result_queue: mp.Queue):
        """
        Args:
            partition_id: 分区ID
            stock_list: 该分区负责的股票列表
            result_queue: 结果汇总队列 (用于跨进程通信)
        """
        super().__init__()
        self.partition_id = partition_id
        self.stock_list = stock_list
        self.result_queue = result_queue
        
        # 进程内数据队列 (无需序列化)
        self.quote_queue = Queue(maxsize=10000)
        self.order_queue = Queue(maxsize=50000)
        self.trans_queue = Queue(maxsize=20000)
        
        # 计算器 (每个进程独立)
        self.seal_calc = None
        self.flow_calc = None
        self.limit_up_calc = None
    
    def run(self):
        """进程主循环"""
        # 初始化计算器
        self._init_calculators()
        
        # 启动3个工作线程
        threads = [
            threading.Thread(target=self._quote_worker, daemon=True),
            threading.Thread(target=self._order_worker, daemon=True),
            threading.Thread(target=self._trans_worker, daemon=True),
        ]
        
        for t in threads:
            t.start()
        
        # 订阅Level 2数据
        self._subscribe_data()
        
        # 定期汇总结果
        self._report_results()
    
    def _init_calculators(self):
        """初始化计算器"""
        from level2.calculators.seal_amount import SealAmountCalculator
        from level2.calculators.limit_up_flow import LimitUpFlowCalculator
        
        self.seal_calc = SealAmountCalculator()
        self.limit_up_calc = LimitUpFlowCalculator(self.seal_calc)
        
        # 设置股票信息
        for stock in self.stock_list:
            # 从配置或API获取昨收价
            self.seal_calc.set_stock_info(stock, last_close=10.0)
    
    def _subscribe_data(self):
        """订阅Level 2数据"""
        from xtquant import xtdata
        
        # 关键: 每个进程只订阅自己分区的股票
        xtdata.subscribe_quote(
            stock_list=self.stock_list,
            period='tick',
            start_time='',
            end_time='',
            count=0,
            callback=self._on_quote_callback
        )
        
        xtdata.subscribe_l2_order(
            stock_list=self.stock_list,
            callback=self._on_order_callback
        )
        
        xtdata.subscribe_l2_transaction(
            stock_list=self.stock_list,
            callback=self._on_trans_callback
        )
    
    def _on_quote_callback(self, datas: dict):
        """
        l2quote回调 - 零拷贝
        
        直接将字典放入队列,无需序列化
        """
        for stock_code, quote_data in datas.items():
            try:
                self.quote_queue.put_nowait((stock_code, quote_data))
            except:
                # 队列满,跳过 (或记录溢出)
                pass
    
    def _on_order_callback(self, datas: dict):
        """l2order回调 - 零拷贝"""
        for stock_code, order_data in datas.items():
            try:
                self.order_queue.put_nowait((stock_code, order_data))
            except:
                pass
    
    def _on_trans_callback(self, datas: dict):
        """l2transaction回调 - 零拷贝"""
        for stock_code, trans_data in datas.items():
            try:
                self.trans_queue.put_nowait((stock_code, trans_data))
            except:
                pass
    
    def _quote_worker(self):
        """l2quote处理线程"""
        while True:
            stock_code, quote_data = self.quote_queue.get()
            
            # 直接处理,无需反序列化
            self.limit_up_calc.on_l2quote(stock_code, quote_data)
    
    def _order_worker(self):
        """l2order处理线程"""
        while True:
            stock_code, order_data = self.order_queue.get()
            self.limit_up_calc.on_l2order(stock_code, order_data)
    
    def _trans_worker(self):
        """l2transaction处理线程"""
        while True:
            stock_code, trans_data = self.trans_queue.get()
            self.limit_up_calc.on_l2transaction(stock_code, trans_data)
    
    def _report_results(self):
        """定期汇总结果到主进程"""
        import time
        
        while True:
            time.sleep(1)  # 每秒汇总一次
            
            # 收集当前分区的统计数据
            partition_stats = {
                'partition_id': self.partition_id,
                'timestamp': time.time(),
                'stocks': {}
            }
            
            for stock in self.stock_list:
                seal_info = self.seal_calc.get_seal_info(stock)
                flow_stats = self.limit_up_calc.get_flow_stats(stock)
                limit_up_stats = self.limit_up_calc.get_limit_up_stats(stock)
                
                if seal_info or flow_stats:
                    partition_stats['stocks'][stock] = {
                        'seal_info': seal_info,
                        'flow_stats': flow_stats,
                        'limit_up_stats': limit_up_stats
                    }
            
            # 通过队列发送到主进程 (仅汇总结果需要序列化)
            try:
                self.result_queue.put_nowait(partition_stats)
            except:
                pass  # 队列满
```

### 3. 主进程协调器

```python
class Level2PartitionedSystem:
    """分区式Level 2系统"""
    
    def __init__(self, stock_pool: List[str], num_partitions: int = 8):
        """
        Args:
            stock_pool: 完整股票池
            num_partitions: 分区数量 (推荐=CPU核心数)
        """
        self.stock_pool = stock_pool
        self.num_partitions = num_partitions
        
        # 分区股票池
        self.partitions = StockPartitioner.partition_stocks(
            stock_pool, num_partitions
        )
        
        # 进程间通信
        self.result_queue = mp.Queue(maxsize=1000)
        
        # 工作进程列表
        self.workers: List[PartitionedWorker] = []
        
        # 全局汇总结果
        self.global_stats = {}
    
    def start(self):
        """启动系统"""
        print(f"启动 {self.num_partitions} 个分区进程...")
        
        # 创建并启动工作进程
        for partition_id, stock_list in enumerate(self.partitions):
            worker = PartitionedWorker(
                partition_id=partition_id,
                stock_list=stock_list,
                result_queue=self.result_queue
            )
            worker.start()
            self.workers.append(worker)
            
            print(f"  分区 {partition_id}: {len(stock_list)} 只股票")
        
        # 启动结果汇总线程
        result_thread = threading.Thread(
            target=self._aggregate_results,
            daemon=True
        )
        result_thread.start()
        
        print("✅ 系统启动完成")
    
    def _aggregate_results(self):
        """汇总各分区结果"""
        while True:
            try:
                partition_stats = self.result_queue.get(timeout=1)
                
                # 更新全局统计
                for stock, stats in partition_stats['stocks'].items():
                    self.global_stats[stock] = stats
                
                # 可选: 输出日志、保存到数据库等
                
            except:
                continue
    
    def get_stats(self, stock_code: str = None) -> dict:
        """获取统计数据"""
        if stock_code:
            return self.global_stats.get(stock_code)
        return self.global_stats
    
    def stop(self):
        """停止系统"""
        for worker in self.workers:
            worker.terminate()
            worker.join()
```

### 4. 使用示例

```python
# 初始化系统
stock_pool = load_stock_pool()  # 加载1000只股票
system = Level2PartitionedSystem(
    stock_pool=stock_pool,
    num_partitions=8  # 8核CPU → 8个进程
)

# 启动
system.start()

# 实时查询
while True:
    time.sleep(1)
    
    # 查询某只股票
    stats = system.get_stats('600000.SH')
    if stats:
        print(f"封板金额: {stats['seal_info'].seal_amount_wan}万")
        print(f"主力净流入: {stats['flow_stats'].net_main/10000}万")
```

---

## 架构选择建议

### 方案A: 当前架构优化 (已完成)

**适用场景:**
- 股票池较小 (<500只)
- 对延迟要求不极端 (<30μs可接受)
- 需要快速上线

**性能:**
- 写入延迟: ~23μs
- 吞吐量: ~42K ops/sec
- 改进空间: 1.3x (通过msgpack-c或cython)

**推荐指数:** ⭐⭐⭐

---

### 方案B: 股票池分区架构 (推荐) ⭐⭐⭐⭐⭐

**适用场景:**
- 股票池大 (>500只)
- 对性能要求高 (<10μs)
- 可以接受架构复杂度

**性能:**
- 写入延迟: ~3-8μs (零序列化)
- 吞吐量: ~125K-330K ops/sec
- 改进空间: 3-10x

**推荐指数:** ⭐⭐⭐⭐⭐

**理由:**
1. ✅ **根本性解决**: 消除序列化瓶颈
2. ✅ **可扩展性**: 增加分区数即可线性扩展
3. ✅ **数据隔离**: 各分区独立,故障隔离好
4. ✅ **适合Python**: 多进程绕过GIL

---

## 实施路线图

### Phase 1: 原型验证 (1-2天)

- [ ] 验证XTDATA API进程级订阅支持
- [ ] 实现简单的2分区原型
- [ ] 测试性能提升
- [ ] 验证数据完整性

### Phase 2: 完整实现 (3-5天)

- [ ] 实现StockPartitioner
- [ ] 实现PartitionedWorker
- [ ] 实现结果汇总逻辑
- [ ] 添加监控和日志

### Phase 3: 优化和部署 (2-3天)

- [ ] 性能调优
- [ ] 异常处理
- [ ] 文档编写
- [ ] 生产部署

---

## 风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| XTDATA API限制 | 高 | 提前验证API进程级订阅 |
| 结果汇总延迟 | 中 | 优化汇总逻辑,增加缓冲 |
| 进程崩溃 | 中 | 添加进程监控和自动重启 |
| 内存占用增加 | 低 | 每进程独立计算器,可控 |
| 调试复杂度 | 中 | 完善日志,添加监控面板 |

---

## 总结

**推荐方案: 股票池分区架构**

**核心优势:**
1. **性能提升 3-10x**: 消除序列化瓶颈
2. **架构清晰**: 分区隔离,易于理解和维护
3. **可扩展性强**: 增加CPU核心 = 增加分区
4. **Python友好**: 绕过GIL限制

**下一步:**
1. 验证XTDATA API支持
2. 实现2分区原型
3. 性能对比测试
4. 全面推广
