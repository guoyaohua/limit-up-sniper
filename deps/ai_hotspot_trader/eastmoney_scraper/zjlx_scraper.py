"""
东方财富个股资金流向爬虫
抓取主力净流入排行（支持今日/3日/5日/10日）
"""

import asyncio
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional

import aiohttp

try:
    from logger_config import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)


@dataclass
class FundFlowItem:
    """个股资金流向数据"""
    code: str = ""                     # 股票代码（无后缀）
    name: str = ""                     # 股票名称
    price: float = 0.0                 # 最新价（元）
    change_pct: float = 0.0            # 涨跌幅（%）
    main_net_inflow: float = 0.0       # 主力净流入（元）
    main_net_inflow_pct: float = 0.0   # 主力净占比（%）
    super_large_net_inflow: float = 0.0      # 超大单净流入（元）
    super_large_net_inflow_pct: float = 0.0  # 超大单净占比（%）
    large_net_inflow: float = 0.0            # 大单净流入（元）
    large_net_inflow_pct: float = 0.0        # 大单净占比（%）
    medium_net_inflow: float = 0.0           # 中单净流入（元）
    medium_net_inflow_pct: float = 0.0       # 中单净占比（%）
    small_net_inflow: float = 0.0            # 小单净流入（元）
    small_net_inflow_pct: float = 0.0        # 小单净占比（%）
    market_id: int = 0                 # 市场代码（0=深/京，1=沪）
    secid: str = ""                    # SecID（市场.代码）

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class EastMoneyZJLXScraper:
    """东方财富个股资金流向抓取器（API方式）"""

    API_URL = "https://push2.eastmoney.com/api/qt/clist/get"
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }

    MARKET_VALUES = {
        "all": "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,"
               "m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2",
        "hsa": "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2",
        "sha": "m:1+t:2+f:!2,m:1+t:23+f:!2",
        "kcb": "m:1+t:23+f:!2",
        "sza": "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2",
        "cyb": "m:0+t:80+f:!2",
        "hb": "m:1+t:3+f:!2",
        "sb": "m:0+t:7+f:!2",
        "bja": "m:0+t:81+s:262144+f:!2",
    }

    PERIOD_FIELDS = {
        "1": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f1,f13",
        "3": "f12,f14,f2,f127,f267,f268,f269,f270,f271,f272,f273,f274,f275,f276,f1,f13",
        "5": "f12,f14,f2,f109,f164,f165,f166,f167,f168,f169,f170,f171,f172,f173,f1,f13",
        "10": "f12,f14,f2,f160,f174,f175,f176,f177,f178,f179,f180,f181,f182,f183,f1,f13",
    }

    PERIOD_SORT_FIELD = {
        "1": "f62",
        "3": "f267",
        "5": "f164",
        "10": "f174",
    }

    def __init__(self, data_dir: Optional[str] = None):
        self._base_data_dir = data_dir

    @property
    def data_dir(self) -> str:
        date_str = datetime.now().strftime("%Y%m%d")
        if self._base_data_dir:
            return self._base_data_dir
        return os.path.join("output", "eastmoney_zjlx", date_str)

    def _ensure_data_dir(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)

    async def fetch_top_inflow(self, top: int = 100, market: str = "all", period: str = "1") -> List[FundFlowItem]:
        """抓取主力净流入 Top N 个股"""
        if period not in self.PERIOD_FIELDS:
            raise ValueError(f"不支持的周期: {period}")
        if market not in self.MARKET_VALUES:
            raise ValueError(f"不支持的市场: {market}")

        fields = self.PERIOD_FIELDS[period]
        fid = self.PERIOD_SORT_FIELD[period]
        params = {
            "pn": "1",
            "pz": str(top),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": fid,
            "fs": self.MARKET_VALUES[market],
            "fields": fields,
            "ut": "8dec03ba335b81bf4ebdf7b29ec27d15",
        }

        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        last_error: Optional[Exception] = None
        max_retries = 2  # 总尝试次数 = 1 + max_retries

        async with aiohttp.ClientSession(headers=self.DEFAULT_HEADERS, timeout=timeout, trust_env=True) as session:
            for attempt in range(max_retries + 1):
                try:
                    async with session.get(self.API_URL, params=params) as response:
                        response.raise_for_status()
                        payload = await response.json(content_type=None)

                    items = payload.get("data", {}).get("diff", []) if payload else []
                    return [self._parse_item(item, period) for item in items if item]

                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                ) as e:
                    last_error = e
                    if attempt < max_retries:
                        sleep_seconds = 1 + attempt  # 1s, 2s
                        logger.warning(
                            f"资金流向请求失败，第 {attempt + 1}/{max_retries + 1} 次尝试: {e}，"
                            f"{sleep_seconds}s 后重试"
                        )
                        await asyncio.sleep(sleep_seconds)
                        continue
                    raise

        if last_error:
            raise last_error
        return []

    def _parse_item(self, item: Dict[str, object], period: str) -> FundFlowItem:
        def get_float(key: str) -> float:
            value = item.get(key, 0) if isinstance(item, dict) else 0
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        if period == "1":
            main_key, main_pct = "f62", "f184"
            super_key, super_pct = "f66", "f69"
            large_key, large_pct = "f72", "f75"
            medium_key, medium_pct = "f78", "f81"
            small_key, small_pct = "f84", "f87"
            change_key = "f3"
        elif period == "3":
            main_key, main_pct = "f267", "f268"
            super_key, super_pct = "f269", "f270"
            large_key, large_pct = "f271", "f272"
            medium_key, medium_pct = "f273", "f274"
            small_key, small_pct = "f275", "f276"
            change_key = "f127"
        elif period == "5":
            main_key, main_pct = "f164", "f165"
            super_key, super_pct = "f166", "f167"
            large_key, large_pct = "f168", "f169"
            medium_key, medium_pct = "f170", "f171"
            small_key, small_pct = "f172", "f173"
            change_key = "f109"
        else:
            main_key, main_pct = "f174", "f175"
            super_key, super_pct = "f176", "f177"
            large_key, large_pct = "f178", "f179"
            medium_key, medium_pct = "f180", "f181"
            small_key, small_pct = "f182", "f183"
            change_key = "f160"

        code = str(item.get("f12", "")).strip()
        name = str(item.get("f14", "")).strip()
        market_id = int(item.get("f13", 0) or 0)
        secid = f"{market_id}.{code}" if code and market_id else ""

        return FundFlowItem(
            code=code,
            name=name,
            price=get_float("f2"),
            change_pct=get_float(change_key),
            main_net_inflow=get_float(main_key),
            main_net_inflow_pct=get_float(main_pct),
            super_large_net_inflow=get_float(super_key),
            super_large_net_inflow_pct=get_float(super_pct),
            large_net_inflow=get_float(large_key),
            large_net_inflow_pct=get_float(large_pct),
            medium_net_inflow=get_float(medium_key),
            medium_net_inflow_pct=get_float(medium_pct),
            small_net_inflow=get_float(small_key),
            small_net_inflow_pct=get_float(small_pct),
            market_id=market_id,
            secid=secid,
        )

    def save_to_tsv(self, items: List[FundFlowItem], period: str = "1") -> str:
        """保存为 TSV 文件"""
        self._ensure_data_dir()
        date_str = datetime.now().strftime("%Y%m%d")
        filename = f"{date_str}_zjlx_top_{period}d.tsv"
        filepath = os.path.join(self.data_dir, filename)

        headers = [
            "代码",
            "名称",
            "最新价",
            "涨跌幅",
            "主力净流入",
            "主力净占比",
            "超大单净流入",
            "超大单净占比",
            "大单净流入",
            "大单净占比",
            "中单净流入",
            "中单净占比",
            "小单净流入",
            "小单净占比",
            "市场",
            "SecID",
        ]

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\t".join(headers) + "\n")
            for item in items:
                row = [
                    item.code,
                    item.name,
                    f"{item.price:.2f}",
                    f"{item.change_pct:.2f}",
                    f"{item.main_net_inflow:.2f}",
                    f"{item.main_net_inflow_pct:.2f}",
                    f"{item.super_large_net_inflow:.2f}",
                    f"{item.super_large_net_inflow_pct:.2f}",
                    f"{item.large_net_inflow:.2f}",
                    f"{item.large_net_inflow_pct:.2f}",
                    f"{item.medium_net_inflow:.2f}",
                    f"{item.medium_net_inflow_pct:.2f}",
                    f"{item.small_net_inflow:.2f}",
                    f"{item.small_net_inflow_pct:.2f}",
                    str(item.market_id),
                    item.secid,
                ]
                f.write("\t".join(row) + "\n")

        logger.info(f"资金流数据已保存: {filepath}")
        return filepath


async def fetch_top_inflow(period: str = "1", top: int = 100, market: str = "all", data_dir: Optional[str] = None) -> List[FundFlowItem]:
    scraper = EastMoneyZJLXScraper(data_dir=data_dir)
    return await scraper.fetch_top_inflow(top=top, market=market, period=period)


def fetch_top_inflow_sync(period: str = "1", top: int = 100, market: str = "all", data_dir: Optional[str] = None) -> List[FundFlowItem]:
    return asyncio.run(fetch_top_inflow(period=period, top=top, market=market, data_dir=data_dir))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="东方财富个股资金流向爬虫")
    parser.add_argument("--period", type=str, default="1", choices=["1", "3", "5", "10"], help="周期：1/3/5/10 日")
    parser.add_argument("--top", type=int, default=100, help="抓取 Top N")
    parser.add_argument("--market", type=str, default="all", help="市场过滤")
    parser.add_argument("--output", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    async def main() -> None:
        scraper = EastMoneyZJLXScraper(data_dir=args.output)
        items = await scraper.fetch_top_inflow(top=args.top, market=args.market, period=args.period)
        if items:
            scraper.save_to_tsv(items, period=args.period)
            print(f"抓取完成: {len(items)} 条")

    asyncio.run(main())