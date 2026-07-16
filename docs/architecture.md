# 系统架构总览

## 一、设计理念

Limit-Up Sniper 是一套面向 A 股市场的**首板涨停打板策略**自动化交易系统。系统的核心理念是：

1. **筛选"涨停基因"强的股票** — 通过历史数据计算每只股票的封板成功率、次日溢价等指标，选出最有可能封板成功且次日有溢价的 1000 只"强势股"
2. **实时监控全市场行情** — 订阅全市场 Tick 数据，毫秒级检测涨停/炸板/回封事件
3. **多因子动态决策** — 综合市场情绪、板块效应、资金流向、封单金额等多维度信号做出买入/撤单/卖出决策
4. **智能风控** — 动态追踪止损、分档止盈、换手率黑名单、板块集中度限制

## 二、整体架构图

系统只有一个 XTQuant 全推行情订阅。主通道、研究通道、Mirror 和 Tick 归档从同一批
实时 Tick 分流，避免重复连接行情服务。只有实盘主通道能向 QMT 发送委托。

```text
XTQuant 全推行情（单订阅）
        │
        ├─ 主 Tick 队列（阻塞、不能静默丢数据）
        │      └─ 8 × Tick 决策进程 ── 委托队列 ──┬─ 模拟：primary_simulation 纸面账本
        │                                           └─ 实盘：QMT 委托
        │
        ├─ 研究 Tick 队列（非阻塞；拥塞时标记结果不完整）
        │      ├─ Coverage：同策略、固定目标市值、解除组合容量影响
        │      └─ A/B：Baseline 与 Challenger 各自决策、账户和状态隔离
        │
        ├─ Mirror 行情队列 ── 仅撮合券商已接受的盘中委托，不重复运行策略
        └─ Tick 归档队列 ── 独立写盘线程 ── gzip 分段 + manifest + SHA-256

板块监控 / 情绪监控 / 盘前 LLM
        └─ 更新主共享状态；研究通道复制盘前公共信息并维护独立策略状态
```

`TaskManager` 统一负责工作进程、线程、停止信号、异常重启和收盘清理。各纸面通道
使用独立 SQLite 账本，不能共享资金、持仓、委托或信号身份。

## 三、模块职责划分

### 3.1 目录结构与职责

```
limit-up-sniper/
│
├── main.py                  # 主控入口：初始化、编排、生命周期
├── config.py                # 全局配置中心：所有阈值、路径、开关
│
├── core/                    # 🧠 核心策略逻辑
│   ├── stock_pool.py        #   股票池初始化（过滤ST、停牌、新股等）
│   ├── gene_calculator.py   #   涨停基因计算（历史统计 → 强势股评分）
│   ├── decisions.py         #   买入/撤单/卖出 实时决策（核心！）
│   ├── trailing_stop.py     #   动态追踪止损（10档阶梯式）
│   ├── pre_market_sell.py   #   盘前卖出策略（固定溢价/分档/涨停价）
│   ├── interpolation.py     #   连续插值（情绪→封单阈值/板块要求）
│   └── runtime_lanes.py     #   Challenger 配置校验、实验编号与冻结快照
│
├── engine/                  # ⚙️ 执行引擎
│   ├── tick_processor.py    #   Tick 数据处理 & 信号状态机
│   ├── trader.py            #   实盘下单执行（XTQuant）
│   ├── simulator.py         #   各隔离通道的持久化纸面交易执行
│   ├── paper_broker.py      #   统一费用、滑点、T+1 与盘口撮合规则
│   ├── mirror.py            #   券商已接受盘中委托的实时纸面镜像
│   ├── live_archive.py      #   复用主行情订阅的 Tick 写盘线程
│   ├── xt_callback.py       #   XTQuant 回调处理
│   └── xt_queries.py        #   持仓/委托/资产查询封装
│
├── data/                    # 📦 数据管理
│   ├── shared_data.py       #   多进程共享数据初始化
│   ├── serialization.py     #   序列化/反序列化/备份恢复
│   ├── sector_mapping.py    #   板块-股票映射加载
│   └── helpers.py           #   数据转换工具
│
├── monitor/                 # 📊 实时监控
│   ├── sector_monitor.py    #   板块涨幅 & 资金流向监控
│   ├── sentiment.py         #   市场情绪指标计算
│   ├── sentiment_task.py    #   情绪监控调度任务
│   ├── indicators.py        #   综合情绪评分（1-10 分）
│   └── dashboard.py         #   HTML 邮件报表生成
│
├── analysis/                # 🔬 分析
│   ├── pre_market_analysis.py  # 盘前 LLM 板块预测
│   ├── post_market_review.py   # 盘后复盘（精度/召回/F1）
│   └── review_daily.py         # 每日交易绩效报告
│
├── scraper/                 # 🕷️ 数据采集
│   ├── em_scraper_api.py         # 东方财富板块数据 API
│   ├── em_stock_capital_flow_scraper.py  # 个股资金流向采集
│   ├── dfcf_ztlb_parser.py      # 涨停/炸板 MHTML 解析
│   ├── tonghuashun_monitor.py    # 同花顺实时监控
│   ├── ths_sector_parser.py      # 同花顺板块数据解析
│   └── anti_ban_helper.py        # 反爬辅助
│
├── level2/                  # 📈 Level2 深度行情
│   ├── ARCHITECTURE.md      #   架构设计文档
│   ├── main.py              #   L2 系统入口
│   ├── enums.py / models.py #   枚举与数据模型
│   ├── buffers/             #   共享内存环形缓冲区
│   ├── calculators/         #   封单额/资金流计算器
│   └── consumers/           #   多进程消费者
│
├── infra/                   # 🔧 基础设施
│   ├── common_enums.py      #   交易相关枚举定义
│   ├── task_manager.py      #   进程/线程管理器
│   ├── trade_log.py         #   交易日志持久化
│   ├── utils.py             #   邮件、日志初始化
│   └── data_helpers.py      #   XTData 连接、价格工具
│
├── deps/                    # 📚 外部依赖库
│   └── ai_hotspot_trader/
│       ├── llm_client/      #   LLM 客户端（DashScope/Azure/Copilot）
│       ├── ths_scraper/     #   同花顺爬虫核心
│       └── eastmoney_scraper/  # 东方财富爬虫核心
│
├── standalone/              # 🧰 独立工具
├── test/                    # 🧪 测试
├── prompts/                 # 📝 LLM Prompt 模板
└── scripts/                 # 🚀 启动脚本
    ├── run-simulation.cmd   #   主模拟 + Coverage + Tick 归档
    ├── run-live.cmd         #   实盘 + Mirror + Coverage + Tick 归档
    └── run-experiment.cmd   #   Baseline / Challenger A/B 实验
```

