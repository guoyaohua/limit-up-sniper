# 东方财富网涨停板行情爬虫

抓取东方财富网涨停板行情数据，包括涨停股池、炸板股池、跌停股池。

## 功能特点

- **双模式支持**：
  - **API 模式（推荐）**：直接调用东方财富 API，无需浏览器，更快更稳定
  - **Web 模式**：通过浏览器抓取页面数据
- 支持三种股池类型：涨停股池、炸板股池、跌停股池
- **支持选择特定日期**：可以获取历史交易日的涨停数据
- 数据保存为 TSV 格式，方便后续处理
- 文件名与页面显示日期保持一致

## 依赖安装

```bash
# API 模式需要 aiohttp
pip install aiohttp

# Web 模式需要 playwright
pip install playwright
playwright install chromium
```

## 使用方法

### 命令行运行

#### API 模式（推荐）

```bash
# 抓取所有股池数据（默认使用 API 模式）
python -m eastmoney_scraper.scraper

# 抓取指定日期的数据
python -m eastmoney_scraper.scraper --date 20240115

# 只抓取涨停股池
python -m eastmoney_scraper.scraper --pool ztgc --date 20240115

# 只抓取炸板股池
python -m eastmoney_scraper.scraper --pool zbgc --date 20240115

# 只抓取跌停股池
python -m eastmoney_scraper.scraper --pool dtgc --date 20240115

# 指定输出目录
python -m eastmoney_scraper.scraper --output my_data --date 20240115
```

#### Web 模式（浏览器抓取）

```bash
# 使用 Web 模式抓取
python -m eastmoney_scraper.scraper --mode web

# Web 模式 + 无头模式
python -m eastmoney_scraper.scraper --mode web --headless

# Web 模式 + 指定日期
python -m eastmoney_scraper.scraper --mode web --date 20240115 --headless
```

### 作为模块导入

```python
import asyncio
from eastmoney_scraper import EastMoneyAPIFetcher, EastMoneyZTBScraper

# 方式1：使用 API 模式（推荐）
async def fetch_via_api():
    fetcher = EastMoneyAPIFetcher(target_date="2024-01-15")
    
    # 获取所有股池数据
    all_data = await fetcher.fetch_all_pools()
    
    print(f"涨停股池: {len(all_data['ztgc'])} 条数据")
    print(f"炸板股池: {len(all_data['zbgc'])} 条数据")
    print(f"跌停股池: {len(all_data['dtgc'])} 条数据")

asyncio.run(fetch_via_api())

# 方式2：使用 Web 模式（浏览器抓取）
async def fetch_via_web():
    async with EastMoneyZTBScraper(headless=True, target_date="2024-01-15") as scraper:
        all_data = await scraper.scrape_all_pools()
        print(f"数据日期: {scraper.page_date}")
        print(f"保存目录: {scraper.data_dir}")

asyncio.run(fetch_via_web())
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--mode` | 抓取模式：api（API获取）、web（网页抓取） | api |
| `--headless` | 使用无头模式运行浏览器（仅 web 模式） | False |
| `--pool` | 要抓取的股池类型：ztgc(涨停)、zbgc(炸板)、dtgc(跌停)、all(全部) | all |
| `--output` | 数据保存目录 | output/eastmoney_ztb/{日期} |
| `--date` | 目标日期，支持 YYYY-MM-DD 或 YYYYMMDD 格式 | 当天 |

## API 模式 vs Web 模式

| 特性 | API 模式 | Web 模式 |
|------|---------|---------|
| 速度 | ⚡ 快 | 🐢 较慢 |
| 稳定性 | ✅ 高 | ⚠️ 中等 |
| 依赖 | aiohttp | playwright + chromium |
| 浏览器 | 不需要 | 需要 |
| 适用场景 | 批量获取、定时任务 | 调试、特殊情况 |

## 股池类型说明

| 类型代码 | 名称 | API 端点 |
|----------|------|----------|
| ztgc | 涨停股池 | getTopicZTPool |
| zbgc | 炸板股池 | getTopicZBPool |
| dtgc | 跌停股池 | getTopicDTPool |

## 输出文件格式

数据默认保存到 `output/eastmoney_ztb/{日期}/` 目录下，格式为 TSV（Tab分隔）。

