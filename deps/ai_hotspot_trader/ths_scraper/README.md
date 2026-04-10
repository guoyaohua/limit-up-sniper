# 同花顺热点股票/板块抓取模块

这是一个用于抓取同花顺热点股票和板块数据的Python模块。

## 功能

- **1小时热股**: 获取过去1小时内最热门的股票
- **24小时热股**: 获取过去24小时内最热门的股票  
- **热门行业板块**: 获取当前最热门的行业板块
- **热门概念板块**: 获取当前最热门的概念板块
- **盘面分析数据** (新增): 获取市场综合评分、涨跌分布、成交额等盘面分析数据

## 安装依赖

```bash
# 基础依赖
pip install requests

# 盘面分析功能需要 Playwright
pip install playwright
playwright install chromium
```

## 使用方法

### 基本使用

```python
from ths_scraper import THSHotSpotScraper

# 使用上下文管理器（推荐）
with THSHotSpotScraper() as scraper:
    # 获取所有热点数据
    hot_data = scraper.get_all_hot_data()
    
    # 打印摘要
    print(hot_data.summary())
    
    # 访问具体数据
    for stock in hot_data.hot_stocks_1h[:10]:
        print(f"{stock.rank}. {stock.code} {stock.name} 热度:{stock.hot_value}")
```

### 获取包含盘面分析的完整数据

```python
from ths_scraper import THSHotSpotScraper

with THSHotSpotScraper() as scraper:
    # 获取所有热点数据（包含盘面分析）
    hot_data = scraper.get_all_hot_data(include_market_analysis=True)
    
    # 访问盘面分析数据
    if hot_data.market_analysis:
        analysis = hot_data.market_analysis
        print(f"市场评分: {analysis.market_score}分")
        print(f"当日点评: {analysis.market_comment}")
        print(f"涨跌家数: 上涨 {analysis.rise_count} / 下跌 {analysis.fall_count}")
        print(f"涨跌停: 涨停 {analysis.limit_up_count} / 跌停 {analysis.limit_down_count}")
        print(f"成交额: 今日 {analysis.today_volume}亿 / 昨日 {analysis.yesterday_volume}亿")
```

### 单独获取盘面分析数据

```python
from ths_scraper import get_market_analysis

# 方式1: 使用便捷函数
analysis = get_market_analysis()
print(analysis.summary())

# 方式2: 使用异步方式
import asyncio
from ths_scraper import MarketAnalysisScraper

async def main():
    async with MarketAnalysisScraper(headless=True) as scraper:
        analysis = await scraper.get_market_analysis()
        print(analysis.summary())

asyncio.run(main())
```

### 单独获取各类数据

```python
from ths_scraper import THSHotSpotScraper

scraper = THSHotSpotScraper()

try:
    # 获取1小时热股
    stocks_1h = scraper.get_hot_stocks_1h(limit=50)
    
    # 获取24小时热股
    stocks_24h = scraper.get_hot_stocks_24h(limit=50)
    
    # 获取热门行业板块
    industry_sectors = scraper.get_hot_industry_sectors(limit=20)
    
    # 获取热门概念板块
    concept_sectors = scraper.get_hot_concept_sectors(limit=20)
    
    # 单独获取盘面分析
    market_analysis = scraper.get_market_analysis()
finally:
    scraper.close()
```

### 导出为JSON

```python
import json
from ths_scraper import THSHotSpotScraper

with THSHotSpotScraper() as scraper:
    hot_data = scraper.get_all_hot_data(include_market_analysis=True)
    
    # 转换为字典
    data_dict = hot_data.to_dict()
    
    # 保存为JSON文件
    with open('hot_data.json', 'w', encoding='utf-8') as f:
        json.dump(data_dict, f, ensure_ascii=False, indent=2)
```

## 数据模型

### HotStock - 热门股票

| 字段 | 类型 | 说明 |
|------|------|------|
| rank | int | 排名 |
| code | str | 股票代码 |
| name | str | 股票名称 |
| hot_value | int | 热度值 |
| change_percent | float | 涨跌幅(%) |
| hot_change | str | 热度排名变化 |
| reason | str | 上榜原因/概念标签 |
| fetch_time | datetime | 抓取时间 |

### HotSector - 热门板块

| 字段 | 类型 | 说明 |
|------|------|------|
| rank | int | 排名 |
| code | str | 板块代码 |
| name | str | 板块名称 |
| hot_value | int | 热度值 |
| change_percent | float | 涨跌幅(%) |
| sector_type | str | 板块类型: 'industry' 或 'concept' |
| hot_change | str | 热度排名变化 |
| leading_stock | str | 领涨信息 |
| fetch_time | datetime | 抓取时间 |

### MarketAnalysis - 盘面分析数据 (新增)

| 字段 | 类型 | 说明 |
|------|------|------|
| market_score | int | 市场综合评分(0-100) |
| risk_preference | str | 风险偏好(高/中/低) |
| market_comment | str | 当日市场点评 |
| rise_count | int | 上涨家数 |
| fall_count | int | 下跌家数 |
| flat_count | int | 平盘家数 |
| limit_up_count | int | 涨停数 |
| limit_down_count | int | 跌停数 |
| seal_rate | float | 封板率(%) |
| rise_distribution | dict | 涨幅分布 {">10%": 3, "10~7%": 8, ...} |
| fall_distribution | dict | 跌幅分布 {"0~-3%": 3032, ...} |
| inflow_sectors | list | 暗盘资金净流入数据 |
| outflow_sectors | list | 暗盘资金净流出数据 |
| today_volume | float | 今日总成交额(亿) |
| yesterday_volume | float | 昨日总成交额(亿) |
| trend_data | list | 涨跌趋势数据点 |
| fetch_time | datetime | 抓取时间 |

### HotSpotData - 热点数据汇总

| 字段 | 类型 | 说明 |
|------|------|------|
| hot_stocks_1h | list[HotStock] | 1小时热股列表 |
| hot_stocks_24h | list[HotStock] | 24小时热股列表 |
| hot_industry_sectors | list[HotSector] | 热门行业板块列表 |
| hot_concept_sectors | list[HotSector] | 热门概念板块列表 |
| market_analysis | MarketAnalysis | 盘面分析数据 (可选) |
| fetch_time | datetime | 抓取时间 |

## 配置选项

```python
# 热点数据抓取器
scraper = THSHotSpotScraper(
    timeout=30,        # 请求超时时间(秒)
    retry_count=3,     # 重试次数
    retry_delay=1.0    # 重试延迟(秒)
)

# 盘面分析抓取器
from ths_scraper import MarketAnalysisScraper

scraper = MarketAnalysisScraper(
    headless=True,     # 是否使用无头模式
    timeout=30000      # 页面加载超时时间(毫秒)
)
```

## 数据来源

数据来源于同花顺：
- 热点榜单: https://eq.10jqka.com.cn/frontend/thsTopRank/index.html
- 盘面分析: https://eq.10jqka.com.cn/webpage/kamis-renderer/index.html

## 注意事项

1. 本模块仅供学习研究使用
2. 请遵守同花顺的使用条款
3. 建议控制请求频率，避免对服务器造成压力
4. 数据仅供参考，不构成投资建议
5. 盘面分析功能需要安装 Playwright 和 Chromium 浏览器
