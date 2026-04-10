# 数据管理

本文档说明 `data/` 目录下的数据管理模块，包括多进程共享数据结构、序列化机制、板块映射。

## 一、模块总览

| 文件 | 职责 |
|------|------|
| `shared_data.py` | 多进程共享数据结构的初始化与管理 |
| `serialization.py` | 共享数据的序列化、反序列化、备份与恢复 |
| `sector_mapping.py` | 板块-股票映射数据的加载 |
| `helpers.py` | 数据转换工具函数 |

---

## 二、共享数据结构 (`shared_data.py`)

### 2.1 `init_shared_data()` — 初始化

这是整个系统数据层的核心函数，创建跨进程共享的数据字典。

**参数**：
```python
def init_shared_data(
    stock_pool,           # 股票池列表
    stock_info_dict,      # 每只股票的基础信息
    strong_stocks,        # 强势股列表
    pre_trade_date,       # 上一个交易日
    shadow_signal_mode,   # 是否影子信号模式
    base_shared_data,     # 影子模式时的基础共享数据
    new_stock_list        # 新股列表
) -> dict
```

### 2.2 完整数据结构

以下是 `shared_data` 字典的完整键值结构：

#### 静态信息区

```python
'股票信息': Manager().dict({
    'stock_code': {
        '涨停价': float,         # 今日涨停价
        '跌停价': float,         # 今日跌停价
        '流通股本': int,         # 流通股数量
        '股票名称': str,
        '昨日收盘价': float,
        '60日均线': float,
        '5日平均成交量': int,
        '5日平均成交额': float,
        '概念板块': str,         # JSON: 所属概念板块列表
        '行业板块': str,         # JSON: 所属行业板块
    }
})

'强势股票': Manager().list([...])   # 涨停基因 Top 1000 股票代码
```

#### 实时状态区

```python
'股票状态信号': Manager().dict({
    'stock_code': {
        '股票状态':     Value('i', NOT_LIMIT_UP),    # 涨停状态枚举
        '下单状态':     Value('i', NOT_ORDERED),     # 委托状态枚举
        '封单金额':     Value('d', 0.0),             # 买一封单金额（元）
        '前一价格':     Value('d', 0.0),             # 上一Tick价格
        '拉板所需资金': Value('d', 0.0),             # 拉升到涨停所需资金
        '下单时成交量': Value('i', 0),               # 下单时刻的成交量
        '下单时封单量': Value('i', 0),               # 下单时刻的封单量
        '最高价':       Value('d', 0.0),             # 盘中最高价
        '止盈止损价格列表': Array('d', [0]*10),       # 10档止损价格
        '目标剩余仓位':   Array('i', [0]*10),        # 10档目标仓位
        '止盈_5pct':   Value('i', 0),               # 5%止盈是否已触发
        '止盈_8pct':   Value('i', 0),               # 8%止盈是否已触发
        '止盈_10pct':  Value('i', 0),               # 10%止盈是否已触发
    }
})

'持仓状态': Manager().dict({
    'stock_code': JSON字符串,   # 包含 volume, can_use_volume, avg_price 等
})

'委托状态': Manager().dict({
    'stock_code': JSON字符串,   # 包含各委托的状态、价格、数量等
})
```

#### 涨停跟踪区

```python
'涨停池': Manager().dict({
    'stock_code': timestamp,   # 首次涨停的毫秒时间戳
})

'炸板池': Manager().dict({
    'stock_code': {
        '炸板次数': int,
        '炸板时间': timestamp,
        '持续时间': int,        # 秒
    }
})

'黑名单': Manager().dict({
    'stock_code': '原因说明',
})

'观察名单': Manager().dict({
    'stock_code': {
        '加入时间': timestamp,
        '原因': str,
    }
})
```

#### 板块效应区

```python
'概念板块效应': Manager().dict({
    'BK代码': JSON字符串,    # 包含板块名、涨幅、领涨股列表
})

'行业板块效应': Manager().dict({
    'BK代码': JSON字符串,    # 同上
})

'个股资金流入': Manager().list([
    'stock_code', ...         # 资金净流入信号的股票列表
])

'概念板块': Manager().dict(),     # 板块→股票列表
'行业板块': Manager().dict(),     # 板块→股票列表
'概念板块成分股': Manager().dict(), # 股票→板块列表 (反向映射)
'行业板块成分股': Manager().dict(), # 股票→板块列表 (反向映射)
```

