# Level2 深度行情处理

本文档说明 `level2/` 目录下的 Level2 逐笔行情数据处理系统。

> 注：Level2 模块有独立的架构设计文档 `level2/ARCHITECTURE.md`（约 850 行），本文档为精简版说明。

## 一、模块总览

```
level2/
├── ARCHITECTURE.md          # 完整架构设计文档
├── main.py                  # 系统入口
├── main_optimized.py        # 优化版入口
├── enums.py                 # 枚举与常量定义
├── models.py                # 数据模型
├── buffers/
│   ├── ring_buffer.py       # 共享内存环形缓冲区
│   └── deque_buffer.py      # Deque 缓冲区（备选方案）
├── calculators/
│   └── l2_calculators.py    # 计算引擎（封单额/资金流）
└── consumers/
    ├── worker.py            # 工作进程
    ├── unified_worker.py    # 统一工作进程
    └── threaded_worker.py   # 线程工作进程
```

---

## 二、设计目标

Level2 系统处理的是 A 股市场最底层的逐笔数据（L2 行情），对性能要求极高：

| 指标 | 目标值 | 说明 |
|------|--------|------|
| 回调延迟 | < 10μs | 超轻量级回调，不做任何计算 |
| 缓冲区溢出率 | < 0.1% | 数据完整性保障 |
| 端到端处理延迟 | < 100ms | 从数据到达到信号产出 |
| 内存占用 | < 4GB | Ring Buffer + 计算状态 |
| CPU 使用率 | < 80% | 多核并行 |

---

## 三、核心需求

### 需求 1：全市场大单资金流

追踪所有股票的大单（≥50 万股或 ≥100 万元）买卖情况：
- 超大单：≥ 50 万股
- 大单：≥ 10 万股
- 主力 = 超大单 + 大单
- 散户 = 中单 + 小单

### 需求 2：涨停封单金额

实时计算涨停股的封单量：
- 基准封单量：涨停时刻买一档的委托量
- 实时封单量 = 基准量 + 新增买入委托 (delta_buy) - 被消耗委托 (delta_consume)
- 封单金额 = 封单量 × 涨停价
- **预警阈值**：封单金额 < 2000 万元

### 需求 3：板上资金流向

涨停期间的资金流入流出监控，帮助判断封板质量。

---

## 四、三层数据流架构

```
Layer 1: XTDATA SDK 回调
    │
    │  on_l2quote_callback()     ← 逐笔行情（价格/量/盘口）
    │  on_l2order_callback()     ← 逐笔委托（挂单/撤单）
    │  on_l2transaction_callback() ← 逐笔成交
    │
    │  [目标: < 10μs，只做 msgpack 编码 + 写入缓冲区]
    │
    ▼
Layer 2: 共享内存环形缓冲区
    │
    │  l2quote_buffer:       100K slots × 1024 bytes
    │  l2order_buffer:       1.5M slots × 256 bytes
    │  l2transaction_buffer: 500K slots × 256 bytes
    │
    │  [零拷贝写入，覆盖策略处理溢出]
    │
    ▼
Layer 3: 多进程消费者池
    │
    │  8 个 Worker 进程并行消费
    │  ├── SealAmountCalculator  → 封单额计算
    │  ├── CapitalFlowCalculator → 资金流计算
    │  └── Aggregation           → 结果汇总
    │
    ▼
  输出: 封单金额/资金流信号 → 写入 shared_data / 告警
```

---

## 五、枚举与常量 (`enums.py`)

### 5.1 市场枚举

```python
class Market(Enum):
    SHANGHAI = 'SH'
    SHENZHEN = 'SZ'
```

### 5.2 委托方向

```python
class EntrustDirection(IntEnum):
    BUY = 1           # 买入
    SELL = 2          # 卖出
    CANCEL_BUY = 3    # 撤买（上海）
    CANCEL_SELL = 4   # 撤卖（上海）
```

### 5.3 成交标志

```python
class TradeFlag(IntEnum):
    BUY = 1     # 外盘（主动买入）
    SELL = 2    # 内盘（主动卖出）
    CANCEL = 3  # 撤单（深圳）
```

