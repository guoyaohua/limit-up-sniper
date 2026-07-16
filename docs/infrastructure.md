# 基础设施

本文档说明 `infra/` 目录下的基础设施模块。

## 一、模块总览

| 文件 | 职责 |
|------|------|
| `common_enums.py` | 交易相关枚举类型定义 |
| `task_manager.py` | 进程/线程生命周期管理 |
| `trade_log.py` | 交易日志持久化与涨停列表保存 |
| `utils.py` | 邮件发送、日志初始化 |
| `data_helpers.py` | 数据连接、时间/价格工具函数 |

---

## 二、枚举定义 (`common_enums.py`)

### 2.1 盘前卖出策略

```python
class PreMarketSellStrategy(Enum):
    TIERED_SELL = 'tiered_sell'              # 分档卖出
    FIXED_PREMIUM_SELL = 'fixed_premium_sell' # 固定溢价卖出（默认）
    LIMIT_UP_SELL = 'limit_up_sell'          # 涨停价卖出
    NO_PRE_MARKET_SELL = 'no_pre_market_sell' # 不做盘前卖出
```

### 2.2 股票涨停状态

```python
class StockLimitStatusInt(IntEnum):
    NOT_LIMIT_UP = 1       # 非涨停
    LIMIT_UP = 2           # 涨停封板中
    LIMIT_UP_BROKEN = 3    # 炸板（曾涨停但已打开）
    LIMIT_UP_REBOUND = 4   # 回封（炸板后重新封住）
```

### 2.3 委托状态

```python
class StockOrderStatus(str, Enum):
    NOT_ORDERED = '未下单'
    ORDER_PLACED = '已下单'
    ORDER_SUCCESS = '成交'
    ORDER_FAILED = '失败'
    ORDER_CANCELLED = '已撤单'
    ORDER_PARTIAL = '部分成交'
```

### 2.4 委托类型

```python
class OrderType(Enum):
    BUY = 'buy'
    SELL = 'sell'
    CANCEL = 'cancel'
```

### 2.5 报价类型

```python
class PriceType(Enum):
    # 上海
    SH_LATEST_PRICE = xtconstant.LATEST_PRICE     # 最新价（市价）
    SH_FIX_PRICE = xtconstant.FIX_PRICE           # 指定价（限价）

    # 深圳
    SZ_MARKET_PEER_PRICE_FIRST = ...  # 对手方最优价
    SZ_MARKET_SZ_CONVERT_5_CANCEL = ...  # 最优五档即时成交剩余撤销
```

### 2.6 XTQuant 委托回报状态

```python
class OrderStatus(Enum):
    UNREPORTED = 48           # 未报
    WAIT_REPORTING = 49       # 待报
    REPORTED = 50             # 已报
    REPORTED_CANCEL = 51      # 已报待撤
    PARTIAL_FILLED_CANCEL = 52 # 部成待撤
    PARTIAL_FILLED = 53       # 部分成交
    FILLED = 54               # 已成交
    CANCELLED = 55            # 已撤
    PARTIAL_CANCELLED = 56    # 部撤
    JUNK = 57                 # 废单
    UNKNOWN = 255             # 未知
```

---

## 三、任务管理器 (`task_manager.py`)

### 3.1 功能概述

管理所有后台进程和线程的生命周期，提供自动重启、健康监控、优雅退出等能力。

### 3.2 `TaskInfo` — 任务描述

```python
@dataclass
class TaskInfo:
    name: str                      # 任务名称
    target: Callable               # 目标函数
    args: tuple = ()               # 函数参数
    task_type: str = 'process'     # 'process' 或 'thread'
    restart_on_failure: bool = True # 失败后是否自动重启
    max_restart_count: int = 3     # 最大重启次数
    is_daemon: bool = True         # 是否为守护进程/线程
    heartbeat_timeout: int = 0     # 心跳超时（0=不监控）
```

### 3.3 `TaskManager` 类

**关键方法**：

| 方法 | 功能 |
|------|------|
| `register_task(task_info)` | 注册任务到管理器 |
| `start_task(name)` | 启动单个任务 |
| `start_all()` | 启动所有注册的任务 + 监控线程 |
| `stop_task(name, timeout=5)` | 优雅停止（超时后强制终止） |
| `restart_task(name)` | 停止后重新启动 |
| `check_task_health(name)` | 检查任务是否存活 |
| `shutdown()` | 关闭所有任务并退出 |
| `get_all_task_status()` | 查询所有任务状态 |

**任务状态枚举**：

```
PENDING → RUNNING → STOPPED
                  → FAILED → RESTARTING → RUNNING
```

**监控循环** (`_monitor_loop`)：
- 每 5 秒检查一次所有任务
- 发现死亡的任务（进程/线程不存活）→ 标记为 FAILED
- 如果 `restart_on_failure=True` 且未超过最大重启次数 → 自动重启
- 超过最大重启次数 → 发送邮件告警

**信号处理**：
- 注册 SIGINT / SIGTERM 处理器
- 触发时调用 `shutdown()` 优雅退出

### 3.4 `CallbackHeartbeatMonitor` 类

**功能**：监控 XTData 行情回调是否正常工作。

**核心属性**：
- `last_heartbeat_time`: Value('d') — 最后心跳时间
- `callback_count`: Value('i') — 回调计数