#### 市场情绪区

```python
# 基础数值
'市场情绪评分':         Value('d', 5.0),    # 综合评分 1-10
'市场情绪_涨停板数量':  Value('i', 0),
'市场情绪_炸板数量':    Value('i', 0),
'市场情绪_炸板率':      Value('d', 0.0),
'市场情绪_昨日首板表现': Value('d', 0.0),   # 昨日首板股票今日平均涨幅
'市场情绪_昨日涨停表现': Value('d', 0.0),   # 昨日涨停股票今日平均涨幅

# 大盘指数
'大盘指数_上证指数':  Value('d', 0.0),    # 000001.SH 涨跌幅
'大盘指数_沪深300':   Value('d', 0.0),    # 000300.SH
'大盘指数_创业板指':  Value('d', 0.0),    # 399006.SZ
'大盘指数_深证成指':  Value('d', 0.0),    # 399001.SZ

# 时间戳
'大盘指数更新时间':     Value('d', 0.0),
'昨日涨停表现更新时间': Value('d', 0.0),
```

#### LLM 预测区

```python
'板块优先级': Manager().dict({
    '板块名称': weight (0.0-1.0),   # LLM 预测的优先板块及权重
})
```

#### 辅助区

```python
'撤单次数': Manager().dict({
    'stock_code': int,           # 每只股票的撤单次数
})

'盘前持仓': Manager().dict({
    'stock_code': JSON字符串,     # 昨日收盘时的持仓快照
})

'昨日涨停股票': Manager().list([...])    # 昨日涨停列表
'昨日首板涨停股票': Manager().list([...]) # 昨日首板涨停列表
'新股列表': Manager().list([...])        # 新上市 < 5 天的股票
```

### 2.3 初始化性能优化 (v2.4.1)

**问题**：创建数百个 `Manager().dict()` 代理非常慢（~2 分钟）

**解决**：使用 `ThreadPoolExecutor(max_workers=12)` 并行创建 Manager 代理

```python
# 定义 13 个代理创建任务
proxy_specs = [
    ('股票信息', lambda: manager.dict(stock_info_dict)),
    ('持仓状态', lambda: manager.dict()),
    ('委托状态', lambda: manager.dict()),
    # ...
]

# 并行创建
with ThreadPoolExecutor(max_workers=12) as executor:
    futures = {executor.submit(spec[1]): spec[0] for spec in proxy_specs}
    for future in as_completed(futures):
        name = futures[future]
        proxies[name] = future.result()
```

**效果**：初始化时间从 ~2 分钟缩短到 10-20 秒。

### 2.4 影子信号模式

影子信号模式下的 `init_shared_data()` 创建一个**轻量级副本**：

- 股票信息、板块映射等静态数据**直接引用** `base_shared_data`（不复制）
- 仅创建独立的状态信号、涨停池、黑名单等可变数据
- 使用 7 个 worker 线程（vs 正常模式 12 个）

---

## 三、序列化与持久化 (`serialization.py`)

### 3.1 序列化: `deep_serialize(value) -> dict`

将 `multiprocessing` 的特殊对象递归转换为可 pickle 的普通 Python 对象。

**转换规则**：

| 源类型 | 序列化格式 |
|--------|-----------|
| `Value('i', 42)` | `{'_type_': 'Value', '_typecode_': 'i', '_value_': 42}` |
| `Value('d', 3.14)` | `{'_type_': 'Value', '_typecode_': 'd', '_value_': 3.14}` |
| `Array('d', [1,2,3])` | `{'_type_': 'Array', '_typecode_': 'd', '_value_': [1,2,3]}` |
| `Manager().dict({...})` | `{'_type_': 'Dict', ...}` (递归序列化内层) |
| `Manager().list([...])` | `{'_type_': 'List', ...}` (递归序列化内层) |
| 基础类型 (int/float/str) | 原样保留 |

### 3.2 反序列化: `deep_restore(value, depth=0) -> object`

将序列化后的字典恢复为原始的 multiprocessing 对象。

**性能优化**：
- 检测到股票状态信号字典（>500 个键）时，使用 `_batch_create_stock_signals()` 批量恢复
- 检测到 Manager 代理字典时，使用 `_parallel_restore_manager_proxies()` 并行恢复

