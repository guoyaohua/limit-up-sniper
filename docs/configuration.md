# 配置参数手册

本文档详细说明 `config.py` 中的所有可配置参数。账号、路径和密钥只通过
环境变量注入；`.env.example` 仅是变量清单，不得填入真实值后提交。

## 一、全局开关

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `VERSION` | str | `'v1.0'` | 策略版本号 |
| `DEBUG_MODE` | bool | `True` | 兼容旧模块；由执行模式自动派生 |
| `ENABLE_SHADOW_SIGNAL` | bool | `False` | 由 `LIMIT_UP_ENABLE_SHADOW_SIGNAL` 控制 |
| `IS_LIVE_TRADING` | bool | `False` | 仅当执行模式为 `live` |
| `SECTOR_DATA_SOURCE` | str | `'THS'` | 板块数据源：`'THS'`(同花顺) 或 `'EM'`(东方财富) |

---

## 二、交易账户配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `LIMIT_UP_CLIENT_NAME` | `GJ_SIM` | 客户端：`GJ_SIM` 或 `CICC_LIVE` |
| `LIMIT_UP_EXECUTION_MODE` | `simulation` | `simulation` 使用模拟执行器；`live` 发送真实委托 |
| `LIMIT_UP_ENABLE_SHADOW_SIGNAL` | `false` | 是否并行运行影子策略 |
| `GJ_SIM_QMT_CLIENT_PATH` | 空 | 模拟端 `userdata_mini` 路径 |
| `GJ_SIM_STOCK_ACCOUNT` | 空 | 模拟资金账号 |
| `CICC_QMT_CLIENT_PATH` | 空 | 实盘端 `userdata_mini` 路径 |
| `CICC_STOCK_ACCOUNT` | 空 | 实盘资金账号 |
| `XTQUANT_HOST` / `XTQUANT_PORT` | `127.0.0.1` / `58610` | XTQuant 数据服务 |
| `LIMIT_UP_LOG_DIR` | `logs/monitor` | 本地日志根目录 |

路径或账号为空时，`main.py` 会在连接 QMT 前立即退出并提示缺少的变量。
`live` 模式还会在启动时要求人工输入 `yes`。

---

## 三、时间控制

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `STOP_TIME` | time | `15:01` | 系统停止时间（调试模式: 23:59） |
| `CLEAR_TIME` | time | `14:50` | 尾盘清仓时间（调试模式: 23:59） |
| `BUY_ORDER_CANCEL_DEADLINE` | time | `14:55` | 未成交买单自动撤单截止 |
| `SELL_ORDER_CANCEL_DEADLINE` | time | `14:50` | 未成交卖单自动撤单截止 |
| `FIRST_LIMIT_TIME_CUTOFF` | str | `'14:30'` | 不买入此时间后首次涨停的股票 |
| `TODAY` | str | 自动 | 当日日期 YYYYMMDD 格式 |

---

## 四、仓位管理

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `MAX_HOLDING_COUNT` | int | `6` | 最大同时持仓数量 |
| `MAX_SAME_SECTOR_COUNT` | int | `2` | 同一板块最大持仓数（分散风险） |
| `VOLATILITY_TARGET` | float | `0.05` | 波动率目标 (U6)：振幅大→减仓，振幅小→加仓 |
| `WATCHLIST_POSITION_RATIO` | float | `0.5` | 观察名单股票的仓位折扣 (50%) |

### 仓位计算公式

```
单股最大金额 = 总资产 ÷ MAX_HOLDING_COUNT

波动率因子 = VOLATILITY_TARGET ÷ 近20日平均振幅
波动率因子 = clamp(波动率因子, 0.5, 1.5)

买入量 = int(单股最大金额 ÷ 涨停价 × 波动率因子 ÷ 100) × 100

如果在观察名单中:
    买入量 = int(买入量 × WATCHLIST_POSITION_RATIO ÷ 100) × 100
```

---

## 五、涨停策略阈值

### 5.1 封单金额阈值

市场情绪评分驱动的**动态封单阈值**（通过线性插值）：