### 5.4 订单分级

```python
class OrderThreshold:
    SUPER_LARGE_VOLUME = 500_000    # 超大单: ≥ 50万股
    LARGE_VOLUME = 100_000          # 大单:   ≥ 10万股
    SEAL_ALERT_AMOUNT = 20_000_000  # 封单预警: < 2000万元
```

### 5.5 沪深差异处理

**关键区别**：上海和深圳交易所的 L2 数据格式有本质差异。

| 特征 | 上海 | 深圳 |
|------|------|------|
| 推送方式 | 3 秒批量推送 | 逐笔实时推送 |
| 撤单标识 | `entrustDirection = 3/4` | `tradeFlag = 3` |
| 数据完整性 | 不推送已全部成交的委托 | 完整推送 |
| 9:25 集合竞价峰值 | 50万-100万笔 | 较少 |

**核心函数**：

```python
def is_cancel_order(stock_code, order_data=None, trans_data=None) -> bool:
    """判断是否为撤单（自动区分沪深逻辑）"""
    market = get_market(stock_code)
    if market == Market.SHANGHAI:
        return order_data['entrustDirection'] in (3, 4)
    else:  # SHENZHEN
        return trans_data['tradeFlag'] == 3
```

---

## 六、数据模型 (`models.py`)

### 6.1 `OrderInfo` — 委托信息

```python
@dataclass
class OrderInfo:
    entrust_no: int       # 委托号
    stock_code: str       # 股票代码
    direction: int        # 买卖方向
    total_volume: int     # 委托总量
    price: float          # 委托价格
    filled_volume: int    # 已成交量
    filled_amount: float  # 已成交金额
    timestamp: int        # 时间戳
    last_order_size: str  # 最新分档 (SUPER_LARGE/LARGE/MEDIUM/SMALL)

    @property
    def is_large_order(self) -> bool   # 是否为大单 (≥10万股)
    @property
    def is_super_large_order(self) -> bool  # 超大单 (≥50万股)
```

### 6.2 `SealAmountInfo` — 封单信息

```python
@dataclass
class SealAmountInfo:
    stock_code: str
    limit_price: float      # 涨停价
    baseline_volume: int    # 基准封单量（涨停时刻的买一量）
    baseline_time: int      # 基准时间
    delta_buy: int          # 新增买入委托量
    delta_consume: int      # 被消耗委托量
    is_limit_up: bool       # 当前是否涨停
    last_quote_time: int    # 最新行情时间

    @property
    def current_volume(self) -> int:
        """实时封单量"""
        return max(0, self.baseline_volume + self.delta_buy - self.delta_consume)

    @property
    def seal_amount(self) -> float:
        """实时封单金额（元）"""
        return self.current_volume * self.limit_price

    @property
    def seal_amount_wan(self) -> float:
        """封单金额（万元）"""
        return self.seal_amount / 10000

    @property
    def is_weak_seal(self) -> bool:
        """是否弱封单（< 2000万）"""
        return self.seal_amount < OrderThreshold.SEAL_ALERT_AMOUNT

    def reset_baseline(self, volume, timestamp):
        """重置基准（开板后重新涨停时调用）"""
```

### 6.3 `CapitalFlowStats` — 资金流统计

```python
@dataclass
class CapitalFlowStats:
    super_large_buy: float   # 超大单买入金额
    super_large_sell: float  # 超大单卖出金额
    large_buy: float
    large_sell: float
    medium_buy: float
    medium_sell: float
    small_buy: float
    small_sell: float

    @property
    def net_main(self) -> float:
        """主力净流入 = 超大单净流入 + 大单净流入"""
    @property
    def net_retail(self) -> float:
        """散户净流入 = 中单净流入 + 小单净流入"""
```

---

## 七、共享内存环形缓冲区 (`buffers/ring_buffer.py`)

### 7.1 设计目标

在 XTDATA 的回调函数中，必须在 **< 10μs** 内完成数据写入。传统的 Queue/Pipe 有锁开销，无法满足要求。因此采用基于共享内存的 Lock-free 环形缓冲区。

