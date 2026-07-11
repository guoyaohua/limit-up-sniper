# deps/ - 外部依赖

本目录包含从 `ai_hotspot_trader` 项目复制的依赖模块，用于 LLM 盘前分析和数据抓取。

## ai_hotspot_trader/

来源：旧项目的 `ai_hotspot_trader` 公共模块；已作为 vendored 依赖迁入本目录。

- `llm_client/` - LLM 客户端（Azure OpenAI / DashScope / Copilot Vision）
- `ths_scraper/` - 同花顺热股榜抓取
- `eastmoney_scraper/` - 东方财富数据抓取
- `logger_config.py` - 日志配置

复制日期：2026-04-10
