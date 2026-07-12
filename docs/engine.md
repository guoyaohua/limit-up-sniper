# 执行引擎

本文档说明 `engine/` 目录下的交易执行引擎模块。

## 一、模块总览

| 文件 | 职责 | 运行方式 |
|------|------|---------|
| `tick_processor.py` | Tick 数据消费、状态更新、决策触发 | 8 个 daemon 进程 |
| `trader.py` | 实盘下单执行 | 1 个 daemon 进程 |
| `simulator.py` | 模拟交易（调试/影子信号） | 1 个 daemon 进程 |
| `xt_callback.py` | XTQuant 交易回调处理 | 回调线程 |
| `xt_queries.py` | 持仓/委托/资产查询 | 定时线程 |

---

## 二、Tick 数据处理器 (`tick_processor.py`)

### 2.1 `on_data(datas, tick_queue, shadow_tick_queue, stock_info_dict, stop_flag, heartbeat_monitor)`

**角色**：XTData 行情回调函数，接收全市场 Tick 数据。

**职责**：
1. 更新心跳时间戳（标记数据源活跃）
2. 按股票代码稳定分片到 8 个 `tick_queue`（每个队列单进程 FIFO 消费）
3. 如果启用影子模式，同时分片到 4 个 `shadow_tick_queue`
4. 日志记录延迟和队列大小
5. 若延迟超过 `LATENCY_THRESHOLD`(20s) 且在交易时间内，设置 `stop_flag`

**Tick 数据格式**：
```python
datas = {
    'stock_code.SH': {
        'lastPrice': 15.23,      # 最新价
        'open': 14.50,           # 开盘价
        'high': 15.30,           # 最高价
        'low': 14.45,            # 最低价
        'lastClose': 14.20,      # 昨收
        'volume': 5000000,       # 成交量
        'amount': 72345000.0,    # 成交额
        'askPrice': [15.24, ...], # 卖盘五档价
        'bidPrice': [15.23, ...], # 买盘五档价
        'askVol': [1000, ...],    # 卖盘五档量
        'bidVol': [5000, ...],    # 买盘五档量
        'time': 1680000000000,    # 毫秒时间戳
        'openInt': 0,             # 集合竞价标记 (13/15=集合竞价)
    }
}
```

### 2.2 `process_tick_data(shared_data, tick_queue, order_queue, shadow_signal_mode)`

**角色**：Tick 消费工作进程的主循环函数。

**性能优化**：
- 本地缓存 `shared_data` 的常用引用，减少 Manager 代理开销
- 跳过集合竞价数据 (`openInt == 13 or 15`)

**完整处理逻辑**（对每只股票的每个 Tick）：

#### Step 1: 价格状态判断

```python
is_limit_up      = _check_same_price(lastPrice, limit_up_price)
is_near_limit_up = lastPrice >= limit_up_price * 0.98
is_down_limit    = _check_same_price(lastPrice, limit_down_price)
```

`_check_same_price` 使用 `< 0.0001` 的容差比较，并有 LRU 缓存优化。

#### Step 2: 状态机更新

```
                  ┌──────────────────┐
                  │  NOT_LIMIT_UP    │ ← 初始状态
                  │  (非涨停)         │
                  └────────┬─────────┘
                           │ 价格触及涨停
                           ▼
                  ┌──────────────────┐
                  │  LIMIT_UP        │
                  │  (涨停封板)       │
                  │  记录首次涨停时间  │
                  │  更新封单金额      │
                  └────────┬─────────┘
                           │ 价格跌离涨停
                           ▼
                  ┌──────────────────┐
                  │  LIMIT_UP_BROKEN │
                  │  (炸板)           │
                  │  记录炸板时间      │
                  │  记录炸板次数      │
                  └────────┬─────────┘
                           │ 价格重回涨停
                           ▼
                  ┌──────────────────┐
                  │  LIMIT_UP_REBOUND│
                  │  (回封)           │
                  └──────────────────┘
```

**涨停时更新的信号**：
- **封单金额**：`bidVol[0] × bidPrice[0]`（买一档量 × 买一档价）
- **首次涨停**：加入 `涨停池`，记录时间戳
- **炸板**：记录到 `炸板池`，包含炸板次数和持续时间

#### Step 3: 更新最高价 & 重算追踪止损

```python
if lastPrice > current_highest and stock_code in 持仓:
    update_highest_price(stock_code, lastPrice)
    calculate_trailing_stop_prices(lastPrice, ...)  # 重新计算 10 档止损
```

#### Step 4: 模拟成交检查