| 参数 | 情绪锚点 | 封单阈值 (万元) | 含义 |
|------|---------|----------------|------|
| `SEAL_THRESHOLD_EXTREME_STRONG` | 10.0 | 2,000 | 极强市场，低门槛 |
| `SEAL_THRESHOLD_STRONG` | 8.0 | 3,000 | 强势 |
| `SEAL_THRESHOLD_MODERATE_STRONG` | 7.0 | 5,000 | 偏强 |
| `SEAL_THRESHOLD_NEUTRAL` | 5.5 | 8,000 | 中性 |
| `SEAL_THRESHOLD_MODERATE_WEAK` | 4.0 | 10,000 | 偏弱 |
| `SEAL_THRESHOLD_WEAK` | 2.5 | 15,000 | 弱势 |
| `SEAL_THRESHOLD_EXTREME_WEAK` | 1.0 | 20,000 | 极弱 |

LLM 优先板块折扣：优先板块内的股票，封单阈值额外 × 0.7。

### 5.2 换手率控制

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `MAX_TURNOVER_RATE_BLACKLIST` | float | `0.25` | ≥ 此值加入黑名单 |
| `MAX_TURNOVER_RATE_THRESHOLD` | float | `0.15` | ≥ 此值加入观察名单 |

### 5.3 其他阈值

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `MAX_UP_LIMIT_BREAK_COUNT` | int | `5` | 最大炸板次数（超过加黑名单） |
| `MAX_CANCEL_COUNT` | int | 配置值 | 单只股票最大撤单次数 |
| `MIN_LIMIT_ORDER_AMOUNT` | float | 2000万 | 最低封单金额要求 |
| `MIN_VOLUME_RATIO_THRESHOLD` | float | `0.7` | 时段归一化量比下限；盘中累计量按已交易分钟折算 |
| `MAX_SWEEP_REQUIRED_CAPITAL` | float | 300万 | 扫板时可见卖盘的最大待吃金额 |
| `MAX_SECTOR_DATA_AGE_SECONDS` | int | `60` | 板块效应最大数据年龄，超时禁止买入 |
| `MAX_CAPITAL_FLOW_DATA_AGE_SECONDS` | int | `180` | 个股资金流最大数据年龄，超时禁止买入 |

扫板资金只统计涨停价以内的委卖档位 `askPrice × askVol × 100`，买盘深度
不计入拉板成本。实时板块或资金流时间戳缺失时采用 fail-closed，不生成买入信号。

---

## 六、止损止盈

### 6.1 固定止损

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `STOP_LOSS_RATE` | float | `0.05` | 固定止损率 5%（用于盘前和兜底） |

### 6.2 动态追踪止损 (v3.0)

追踪止损率根据盈利水平自动调整：

| 盈利水平 | 追踪率 | 含义 |
|---------|--------|------|
| ≥ 10% | 2.5% | 利润 ≥10% 时，最高价回撤 2.5% 即触发 |
| 5%-10% | 3.0% | |
| 2%-5% | 4.0% | |
| < 2% | 5.0% | 容忍正常波动 |

### 6.3 日内分档止盈

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `INTRADAY_TAKE_PROFIT_ENABLED` | bool | `True` | 是否启用日内止盈 |

止盈档位：

| 盈利达到 | 卖出比例 | 每档独立触发 |
|---------|---------|------------|
| 5% | 25% | 首次触发 |
| 8% | 25% | 首次触发 |
| 10% | 25% | 首次触发 |

---

## 七、盘前卖出策略

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `PRE_MARKET_SELL_STRATEGY` | enum | `FIXED_PREMIUM_SELL` | 盘前卖出策略类型 |
| `FIXED_PREMIUM_RATE` | float | `0.02` | 固定溢价率 2% |

### 策略说明

