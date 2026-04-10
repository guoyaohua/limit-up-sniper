"""
同花顺热点股票/板块抓取模块

功能：
- 1小时热股
- 24小时热股
- 热门行业板块
- 热门概念板块
- 盘面分析数据（需要 Playwright）

使用示例：
    from ths_scraper import THSHotSpotScraper
    
    with THSHotSpotScraper() as scraper:
        # 获取所有热点数据
        hot_data = scraper.get_all_hot_data()
        
        # 获取所有数据（包含盘面分析）
        hot_data = scraper.get_all_hot_data(include_market_analysis=True)
        
        # 或者单独获取
        stocks_1h = scraper.get_hot_stocks_1h()
        stocks_24h = scraper.get_hot_stocks_24h()
        industry_sectors = scraper.get_hot_industry_sectors()
        concept_sectors = scraper.get_hot_concept_sectors()
        market_analysis = scraper.get_market_analysis()

盘面分析数据抓取：
    from ths_scraper import MarketAnalysisScraper, get_market_analysis
    
    # 方式1: 使用便捷函数
    analysis = get_market_analysis()
    
    # 方式2: 使用上下文管理器
    import asyncio
    async def main():
        async with MarketAnalysisScraper() as scraper:
            analysis = await scraper.get_market_analysis()
            print(analysis.summary())
    asyncio.run(main())
"""

from .scraper import THSHotSpotScraper
from .models import HotStock, HotSector, HotSpotData, TopicInfo, ETFInfo, MarketAnalysis

# 盘面分析相关导入（可选，需要 Playwright）
try:
    from .market_analysis_scraper import (
        MarketAnalysisScraper,
        get_market_analysis,
        get_market_analysis_async
    )
    _market_analysis_available = True
except ImportError:
    _market_analysis_available = False
    MarketAnalysisScraper = None
    get_market_analysis = None
    get_market_analysis_async = None

__all__ = [
    'THSHotSpotScraper',
    'HotStock',
    'HotSector', 
    'HotSpotData',
    'TopicInfo',
    'ETFInfo',
    'MarketAnalysis',
    'MarketAnalysisScraper',
    'get_market_analysis',
    'get_market_analysis_async',
]
__version__ = '1.1.0'