仅在 `DEBUG_MODE` 或影子信号模式下：
- 调用 `check_order_successed()` 检查排板订单是否可能已成交
- 只用下单后的累计成交量消耗排队位置，封单减少可能来自撤单，不作为成交证据
- 必须满足“新增成交手数 ≥ 前方队列手数 + 本单手数”才确认整单成交
- 开板本身、旧委托缺少队列快照或行情字段异常时均保持未成交

#### Step 5: 决策触发

```python
if should_buy(shared_data, tick_data, stock_code, ...):
    order_queue.put(buy_order)

if should_cancel(shared_data, tick_data, stock_code, ...):
    order_queue.put(cancel_order)

if should_sell(shared_data, stock_code, tick_data, ...):
    order_queue.put(sell_order)
```

### 2.3 `check_order_successed(shared_data, stock_code, tick_data, ...)`

**功能**：在模拟环境中判断排板买单是否已成交。

**判断逻辑**：
1. 下单时保存累计成交手数、前方买一队列手数和本单手数。
2. 后续仅计算 `traded_lots = current_volume - submitted_volume`。
3. 只有股票仍封板且 `traded_lots ≥ queue_ahead_lots + order_lots` 时确认整单成交。
4. 买一封单减少不计入成交量，避免把其他订单撤单误判为本单成交。
5. 开板不自动确认成交；无法取得完整、合法的队列证据时按未成交处理。

### 2.4 `create_whole_quote_task(stock_pool, stock_info_dict, tick_queue, shadow_tick_queue)`

**功能**：订阅全市场行情并监控回调健康状态。

**流程**：
1. 调用 `xtdata.subscribe_whole_quote(code_list=['沪深A股'], callback=on_data)`
2. 失败自动重试
3. 进入心跳监控循环（每 5 秒检查一次）：
   - 检查回调计数是否增长
   - 如果停滞超过超时时间 → 发送告警邮件 → 设置 `stop_flag`
   - 如果发现前次暂停，尝试重新连接

---

## 三、实盘交易器 (`trader.py`)

### 3.1 `run_xt_trader_task(order_queue, shared_data)`

**角色**：消费 `order_queue`，执行真实的下单操作。

**初始化阶段**：
1. 通过 `get_trader_entity()` 建立 XTQuant 交易连接
2. 处理昨日持仓的盘前卖出策略
3. 为每只持仓股计算追踪止损价格数组
4. 启动持仓/委托定时查询线程（每 2 秒）
5. 计算单只股票最大持仓金额 = 总资产 ÷ MAX_HOLDING_COUNT

**主循环**：从 `order_queue` 取出委托指令，按类型执行：

#### 买入处理

```python
order = order_queue.get(timeout=1)
if order['委托类型'] == OrderType.BUY:
    # 1. 查询可用资金
    cash = query_stock_asset(xt_trader, acc)['cash']

    # 2. 检查是否已持仓（防重复买入）
    positions = query_stock_positions(xt_trader, acc)

    # 3. 检查是否已有挂单
    orders = query_stock_orders(xt_trader, acc)

    # 4. 计算买入数量
    max_amount = total_asset / MAX_HOLDING_COUNT
    buy_volume = int(max_amount / limit_up_price / 100) * 100  # 整百取整

    # 5. 波动率加权仓位 (U6)
    amplitude = stock_info['近20日平均振幅']
    volatility_factor = VOLATILITY_TARGET / amplitude  # 波动大→减仓，波动小→加仓
    volatility_factor = clamp(volatility_factor, 0.5, 1.5)
    buy_volume = int(buy_volume * volatility_factor / 100) * 100

    # 6. 观察名单减仓 (U5)
    if stock_code in 观察名单:
        buy_volume = int(buy_volume * WATCHLIST_POSITION_RATIO / 100) * 100

    # 7. 下单
    xt_trader.order_stock(
        acc, stock_code, xtconstant.STOCK_BUY,
        buy_volume, xtconstant.FIX_PRICE, limit_up_price
    )

    # 8. 记录交易日志
    save_trade_log(trade_record)
```

#### 卖出处理

```python
if order['委托类型'] == OrderType.SELL:
    # 1. 查询持仓
    position = query_stock_positions(xt_trader, acc)
    sell_volume = available_volume - remaining_volume

    # 2. 根据报价类型下单
    if price_type == 'FIX_PRICE':
        xt_trader.order_stock(..., xtconstant.FIX_PRICE, price)
    else:  # MARKET
        xt_trader.order_stock(..., xtconstant.LATEST_PRICE, 0)  # 市价

    # 3. 记录交易日志
    save_trade_log(trade_record)
```

