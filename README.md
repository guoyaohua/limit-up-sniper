# limit-up-sniper

A股首板涨停板交易策略系统 (v2.4)

## 项目结构

```
limit-up-sniper/
├── main.py              # 策略入口
├── config.py            # 全局配置/常量
├── core/                # 核心策略逻辑 (买卖决策、止损、基因计算)
├── engine/              # 执行引擎 (行情处理、下单、模拟)
├── data/                # 数据管理 (序列化、板块映射、共享数据)
├── monitor/             # 监控任务 (板块监控、情绪指标、报表)
├── infra/               # 基础设施 (枚举、工具、任务管理)
├── analysis/            # 盘前/盘后分析
├── scraper/             # 数据抓取 (东方财富、同花顺)
├── level2/              # Level2 行情处理
├── standalone/          # 独立运行的工具模块
├── deps/                # 外部依赖 (llm_client, ths_scraper)
├── prompts/             # LLM Prompt 模板
├── test/                # 单元测试
└── scripts/             # 启动脚本
```

## 快速启动

```bash
python main.py
```

或使用批处理脚本:

```bash
scripts\run_strategy.bat
```

## 依赖

- Python 3.8+
- XTQuant (QMT交易接口)
- pandas / numpy / akshare / schedule / tqdm / loguru