### 3.2 运行角色与数据口径

| 角色 | 决策规则 | 资金/容量口径 | 核心用途 |
|------|----------|---------------|----------|
| 主模拟 | 当前正式策略 | 有限纸面资金、正式持仓与板块容量 | 上线前连续观察可实现收益 |
| 实盘主通道 | 当前正式策略 | 券商真实资金和持仓 | 唯一允许发送真实委托的通道 |
| Live Mirror | 不运行决策，只接收券商同步接受的盘中委托 | 独立纸面账户 | 比较实盘委托与统一纸面撮合口径 |
| Coverage | 与当前正式策略相同的选股、买点和风控信号 | 每个信号固定目标市值；绕过总持仓槽位与板块容量 | 统计被资金和组合容量遮蔽的全部机会 |
| Baseline / Challenger | A 使用正式配置，B 只应用显式 Challenger 配置 | 相同初始资金、费用、滑点和撮合规则 | 公平评估单一策略变化 |

Coverage 的技术资金上限只用于确保信号不因现金耗尽而消失，不能把巨额初始资金当作
策略可实现收益分母。它重点回答“如果每个合格信号都能分配同样金额，封板率和单笔
收益如何”。主模拟则回答“在实际资金和组合约束下，账户最终能赚多少”。

Mirror 不是“再跑一遍主策略”。它当前只接收盘中订单队列里经券商同步接受的委托；
不会补造启用前已有持仓，也不覆盖盘前卖出模块直接发出的订单。因此它用于校验盘中
撮合差异，不能替代券商成交回报或完整实盘收益账本。

## 四、多进程架构

系统采用 `multiprocessing` 多进程模型，通过 `Manager()` 代理实现跨进程状态共享：

| 进程/线程 | 默认数量 | 类型 | 职责 |
|-----------|----------|------|------|
| 主 Tick 处理 | 8 | Process | 按股票代码固定分片消费 Tick，更新状态，生成正式信号 |
| 每个研究通道 Tick 处理 | 2 | Process | Coverage、Baseline 或 Challenger 的独立决策 |
| 行情订阅 | 1 | 主进程 | 单次订阅全市场行情并分流 |
| 主交易执行 | 1 | Thread | 模拟时写主纸面账本；实盘时调用 QMT |
| 每个研究通道交易执行 | 1 | Thread | 写入该通道的独立纸面账本 |
| Mirror | 0 或 1 | Thread | 撮合实盘已接受的盘中委托 |
| Tick 归档 | 0 或 1 | Thread | 从非阻塞队列写入可审计 Tick 归档 |
| 板块、情绪等监控 | 若干 | Process / Thread | 更新公共市场状态、查询持仓、备份数据 |

默认 `run-simulation.cmd` 和 `run-live.cmd` 各使用 8 个主 Tick 工作者加 2 个 Coverage
工作者；`run-experiment.cmd` 使用 8 个主工作者加 Baseline/Challenger 各 2 个，共 12 个。
Mirror 与 Tick 归档增加线程，不增加策略决策进程。启动器会打印预计 Tick 工作者数量。

### 进程间通信