#### 撤单处理

```python
if order['委托类型'] == OrderType.CANCEL:
    # 1. 查询可撤单列表
    cancelable = query_stock_orders(xt_trader, acc, cancelable_only=True)

    # 2. 过滤：排除跌停价挂卖单（不撤）
    # 3. 逐笔撤单
    for order_id in cancelable[stock_code]:
        xt_trader.cancel_order_stock_sysid(acc, order_sys_id)

    # 4. 更新撤单计数
    shared_data['撤单次数'][stock_code] += 1
```

---

## 四、模拟交易器 (`simulator.py`)

### 4.1 `run_xt_trader_simulator(order_queue, shared_data, shadow_signal_mode)`

**功能**：在不执行真实交易的情况下模拟交易执行，用于：
- `DEBUG_MODE` 调试
- 影子信号并行回测

**与实盘交易器的区别**：

| 项目 | 实盘 | 模拟 |
|------|------|------|
| 资金 | 真实账户资金 | 虚拟资金（影子 1000 万/调试 3 万） |
| 下单 | 调用 xt_trader.order_stock() | 直接更新 shared_data |
| 成交 | 等待回报 | 根据排板/扫板模式模拟 |
| 卖出 | 支持 | 抛出 NotImplementedError（暂未实现） |

**三种买入模拟模式**：
1. **模拟成交**：直接将订单标记为已成交，更新持仓状态
2. **排板模拟**：记录下单时的封单量和成交量，等待 `check_order_successed()` 判断
3. **扫板模拟**：直接标记为成交（假设扫板即成交）

---

## 五、XTQuant 回调处理 (`xt_callback.py`)

### 5.1 `MyXtQuantTraderCallback` 类

继承 `XtQuantTraderCallback`，处理交易接口的各种回调事件：

| 方法 | 触发时机 | 处理方式 |
|------|---------|---------|
| `on_disconnected()` | 与 QMT 断开连接 | 日志 + 邮件告警 |
| `on_stock_order()` | 委托状态变化 | 日志记录所有字段 |
| `on_stock_trade()` | 成交回报 | 日志记录成交详情 |
| `on_order_error()` | 委托错误 | 日志 + 邮件告警 |
| `on_cancel_error()` | 撤单错误 | 日志 + 邮件告警 |
| `on_order_stock_async_response()` | 异步委托响应 | 日志记录 |
| `on_account_status()` | 账户状态变化 | 日志记录 |

### 5.2 `get_trader_entity(logger, client_path, stock_account, strategy_name)`

**功能**：创建并连接 XTQuant 交易实体。

**返回**：`(xt_trader, acc)` — 交易器实例和账户对象

**流程**：
1. 创建 `XtQuantTrader(client_path, session_id=timestamp)`
2. 创建 `StockAccount(stock_account)`
3. 启动交易线程
4. 连接服务（重试直到成功）
5. 注册回调 `MyXtQuantTraderCallback`
6. 订阅账户（重试直到成功）

---

## 六、查询封装 (`xt_queries.py`)

### 6.1 数据转换函数

| 函数 | 功能 |
|------|------|
| `xtposition_to_dict(xtpositions)` | XtPosition 列表 → `{stock_code: JSON}` |
| `group_xtorders_by_stock_code(xtorders)` | XtOrder 列表 → `{stock_code: [JSON, ...]}` |
| `xtasset_to_dict(xtasset)` | XtAsset → `{cash, total_asset, ...}` |

### 6.2 查询函数

| 函数 | 功能 | 重试 |
|------|------|------|
| `query_stock_asset(xt_trader, acc)` | 查询账户资产 | 循环重试直到非 None |
| `query_stock_positions(xt_trader, acc)` | 查询所有持仓 | 异常时邮件告警 |
| `query_stock_orders(xt_trader, acc, cancelable_only)` | 查询委托 | 支持仅查可撤单 |

### 6.3 `query_positions_and_orders_task(xt_trader, acc, shared_data)`

**功能**：作为守护线程运行，每 2 秒同步一次持仓和委托状态到 `shared_data`。

使用 `schedule` 库调度：
```python
schedule.every(2).seconds.do(safe_query_positions_and_orders, xt_trader, acc, shared_data)
```

每次执行：
1. 查询所有持仓 → 更新 `shared_data['持仓状态']`
2. 查询所有委托 → 更新 `shared_data['委托状态']`
3. 在 STOP_TIME 后退出循环
