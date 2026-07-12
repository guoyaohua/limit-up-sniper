# Tick 回测与影子交易

该模块使用同一个 `PaperBroker` 承担实时影子账户和离线回测撮合，避免研究阶段与
线上模拟采用两套费用、T+1 或成交规则。它用于验证策略，不保证未来收益。

## 三种执行模式

| 模式 | 开关 | 行情 | 委托去向 |
|------|------|------|----------|
| 模拟 | `LIMIT_UP_EXECUTION_MODE=simulation` | 实时 XTQuant | 持久化纸面账户 |
| 实盘 + 影子 | `live` 且 `LIMIT_UP_ENABLE_SHADOW_SIGNAL=true` | 同一份实时 Tick，独立策略状态 | 实盘发 QMT；影子只写纸面账户 |
| 离线回测 | `scripts/run_backtest.py` | Tick 归档事件回放 | 内存纸面账户 |

纸面账户实时接收最新行情，更新持仓市值、浮盈和净值，并约每 5 秒写入 SQLite。
默认文件为 `output/paper_trading/simulation.sqlite3` 和 `shadow.sqlite3`。

## 保存 Tick

在 QMT Python 环境运行：

```powershell
python scripts/capture_ticks.py --output-dir output/tick_archive --verify
```

每次启动生成一个不可变的 `ticks-*.jsonl.gz` 分段及配套 `*manifest.json`。
记录保留原始五档数组、事件时间、接收时间、回调批次、采集 session 和股票代码。
进程重启产生新 session，不会把重新从 1 计数的批次错误合并。

`--verify` 会流式检查：

- JSON schema、必填字段和数值类型；
- 单股事件时间是否倒序、记录键是否重复；
- manifest 记录数/批次数与文件是否一致；
- SHA-256 是否一致以及回调队列是否丢包。

有丢包、缺失 manifest 或校验失败的分段默认不能进入严谨回测。全市场 Tick 体积
较大，应将 `output/tick_archive` 放在空间充足的 SSD，并按数据保留策略归档。

## 回测策略插件

插件提供 `create_strategy(settings)`，返回带 `on_tick_batch(batch, broker)` 的对象；
每次返回零到多个 `BacktestSignal`：

```python
from engine.backtest import BacktestSignal


class Strategy:
    def on_tick_batch(self, batch, broker):
        tick = batch.ticks.get("000001.SZ")
        if tick and should_enter(tick):
            return [BacktestSignal(
                stock_code="000001.SZ",
                side="BUY",
                target_value=100_000,
                limit_price=11.00,
                signal_id=f"entry-{batch.session_id}-{batch.batch_id}",
                reason="example",
                limit_up_entry=True,
            )]
        return []


def create_strategy(settings):
    return Strategy()
```

运行示例：

```powershell
python scripts/run_backtest.py `
  --ticks output/tick_archive/20260710 output/tick_archive/20260711 `
  --strategy path/to/strategy.py `
  --settings '{"threshold": 0.8}' `
  --output output/backtests/experiment-a.json
```

默认在信号生成后的下一笔该股票 Tick 执行，防止用产生信号的盘口同时成交。
`--same-tick-execution` 只适合明确研究撮合敏感性；结论报告应注明使用了它。

## 撮合假设

- 买入按卖盘、卖出按买盘逐档计算可见盘口 VWAP，再加入滑点；
- 数量向下取整到 100 股，且受可见盘口 × `participation_rate` 限制；
- 限价订单不会以更差价格成交；
- 默认 T+1，买入当日不可卖；`allow_t0` 只用于敏感性实验；
- 双边佣金（最低 5 元）、卖出印花税、双边过户费均计入；
- SQLite 账本用稳定 `signal_id` 防止进程重启后重复成交。

纸面撮合仍不能精确复原交易所排队位置。尤其涨停排板成交依赖队列前方撤单、成交和
券商通道时延；回测结果应使用更保守的参与率和滑点，并与影子账户长期对照。

## 指标口径

结果 JSON 包括成交、未平仓、FIFO 已平交易、净值曲线和：总收益、最大回撤、
胜率、盈亏比、费用、逐观测 Sharpe、成交数，以及打板专用指标。

`limit_up_entry=True` 表示该信号属于打板入场。封板定义为买一价等于最新价、卖一
为空；若 Tick 提供 `limitUpPrice`/`upperLimitPrice`，还要求买一等于涨停价。

- `sealed_at_least_once_count`：入场后至少出现一次封板；
- `broken_after_seal_count`：封板后又开板；
- `sealed_through_close_count`：该股票最后一笔有效 Tick 仍封板；
- `seal_success_rate`：收盘仍封板数 ÷ 打板入场股票数。

封板率与交易胜率是不同指标，不应互相替代。跨日比较至少同时报告收益、最大回撤、
封板率、样本数、费用假设、数据完整性和样本外区间。
