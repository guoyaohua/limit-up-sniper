# Limit-Up Sniper 文档中心

> A股首板涨停打板策略自动化交易系统 v1.0

## 文档导航

| 文档 | 说明 | 适合人群 |
|------|------|----------|
| [系统架构总览](architecture.md) | 整体架构设计、模块关系、数据流向、多进程模型 | 所有人（推荐首先阅读） |
| [每日交易流程](trading-flow.md) | 从开机到收盘的完整交易日流程详解 | 所有人 |
| [配置参数手册](configuration.md) | 所有可配置参数的详细说明与调优建议 | 运维 / 策略调优 |
| [核心策略模块](core-strategy.md) | 买入/卖出/撤单决策逻辑、涨停基因、止盈止损 | 策略研究 |
| [执行引擎](engine.md) | Tick 处理、实盘下单、模拟交易、回调机制 | 开发 / 运维 |
| [数据管理](data-management.md) | 共享数据结构、序列化/持久化、板块映射 | 开发 |
| [监控体系](monitoring.md) | 板块监控、市场情绪评分、指标计算、邮件报表 | 策略研究 / 运维 |
| [分析模块](analysis.md) | 盘前 LLM 板块预测、盘后复盘与绩效分析 | 策略研究 |
| [数据采集](scraper.md) | 东方财富/同花顺数据抓取、反爬策略 | 开发 |
| [Level2 行情处理](level2.md) | Level2 逐笔委托/成交数据处理架构 | 开发（高级） |
| [基础设施](infrastructure.md) | 进程管理、枚举定义、工具函数、日志与邮件 | 开发 |

## 快速了解

如果你是第一次接触本项目，建议按以下顺序阅读：

1. **[系统架构总览](architecture.md)** — 理解系统全貌和设计理念
2. **[每日交易流程](trading-flow.md)** — 理解一个交易日的完整运行过程
3. **[核心策略模块](core-strategy.md)** — 深入了解买卖决策的核心逻辑
4. **[配置参数手册](configuration.md)** — 了解如何调整策略参数

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.8+ |
| 交易接口 | XTQuant (QMT 量化交易) |
| 数据源 | XTQuant、东方财富 API、同花顺爬虫、akshare |
| LLM 集成 | DashScope (通义千问)、Azure OpenAI、Copilot Vision |
| 进程模型 | multiprocessing (8 Tick 处理进程 + 交易进程 + 监控进程) |
| 序列化 | pickle + msgpack (Level2) |
| 日志 | loguru |
| 邮件 | SMTP (QQ 邮箱) |
