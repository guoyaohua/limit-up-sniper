# Tick 回测与纸面研究

该模块使用同一个 `PaperBroker` 承担主模拟、Mirror、Coverage、A/B 与离线回测撮合，
避免研究阶段与线上模拟采用两套费用、T+1 或成交规则。它用于验证策略，不保证未来收益。

## 执行角色

| 角色 | 入口/开关 | 行情 | 委托去向 |
|------|-----------|------|----------|
| 主模拟 + Coverage | `run-simulation.cmd` | 实时 XTQuant | 隔离持久化纸面账户 |
| 实盘 + Mirror + Coverage | `run-live.cmd` | 同一份实时 Tick，状态与账本隔离 | 只有主策略发 QMT |
| Baseline / Challenger | `run-experiment.cmd` | 同一份实时 Tick | 两个冻结口径的纸面账户 |
| 离线回测 | `scripts/run_backtest.py` | Tick 归档事件回放 | 内存纸面账户 |

纸面账户实时接收最新行情，更新持仓市值、浮盈和净值，并约每 5 秒写入 SQLite。
默认账本为 `primary_simulation.sqlite3`、`live_mirror.sqlite3`、
`coverage.sqlite3`，实验账本位于 `experiments/<id>/`。

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
回放多个分段时先按交易日排序，再用回调接收时间校验同一交易日内的分段范围；
同日重叠或倒序分段直接拒绝，避免同一行情被重复消费或用错时间顺序。

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

也可以直接回放系统实际写出的决策事件，无需另写插件：

```powershell
python scripts/run_backtest.py `
  --ticks output/tick_archive/20260710 `
  --events output/trade_logs/20260710/events.jsonl `
  --event-source primary `
  --event-buy-target-value 100000 `
  --output output/backtests/primary-20260710.json
```

`--event-source coverage|baseline|challenger` 可单独评估研究通道；`shadow` 仅兼容
历史数据。新版事件带有 `signal_source`，默认拒绝
无法区分主策略与影子策略的旧日志；若明确接受这一歧义，可加
`--accept-legacy-unlabelled-events`，结果中的 `event_log_diagnostics` 会保留旧日志计数。
由于旧事件没有实际委托数量，买入仓位由 `--event-buy-target-value` 统一指定；卖出按
事件中的目标剩余仓位执行。

事件回放使用日志 `timestamp` 对齐 Tick。严谨归档优先用 `received_at_ns`（行情回调到达
本机的时刻）与日志的本地墙钟对齐，避免交易所行情时间与本机时钟偏差造成隐性前视；
历史/人工归档只有在接收时间不可用时才回退到 Tick `time`，并在诊断中计数。决策只在
时间到达后生效。扫板在下一笔该股 Tick 撮合；排板使用事件快照重建前方队列，只有累计成交穿透“前方队列 + 本单”且全程
封板才成交，开板、撤单或跨交易日都会使原队列永久失效。

默认在信号生成后的下一笔该股票 Tick 执行，防止用产生信号的盘口同时成交。
`--same-tick-execution` 只适合明确研究撮合敏感性；结论报告应注明使用了它。

## 撮合假设

- 买入按卖盘、卖出按买盘逐档计算可见盘口 VWAP，再加入滑点；
- 数量向下取整到 100 股，且受可见盘口 × `participation_rate` 限制；
- 限价订单不会以更差价格成交；
- 默认 T+1，买入当日不可卖；`allow_t0` 只用于敏感性实验；
- 双边佣金（最低 5 元）、卖出印花税、双边过户费均计入；
- SQLite 账本用稳定 `signal_id` 防止进程重启后重复成交。
- `--close-at-end` 仍服从最后盘口的买盘与参与率；无买盘/跌停锁死时保留未平仓，
  不会用最新价虚构强制卖出。

纸面撮合仍不能精确复原交易所排队位置。实时模拟和研究纸面通道的排板只接受一个保守的完整成交
证据：下单后的累计成交手数必须覆盖“下单时前方买一队列 + 本单手数”，且确认时仍处于
封板盘口。封单减少可能来自撤单，不计作成交；开板也不自动视为成交。该规则可能低估
真实成交，但能避免把不可成交的涨停收益记入策略。回测仍应使用保守参与率和滑点，并
与对应纸面账户长期对照。

## 指标口径

结果 JSON 包括成交、未平仓、FIFO 已平交易、净值曲线和：总收益、最大回撤、
胜率、盈亏比、费用、逐观测 Sharpe、成交数，以及打板专用指标。
最大回撤按每一个回放批次的净值计算，即使输出净值曲线按 `--equity-every` 抽样也不会
漏掉批次间回撤。`strategy_signal_count`、执行尝试、撮合拒绝、过期/未执行信号以及
收尾平仓失败数共同形成信号到成交的漏斗；优化策略时必须同时检查，不能只比较成交后胜率。
胜率和收盘封板率同时给出 Wilson 95% 置信区间；样本数为 0 时点估计与区间均为
`null`，避免把“没有交易”误读成 0% 胜率。

`limit_up_entry=True` 表示该信号属于打板入场。封板定义为最新价和买一价都等于
明确的涨停价，且卖一为空。严谨封板统计必须由 Tick 的 `limitUpPrice` /
`upperLimitPrice` 或信号限价提供涨停价；缺少该证据时不把空卖盘误记为封板。

- `sealed_at_least_once_count`：入场后至少出现一次封板；
- `broken_after_seal_count`：封板后又开板；
- `sealed_through_close_count`：该股票最后一笔有效 Tick 仍封板；
- `seal_success_rate`：收盘仍封板数 ÷ 打板入场股票数。

封板率与交易胜率是不同指标，不应互相替代。跨日比较至少同时报告收益、最大回撤、
封板率、样本数、费用假设、数据完整性和样本外区间。
