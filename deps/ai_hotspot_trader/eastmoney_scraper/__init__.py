"""
东方财富网涨停板行情爬虫
"""

__all__ = ['EastMoneyZTBScraper', 'scrape_all_pools', 'scrape_all_pools_async']

def __getattr__(name):
    """延迟导入，避免 RuntimeWarning"""
    if name in ('EastMoneyZTBScraper', 'scrape_all_pools', 'scrape_all_pools_async', 'StockInfo'):
        from . import scraper
        return getattr(scraper, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