**方法**：

| 方法 | 功能 |
|------|------|
| `update()` | 更新心跳时间和计数器 |
| `is_healthy()` | 检查是否超时（默认 30 秒） |
| `check_and_notify()` | 检测不健康状态，首次时返回 True |

---

## 四、交易日志 (`trade_log.py`)

### 4.1 `save_trade_log(trade_record)`

将单条已受理委托保存为 JSON 文件。该文件是操作审计记录，不是成交回报；固定包含
`record_type=order_submission` 与 `execution_status=SUBMITTED_NOT_FILLED`，不得直接用于
收益配对。

**文件路径**：`output/trade_logs/{YYYYMMDD}/trade_{timestamp}_{stock_code}.json`

**记录格式**：
```json
{
    "record_type": "order_submission",
    "execution_status": "SUBMITTED_NOT_FILLED",
    "时间": "2025-04-10 10:23:45",
    "股票代码": "600000.SH",
    "股票名称": "XX股份",
    "委托类型": "buy",
    "委托价格": 15.23,
    "委托数量": 1000,
    "委托原因": "排板买入",
    "操作详情": "封单金额5000万,板块效应3个,领涨2个",
    "市场情绪": 7.5,
    "策略版本": "v1.1"
}
```

### 4.2 `save_trade_fill(fill_record)`

交易回调 `on_stock_trade` 将券商确认成交按“账户 + 策略 + `trade_id`”幂等落盘到
`fill_{trade_id}_{identity_hash}.json`，标记 `record_type=fill` 和
`execution_status=FILLED`，并记录券商成交时间与手续费；跨午夜回调仍写入实际成交日。
只有这些记录能进入日度盈亏配对；
重复回调不会重复计数，同机多账户或多策略也不会因成交号相同而互相覆盖。

### 4.3 `save_daily_limit_up_list(shared_data)`

从共享数据中提取涨停相关信息，生成三个文件：

| 文件 | 内容 |
|------|------|
| `output/涨停列表/涨停_{TODAY}.txt` | 今日所有涨停股票 |
| `output/涨停列表/首次涨停_{TODAY}.txt` | 今日首次涨停股票 |
| `output/涨停列表/炸板_{TODAY}.txt` | 今日炸板股票 |

**逻辑**：
1. 遍历 `shared_data['涨停池']`
2. 检查每只股票的状态信号（LIMIT_UP = 真涨停，否则 = 炸板）
3. 与 `昨日涨停股票` 交叉对比，区分首板和连板
4. 写入文件
5. 发送邮件汇总

---

## 五、工具函数 (`utils.py`)

### 5.1 `init_logger(name, log_dir, verbose=False)`

使用 loguru 初始化日志系统。

**日志文件分级**：

| 级别 | 文件 | 保留 |
|------|------|------|
| DEBUG | `{log_dir}/{name}_debug_{date}.log` | 3 天 |
| INFO | `{log_dir}/{name}_info_{date}.log` | 7 天 |
| WARNING | `{log_dir}/{name}_warning_{date}.log` | 30 天 |
| ERROR | `{log_dir}/{name}_error_{date}.log` | 30 天 |
| CRITICAL | `{log_dir}/{name}_critical_{date}.log` | 30 天 |

### 5.2 `send_email(subject, content, add_timestamp=True)`

通过 QQ 邮箱 SMTP 发送纯文本告警邮件。

**配置**：
- SMTP 服务器：`smtp.qq.com:465` (SSL)
- 发件人：环境变量 `SMTP_SENDER`
- SMTP 账户、密码及收发件地址：环境变量 `SMTP_USERNAME`、
  `SMTP_PASSWORD`、`SMTP_SENDER`、`SMTP_RECIPIENT`

### 5.3 `send_html_email(subject, html_content, attachments=None)`

发送 HTML 格式邮件，支持附件。用于发送市场状态报表。

### 5.4 `run_with_timeout(func, args, kwargs, timeout)`

使用 `ThreadPoolExecutor` 对函数执行设置超时限制。超时后尝试终止线程。

---

## 六、数据工具 (`data_helpers.py`)

### 6.1 `xtdata_connect(ip, port)`

连接 XTQuant 数据服务。

```python
def xtdata_connect(ip='127.0.0.1', port=58610):
    xtdata.reconnect(ip, port)
```

### 6.2 `get_pretrade_date(today) -> str`

获取上一个交易日日期。使用 akshare 的交易日历。

### 6.3 `is_trading_time() -> bool`

判断当前时间是否在交易时段内：
- 上午：9:29 ~ 11:31
- 下午：12:59 ~ 15:00

### 6.4 `_round_price(price) -> float`

将价格四舍五入到小数点后 2 位。使用 `Decimal` 的 `ROUND_HALF_UP` 模式确保精确。

### 6.5 `_check_same_price(price1, price2) -> bool`

比较两个价格是否相等（容差 < 0.0001 元）。

带 LRU 缓存优化（最多 10000 条缓存记录）。

### 6.6 `_calc_delay_time(time_stamp) -> float`

计算数据延迟（当前时间 - 数据时间戳），单位秒。

### 6.7 `_calc_limit_up_break_duration(now, limit_break_time) -> int`

计算炸板持续时间（秒），自动扣除午休 2 小时。