### 7.2 内存布局

```
┌─────────────────────────────────────────────────────────┐
│  Header (24 bytes)                                       │
│  ┌──────────┬──────────┬────────────────┐               │
│  │write_pos │read_pos  │overflow_count  │               │
│  │(8 bytes) │(8 bytes) │(8 bytes)       │               │
│  └──────────┴──────────┴────────────────┘               │
│                                                          │
│  Slot 0                                                  │
│  ┌──────┬──────────┬───────────┬──────────────────────┐ │
│  │valid │timestamp │data_length│data (fixed size)     │ │
│  │(1B)  │(8B)      │(4B)       │(configurable)        │ │
│  └──────┴──────────┴───────────┴──────────────────────┘ │
│                                                          │
│  Slot 1 ... Slot N-1                                     │
└─────────────────────────────────────────────────────────┘
```

### 7.3 缓冲区规格

| 缓冲区 | 槽数 | 每槽大小 | 总内存 | 用途 |
|--------|------|---------|--------|------|
| l2quote | 100,000 | 1024B | ~100MB | 逐笔行情 |
| l2order | 1,500,000 | 256B | ~384MB | 逐笔委托 (应对9:25峰值) |
| l2transaction | 500,000 | 256B | ~128MB | 逐笔成交 |

### 7.4 性能优化技巧

| 优化 | 效果 | 说明 |
|------|------|------|
| msgpack 替代 JSON | 5-10x 加速 | 二进制序列化 |
| 时间戳缓存 (100μs) | -2μs/写入 | 同一 100μs 窗口复用时间戳 |
| 批量时间戳共享 | -1.5μs/条 | 同批数据共用一个时间戳 |
| memoryview 零拷贝 | -2-3μs/写入 | 避免内存复制 |
| 预编译 msgpack.Packer | -2-3μs/写入 | 复用 Packer 实例 |

### 7.5 溢出策略

当写入指针追上读取指针时，**覆盖最旧数据**：

```python
if write_pos catches up to read_pos:
    overflow_count += 1
    read_pos = write_pos + 1  # 强制推进读指针
```

**设计决策**：对于实时交易系统，新数据永远比旧数据更重要。丢弃旧数据比阻塞写入好。

---

## 八、计算引擎 (`calculators/l2_calculators.py`)

### 8.1 `SealAmountCalculator` — 封单额计算器

**核心方法**：

| 方法 | 数据源 | 功能 |
|------|--------|------|
| `on_l2quote(stock_code, quote_data)` | 逐笔行情 | 检测涨停状态，获取基准封单量 |
| `on_l2order(stock_code, order_data)` | 逐笔委托 | 累加涨停价买入委托 (delta_buy) |
| `on_l2transaction(stock_code, trans_data)` | 逐笔成交 | 累加消耗量 (delta_consume) |
| `get_seal_amount(stock_code)` | - | 查询实时封单金额 |
| `get_weak_seal_stocks()` | - | 获取所有弱封单股票 (< 2000万) |

**封单计算公式**：

$$\text{封单量} = \max(0,\ \text{基准量} + \Delta_{\text{buy}} - \Delta_{\text{consume}})$$

$$\text{封单金额} = \text{封单量} \times \text{涨停价}$$

### 8.2 `Level2Calculator` — 统一计算器

整合封单额计算和资金流计算的统一接口，分发数据到对应的专项计算器。

---

## 九、系统入口 (`main.py`)

### 9.1 `Level2DataSystem` 类

**启动流程**：

```
1. 初始化 (stock_list, consumer_count, enable_flags)
2. 创建 3 个共享内存缓冲区 (Level2BufferManager)
3. 创建消费者进程池 (create_consumer_pool)
4. 订阅 XTDATA 的三类 L2 数据
5. 注册回调函数
6. 进入监控循环
```

**监控指标**（每 30 秒输出）：
- 各缓冲区使用率
- 溢出次数和溢出率
- 消费者进程健康状态

**关闭流程**：
1. 取消 XTDATA 订阅
2. 停止消费者进程池
3. 清理共享内存
4. 退出