### 3.3 数据备份: `save_shared_data(shared_data, data_dir, prefix)`

**原子写入流程**：
```
1. serialize = deep_serialize(shared_data)
2. 写入临时文件 .tmp
3. fsync 确保落盘
4. 如果旧备份文件存在 → 重命名为 .bak
5. 将 .tmp 重命名为最终文件名
```

**文件命名**：`{prefix}_{TODAY}.pkl`（如 `shared_data_20250410.pkl`）

### 3.4 数据恢复: `load_shared_data(data_dir, prefix)`

1. 检查今日备份文件是否存在
2. pickle.load() 加载
3. deep_restore() 恢复 multiprocessing 对象
4. 返回完整的 shared_data 或 None

### 3.5 自动备份任务: `start_shared_data_backup_task(shared_data, backup_interval=2)`

启动守护线程，每 `backup_interval` 秒自动备份一次 shared_data。

**异常处理**：
- `BrokenPipeError`：Manager 连接断开（进程崩溃），每 30 秒重试
- `OSError`：磁盘问题，每 30 秒重试
- 其他异常：日志记录，每 30 秒重试

### 3.6 股票信号批量创建: `_batch_create_stock_signals(stock_codes, signals_data)`

**功能**：并行创建所有股票的状态信号 dict。

每只股票的信号结构（13 个字段）：
```python
{
    '股票状态':     Value('i', NOT_LIMIT_UP),
    '下单状态':     Value('i', NOT_ORDERED),
    '封单金额':     Value('d', 0.0),
    '前一价格':     Value('d', 0.0),
    '拉板所需资金': Value('d', 0.0),
    '下单时成交量': Value('i', 0),
    '下单时封单量': Value('i', 0),
    '最高价':       Value('d', 0.0),
    '止盈止损价格列表': Array('d', [0.0]*10),
    '目标剩余仓位':   Array('i', [0]*10),
    '止盈_5pct':   Value('i', 0),
    '止盈_8pct':   Value('i', 0),
    '止盈_10pct':  Value('i', 0),
}
```

使用 `ThreadPoolExecutor(max_workers=min(32, max(4, count//100)))` 并行创建。

---

## 四、板块映射 (`sector_mapping.py`)

### 4.1 `load_sector_mapping(sector_type, mapping_file, exclude_stocks)`

**功能**：加载板块-股票映射数据。

**数据源**：同花顺板块分类 JSON 文件

**文件路径**：
- 概念板块：`output/concept_sectors/THS/sector_to_stocks_mapping_latest.json`
- 行业板块：`output/industry_sectors/THS/sector_to_stocks_mapping_latest.json`

**数据结构**：
```json
{
    "BK0001": {"name": "半导体", "stocks": ["000001.SZ", "600000.SH", ...]},
    "BK0002": {"name": "AI应用", "stocks": [...]},
    ...
}
```

### 4.2 `load_yesterday_first_limit_up_stock_list(pre_trade_date, stock_pool)`

**功能**：加载昨日首板涨停股票列表。

**数据源**：
- 优先：本地文件 `output/涨停列表/首次涨停_{date}.txt`
- 备用：同花顺板块代码 "883993" 的成分股

### 4.3 `load_yesterday_limit_up_stock_list(pre_trade_date, stock_pool)`

**功能**：加载昨日全部涨停股票列表。

**数据源**：同上逻辑，文件为 `涨停_{date}.txt`，板块代码 "883986"。

---

## 五、数据转换工具 (`helpers.py`)

### 5.1 `transform_dict_mapping(original_dict, multi_values=True)`

**功能**：反转字典映射方向。

```python
# 输入: 板块 → 股票列表
{'BK001': ['000001', '000002'], 'BK002': ['000002', '000003']}

# 输出: 股票 → 板块列表
{'000001': ['BK001'], '000002': ['BK001', 'BK002'], '000003': ['BK002']}
```

用于构建 `概念板块成分股`（股票→所属板块列表）的反向映射。

### 5.2 `get_safe_typecode(value)`

**功能**：安全获取 multiprocessing Value/Array 对象的 typecode。

**fallback 链**：
1. 直接读取 `_typecode` 属性
2. 遍历 `_obj._type_` 层级
3. 读取 `_value._type_` 属性
4. 从实际值的 Python 类型推断（bool→'b', int→'i', float→'f'）
