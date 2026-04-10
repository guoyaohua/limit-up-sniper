# 分析模块

本文档说明 `analysis/` 目录下的盘前和盘后分析模块。

## 一、模块总览

| 文件 | 职责 | 运行时机 |
|------|------|---------|
| `pre_market_analysis.py` | LLM 驱动的盘前板块预测 (U7) | 盘前 9:15-9:25 |
| `post_market_review.py` | 盘后策略复盘与绩效分析 | 收盘后 15:00+ |
| `review_daily.py` | 每日交易绩效报告 | 收盘后 |

---

## 二、盘前 LLM 分析 (`pre_market_analysis.py`)

### 2.1 功能概述

利用大语言模型分析同花顺热榜数据，预测当日可能活跃的板块方向，生成板块优先级权重。预测结果用于买入决策时的**封单阈值折扣**（优先板块降低 30% 门槛）。

### 2.2 `run_pre_market_analysis() -> dict`

**完整流程**：

```
Step 1: 加载 Prompt 模板
        └── prompts/pre_market_sector_v1.md

Step 2: 抓取同花顺数据
        ├── 24 小时热股排行
        ├── 概念板块热度排行
        └── 行业板块热度排行

Step 3: 获取昨日涨停数据
        └── output/强势股票/{date}.csv

Step 4: 拼接 Prompt
        ├── {current_time} → 当前时间
        ├── {hot_stocks_24h} → 热股排行
        ├── {hot_concept_sectors} → 概念板块
        ├── {hot_industry_sectors} → 行业板块
        └── {yesterday_limit_up_stocks} → 昨日涨停

Step 5: 调用 LLM
        ├── 首选: DashScope (qwen3.5-plus / DeepSeek-V3.2)
        ├── 备选: Azure OpenAI
        └── 超时: 60 秒

Step 6: 解析返回 JSON
        └── 从 Markdown 代码块中提取 JSON
```

**返回数据格式**：

```json
{
    "market_outlook": "乐观",
    "priority_sectors": [
        {"sector": "半导体", "weight": 0.9, "reason": "国产替代加速..."},
        {"sector": "AI应用", "weight": 0.7, "reason": "ChatGPT热度..."},
        {"sector": "机器人", "weight": 0.6, "reason": "政策催化..."}
    ],
    "avoid_sectors": [
        {"sector": "房地产", "reason": "政策面持续打压..."}
    ],
    "key_stocks": ["300059", "002475", "603019"]
}
```

### 2.3 LLM Prompt 设计

**模板文件**: `prompts/pre_market_sector_v1.md`

**分析框架**（要求 LLM 从四个维度思考）：
1. **主线延续性**：昨日涨停领涨板块是否还在今日热榜？龙头是否仍活跃？
2. **新题材发酵**：热股榜中是否出现新概念？板块热度排名是否有新面孔上升？
3. **板块轮动风险**：高位板块是否有见顶回落迹象？市场处于什么阶段？
4. **资金集中度**：是否有多只同板块股票同时出现在热榜？

**约束**：
- 优先板块最多 5 个，每个须给出 0.0-1.0 权重和理由
- 回避板块最多 3 个
- 关键股票最多 5 只（6 位代码不带后缀）

### 2.4 在决策中的应用

```python
# decisions.py → should_buy() 中的应用
if stock_sector in shared_data['板块优先级']:
    sector_weight = shared_data['板块优先级'][stock_sector]
    seal_threshold *= 0.7   # LLM 优先板块，封单门槛降低 30%
```

---

## 三、盘后复盘 (`post_market_review.py`)

### 3.1 功能概述

每日收盘后自动运行，对当日策略表现进行全面复盘分析，包括：
- 过滤条件效果评估（每个过滤条件的精确率/召回率/F1）
- 错过的机会分析（被拒绝但实际涨停的股票）
- 整体策略胜率统计

### 3.2 核心类

#### `ReviewConfig`

```python
@dataclass
class ReviewConfig:
    log_dir: str           # 日志目录
    output_dir: str        # 输出目录
    gene_data_dir: str     # 涨停基因数据目录
    shared_data_dir: str   # 共享数据备份目录
    mode: str              # 'live' 或 'shadow'
    email_enabled: bool    # 是否发送邮件
```

#### `StockOutcome`

```python
@dataclass
class StockOutcome:
    stock_code: str
    outcome_type: str      # 'limit_up' / 'broken_board' / 'normal'
    limit_up_time: str     # 首次涨停时间
    break_count: int       # 炸板次数
```

#### `StrategyDecision`