文件路径示例：
- `output/eastmoney_ztb/20260127/20260127_limit_up_stocks.tsv` - 涨停股池数据
- `output/eastmoney_ztb/20260127/20260127_broken_limit_stocks.tsv` - 炸板股池数据
- `output/eastmoney_ztb/20260127/20260127_limit_down_stocks.tsv` - 跌停股池数据

### 涨停股池字段

| 字段 | 说明 |
|------|------|
| 代码 | 股票代码 |
| 名称 | 股票名称 |
| 涨跌幅 | 涨跌幅百分比 |
| 最新价 | 当前股价 |
| 成交额 | 成交金额 |
| 流通市值 | 流通市值 |
| 总市值 | 总市值 |
| 换手率 | 换手率百分比 |
| 封板资金 | 封板资金金额 |
| 首次封板时间 | 首次封板的时间 |
| 最后封板时间 | 最后封板的时间 |
| 炸板次数 | 打开涨停板的次数 |
| 涨停统计 | 涨停统计信息 |
| 连板数 | 连续涨停天数 |
| 所属行业 | 所属行业板块 |

### 炸板股池字段

| 字段 | 说明 |
|------|------|
| 代码 | 股票代码 |
| 名称 | 股票名称 |
| 涨跌幅 | 涨跌幅百分比 |
| 最新价 | 当前股价 |
| 涨停价 | 涨停价格 |
| 成交额 | 成交金额 |
| 流通市值 | 流通市值 |
| 总市值 | 总市值 |
| 换手率 | 换手率百分比 |
| 涨速 | 涨速 |
| 首次封板时间 | 首次封板的时间 |
| 炸板次数 | 打开涨停板的次数 |
| 涨停统计 | 涨停统计信息 |
| 振幅 | 股价振幅百分比 |
| 所属行业 | 所属行业板块 |

### 跌停股池字段

| 字段 | 说明 |
|------|------|
| 代码 | 股票代码 |
| 名称 | 股票名称 |
| 涨跌幅 | 涨跌幅百分比 |
| 最新价 | 当前股价 |
| 成交额 | 成交金额 |
| 流通市值 | 流通市值 |
| 总市值 | 总市值 |
| 动态市盈率 | 动态市盈率 |
| 换手率 | 换手率百分比 |
| 封单资金 | 封单资金金额 |
| 最后封板时间 | 最后封板的时间 |
| 板上成交额 | 板上成交金额 |
| 连续跌停 | 连续跌停天数 |
| 开板次数 | 打开跌停板的次数 |
| 所属行业 | 所属行业板块 |

## 注意事项

1. **API 模式**是推荐的使用方式，速度快且稳定
2. 请在交易时段（9:30-15:00）运行爬虫以获取实时数据
3. 非交易时段会显示上一个交易日的数据
4. 选择的日期如果是非交易日，会返回空数据或最近一个交易日的数据
5. 如遇网络问题，程序会自动重试

## 数据类说明

`StockInfo` 数据类包含以下字段：

```python
@dataclass
class StockInfo:
    code: str = ""              # 股票代码
    name: str = ""              # 股票名称
    price: str = ""             # 最新价
    change_pct: str = ""        # 涨跌幅
    turnover_rate: str = ""     # 换手率
    change_speed: str = ""      # 涨速
    amplitude: str = ""         # 振幅
    amount: str = ""            # 成交额
    circulating_value: str = "" # 流通市值
    total_value: str = ""       # 总市值
    pe_ratio: str = ""          # 动态市盈率
    seal_amount: str = ""       # 封板资金/封单资金
    limit_up_price: str = ""    # 涨停价（炸板股池）
    limit_up_time: str = ""     # 首次封板时间
    last_limit_time: str = ""   # 最后封板时间
    limit_up_stats: str = ""    # 涨停统计
    industry: str = ""          # 所属行业
    continuous_days: str = ""   # 连板数
    open_count: str = ""        # 开板次数/炸板次数
    continuous_limit_down: str = ""  # 连续跌停（跌停股池）
    board_amount: str = ""      # 板上成交额（跌停股池）
    pool_type: str = ""         # 股池类型: ztgc(涨停), zbgc(炸板), dtgc(跌停)