```text
行情回调 ── blocking Queue ── 主 Tick 工作者 ── 主委托 Queue ── 主执行线程
    │
    ├── non-blocking Queue ── Coverage 工作者 ── Coverage 委托 ── 纸面线程
    ├── non-blocking Queue ── Baseline 工作者 ── Baseline 委托 ── 纸面线程
    ├── non-blocking Queue ── Challenger 工作者 ── Challenger 委托 ── 纸面线程
    ├── latest-only Queue ── Mirror / 各纸面账户的市值更新
    └── non-blocking Queue ── Tick 归档线程

每个策略通道 ── 独立 Manager 代理状态 + 独立订单队列 + 独立 SQLite 账本
```

股票代码通过稳定 CRC32 映射到固定队列。同一股票的连续 Tick 由同一进程
按 FIFO 顺序处理，避免并行完成顺序反转破坏前价、炸板/回封及卖盘缩量状态；
不同股票仍在同一通道的多个分片之间并行。研究通道投递不阻塞主行情回调；只要研究
队列发生拥塞，就把当日研究结果标记为不完整，禁止把残缺样本当作完整策略表现。

## 五、关键设计决策

### 5.1 为什么用多进程而不是多线程？

Python GIL 限制了多线程的 CPU 并行能力。Tick 数据处理是 CPU 密集型任务（每秒数千条数据的解析和决策计算），8 个独立进程可以真正并行处理。

### 5.2 为什么用 Manager() 而不是共享内存？

`Manager()` 提供了 `DictProxy`、`ListProxy` 等高级数据结构的跨进程共享能力，支持动态嵌套结构。虽然有序列化开销，但代码可读性和灵活性更好。Level2 模块使用了共享内存环形缓冲区以满足微秒级延迟要求。

### 5.3 为什么不再使用一个“影子模式”概括所有研究？

旧 Shadow 同时表达了“复制正式信号以解除资金限制”和“运行不同策略做实验”，导致
统计口径混淆。现在按问题拆分：

- Coverage 衡量正式策略的全部合格机会，规则与正式策略相同；
- Baseline/Challenger 才用于不同策略或候选池的 A/B 对照；
- Mirror 只复刻已接受委托的纸面撮合，不运行第二套决策；
- `LIMIT_UP_ENABLE_SHADOW_SIGNAL` 和 `-EnableShadow` 仅为旧部署兼容，映射为 Coverage，
  新配置不得继续使用。

### 5.4 为什么 A/B 必须冻结配置和撮合假设？

Baseline 与 Challenger 使用相同初始资金、费用、滑点、T+1、盘口参与率和撮合规则。
实验第一次运行会把 Challenger 配置及撮合假设写入
`output/paper_trading/experiments/<id>/frozen-config.json`；以后若内容变化，系统拒绝继续
写入旧实验，必须换新 `ExperimentId`。这样收益差异才能归因于显式实验变量。

实盘 A/B 通常没有额外决策价值：B 组不能下真实订单，而且会增加进程与 Tick 处理
压力。推荐实盘只开 Mirror、Coverage 与归档；策略变体使用独立模拟入口，或基于完整
Tick 归档盘后回放。

### 5.5 LLM 集成的作用是什么？

盘前 LLM 分析（U7 升级）在 9:15-9:25 运行，利用大语言模型分析同花顺热榜数据和昨日涨停信息，预测当日可能活跃的板块。预测结果应用为**封单阈值折扣**（优先板块的买入门槛降低 30%），而非直接决定买卖。

## 六、版本演进

| 版本标记 | 功能 |
|---------|------|
| v2.3 | 涨停基因计算向量化优化 |
| v2.4 | 任务管理器 + 心跳监控 + 影子信号模式 + 观察名单 |
| v3.0 | 动态追踪止损（替代静态成本止损）+ 日内分档止盈 + 波动率仓位管理 |
| U5 | 换手率分级处理（<15% 正常 / 15-25% 观察 / ≥25% 黑名单） |
| U6 | 波动率加权仓位管理（VOLATILITY_TARGET=5%） |
| U7 | LLM 盘前板块优先级预测 + 阈值折扣 |
| U8 | 交易日志结构化记录 |
| v1.1 | 主通道、Mirror、Coverage、A/B 实验和 Tick 归档职责分离 |

## 七、启动入口与账本隔离

| 入口 | 默认组合 | 账本 | 是否发真实委托 |
|------|----------|------|----------------|
| `scripts/run-simulation.cmd` | 主模拟 + Coverage + Tick 归档 | `primary_simulation.sqlite3`、`coverage.sqlite3` | 否 |
| `scripts/run-live.cmd` | 实盘主通道 + Mirror + Coverage + Tick 归档 | `live_mirror.sqlite3`、`coverage.sqlite3`；主账户在券商 | 仅主通道 |
| `scripts/run-experiment.cmd` | 主模拟 + Baseline + Challenger + Tick 归档 | `experiments/<id>/baseline.sqlite3`、`challenger.sqlite3`，另有主模拟账本 | 否 |

`run.ps1 -PreflightOnly` 只检查环境、账号路径、XTQuant、板块映射和上一交易日清单，
不会启动策略。`-RefreshSector` 会先刷新问财概念/行业映射。启动脚本始终以命令行模式
覆盖 `.env`，因此安全模拟入口不会被误配置成实盘。