```python
@dataclass
class StrategyDecision:
    stock_code: str
    decision_type: str     # 'approved' / 'rejected'
    reasons: list          # 买入/拒绝原因标签
    timestamp: str
```

#### `FilterMetrics`

每个过滤条件的效果评估：

```python
@dataclass
class FilterMetrics:
    filter_name: str       # 过滤条件名称
    true_positives: int    # 正确买入（买入且涨停）
    false_positives: int   # 错误买入（买入但未涨停）
    true_negatives: int    # 正确拒绝（拒绝且未涨停）
    false_negatives: int   # 错误拒绝（拒绝但涨停了）

    @property
    def precision(self):   # TP / (TP + FP) — 买入准确率
    @property
    def recall(self):      # TP / (TP + FN) — 涨停覆盖率
    @property
    def f1_score(self):    # 2 × P × R / (P + R)
```

### 3.3 主要分析器

#### `EnhancedDataCollector` — 数据收集

1. **加载涨停基因数据**：从 `output/强势股票/` CSV
2. **加载共享数据备份**：从 pickle 文件恢复当日交易状态
3. **解析日志**：从 DEBUG 日志中提取过滤记录和决策记录
4. **分类市场结果**：涨停/炸板/普通
5. **分类策略决策**：批准/拒绝 及原因标签

#### `FilterPerformanceAnalyzer` — 过滤效果分析

评估每个过滤条件的效果：

| 分析维度 | 说明 |
|---------|------|
| 换手率过滤 | 过滤掉的股票中有多少实际涨停？ |
| 量比过滤 | 量比过低被拒的股票后来怎样？ |
| 板块效应过滤 | 无板块效应被拒的股票表现如何？ |
| 封单金额过滤 | 封单不够被拒的股票最终封住了吗？ |
| 资金流向过滤 | 无资金流入被拒的结果如何？ |

**核心问题**：每个过滤条件是否在保护（排除风险）的同时，错过了有价值的机会？

#### `MissedOpportunityAnalyzer` — 错过机会分析

分析被策略拒绝但最终成功涨停的股票：

```
对每只被拒绝的涨停股:
    1. 记录被拒原因（哪些过滤条件触发）
    2. 计算影响评分 = f(封板持续时间, 次日溢价预期, 涨停基因得分)
    3. 按影响评分排序
    4. 输出 Top N 最遗憾的错过机会
```

### 3.4 报告输出

#### HTML 报告
- 策略总览（买入数、命中率、收益）
- 过滤条件效果表（精确率/召回率/F1 热力图）
- 错过机会 Top 列表
- 持仓分析

#### JSON 报告
- 原始数据供程序化分析
- 可用于时间序列追踪策略表现

---

## 四、每日绩效报告 (`review_daily.py`)

### 4.1 功能概述

匹配当日的买入和卖出交易，计算交易绩效指标。

### 4.2 使用方式

```bash
python analysis/review_daily.py --date 20250410
```

不指定日期则默认使用当天。

### 4.3 `load_trade_logs(date_str) -> list`

加载指定日期的所有交易日志：

```
output/trade_logs/{date_str}/trade_*.json
```

### 4.4 `match_buy_sell_pairs(logs) -> list`

**匹配逻辑**：
1. 按时间排序所有交易记录
2. 对每个买入记录，查找同一股票的后续卖出记录
3. 计算盈亏 = (卖出价 - 买入价) / 买入价 × 100%
4. 标记未匹配到卖出的买入为"未了结"

### 4.5 `generate_report(date_str, logs, pairs) -> str`

**Markdown 报告内容**：

```markdown
# 交易绩效日报 - 2025-04-10

## 核心指标
| 指标 | 数值 |
|------|------|
| 买入笔数 | 5 |
| 卖出笔数 | 4 |
| 胜率 | 60% |
| 平均盈利 | +3.2% |
| 平均亏损 | -1.8% |
| 盈亏比 | 1.78 |
| 总收益 | +5,234 元 |
| 最大单笔盈利 | +6.5% |
| 最大单笔亏损 | -2.3% |

## 交易明细
| 股票 | 买入价 | 卖出价 | 数量 | 盈亏% | 买入时间 | 卖出时间 |
|------|--------|--------|------|-------|---------|---------|
| ... | ... | ... | ... | ... | ... | ... |

## 买入原因汇总
- 排板买入: 3 笔
- 扫板买入: 2 笔

## 卖出触发汇总
- 追踪止损: 2 笔
- 日内止盈: 1 笔
- 尾盘清仓: 1 笔
```