| 策略 | 挂单价格 | 保留仓位 |
|------|---------|---------|
| `TIERED_SELL` | 1/4 @ 昨收+5%，1/4 @ 涨停价 | 50% |
| `FIXED_PREMIUM_SELL` | 全部 @ 昨收+2%（或涨停价取高） | 0% |
| `LIMIT_UP_SELL` | 全部 @ 涨停价 | 0% |
| `NO_PRE_MARKET_SELL` | 不挂 | 100% |

---

## 八、情绪评分体系

### 8.1 评分因子

| 因子 | 分值范围 | 数据来源 |
|------|---------|---------|
| 涨停板数量 | ±1.5 | 涨停池统计 |
| 炸板率 | ±1.5 | 涨停/炸板比率 |
| 昨日延续率 | ±1.2 | 同花顺数据 |
| 昨日表现 | ±1.0 | 首板/涨停平均涨幅 |
| 大盘指数 | ±1.0 | 四大指数涨跌幅 |
| 指数分歧 | ±0.3 | 指数涨跌幅标准差 |

### 8.2 评分→操作映射

| 评分 | 等级 | 操作策略 |
|------|------|---------|
| ≥ 8.0 | 极强 | 积极扫板和排板，封单门槛最低 |
| ≥ 7.0 | 强势 | 正常排板，适度扫板 |
| ≥ 5.5 | 中性偏强 | 仅排板，精选个股 |
| ≥ 4.0 | 中性 | 仅排板，提高封单门槛 |
| ≥ 2.5 | 弱势 | 仅排板，极高封单门槛 |
| < 2.5 | 极弱 | **停止买入** |

---

## 九、邮件通知

| 参数 | 说明 |
|------|------|
| `SMTP_HOST` | SMTP 服务器（默认 `smtp.qq.com`） |
| `SMTP_USERNAME` | SMTP 登录账号 |
| `SMTP_PASSWORD` | SMTP 密码或授权码 |
| `SMTP_SENDER` | 发件地址（默认与登录账号相同） |
| `SMTP_RECIPIENT` | 收件地址 |

为兼容旧部署，未设置 `SMTP_PASSWORD` 时仍会读取 `QQ_MAIL_TOKEN`。

**触发邮件的场景**：
- 市场情绪评分变化 ≥ 1.0
- XTData 回调超时
- 交易连接断开
- 委托/撤单错误
- 定时状态报表（每 5 分钟）
- 收盘涨停列表汇总

---

## 十、输出目录

| 路径 | 内容 |
|------|------|
| `output/强势股票/` | 涨停基因 Top 1000 CSV |
| `output/涨停列表/` | 每日涨停/首板/炸板列表 |
| `output/trade_logs/{date}/` | 交易日志 JSON |
| `output/concept_sectors/THS/` | 概念板块映射 JSON |
| `output/industry_sectors/THS/` | 行业板块映射 JSON |
| `log/` | 分级日志文件 |

---

## 十一、调优建议

### 11.1 保守策略（减少亏损）

```python
MAX_HOLDING_COUNT = 4          # 减少同时持仓
STOP_LOSS_RATE = 0.03          # 更紧的止损
MAX_TURNOVER_RATE_THRESHOLD = 0.12  # 降低换手率容忍
PRE_MARKET_SELL_STRATEGY = FIXED_PREMIUM_SELL  # 快速出场
FIXED_PREMIUM_RATE = 0.01      # 降低溢价期望
```

### 11.2 激进策略（追求收益）

```python
MAX_HOLDING_COUNT = 8          # 更多持仓
STOP_LOSS_RATE = 0.08          # 更宽的止损
VOLATILITY_TARGET = 0.08       # 允许更大波动
INTRADAY_TAKE_PROFIT_ENABLED = False  # 关闭日内止盈，留更长
PRE_MARKET_SELL_STRATEGY = LIMIT_UP_SELL  # 涨停价卖出
```

### 11.3 弱市策略

```python
MAX_HOLDING_COUNT = 3          # 最少持仓
MAX_SAME_SECTOR_COUNT = 1      # 极致分散
FIRST_LIMIT_TIME_CUTOFF = '13:00'  # 只在上午打板
# 封单阈值会通过情绪评分自动提高
```
