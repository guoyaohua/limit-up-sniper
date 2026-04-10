"""
数据模型定义
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime

@dataclass
class TopicInfo:
    """话题信息"""
    topic_code: str = ""
    title: str = ""
    ios_jump_url: str = ""
    android_jump_url: str = ""

@dataclass
class HotStock:
    """热门股票数据模型"""
    rank: int  # 排名
    code: str  # 股票代码
    name: str  # 股票名称
    hot_value: int  # 热度值
    change_percent: float  # 涨跌幅(%)
    market: int = 0  # 市场ID (33=深市, 17=沪市)
    hot_rank_change: Optional[int] = None  # 热度排名变化
    concept_tags: list[str] = field(default_factory=list)  # 概念标签列表
    popularity_tag: str = ""  # 热度标签(如: 持续上榜, 首板涨停, 3天3板)
    analyse: str = ""  # AI分析内容
    analyse_title: str = ""  # 分析标题/简要标签
    topic: Optional[TopicInfo] = None  # 话题信息
    fetch_time: datetime = field(default_factory=datetime.now)  # 抓取时间
    
    @property
    def market_name(self) -> str:
        """获取市场名称"""
        return {33: "深市", 17: "沪市", 48: "北交所"}.get(self.market, f"未知({self.market})")
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'rank': self.rank,
            'code': self.code,
            'name': self.name,
            'market': self.market,
            'market_name': self.market_name,
            'hot_value': self.hot_value,
            'change_percent': self.change_percent,
            'hot_rank_change': self.hot_rank_change,
            'concept_tags': self.concept_tags,
            'popularity_tag': self.popularity_tag,
            'analyse': self.analyse,
            'analyse_title': self.analyse_title,
            'topic': {
                'topic_code': self.topic.topic_code,
                'title': self.topic.title,
                'ios_jump_url': self.topic.ios_jump_url,
                'android_jump_url': self.topic.android_jump_url,
            } if self.topic else None,
            'fetch_time': self.fetch_time.isoformat()
        }

@dataclass
class ETFInfo:
    """关联ETF信息"""
    product_id: str = ""  # ETF代码
    name: str = ""  # ETF名称
    rise_and_fall: float = 0.0  # ETF涨跌幅
    market_id: int = 0  # ETF市场ID

@dataclass
class HotSector:
    """热门板块数据模型"""
    rank: int  # 排名
    code: str  # 板块代码
    name: str  # 板块名称
    hot_value: int  # 热度值
    change_percent: float  # 涨跌幅(%)
    sector_type: str  # 板块类型: 'industry'(行业) 或 'concept'(概念)
    market_id: int = 0  # 市场ID
    hot_rank_change: Optional[int] = None  # 热度排名变化
    hot_tag: str = ""  # 热度标签(如: 连续48天上榜)
    rise_stop_info: str = ""  # 涨停信息(如: 18家涨停)
    etf_info: Optional[ETFInfo] = None  # 关联ETF信息
    fetch_time: datetime = field(default_factory=datetime.now)  # 抓取时间
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'rank': self.rank,
            'code': self.code,
            'name': self.name,
            'market_id': self.market_id,
            'hot_value': self.hot_value,
            'change_percent': self.change_percent,
            'sector_type': self.sector_type,
            'hot_rank_change': self.hot_rank_change,
            'hot_tag': self.hot_tag,
            'rise_stop_info': self.rise_stop_info,
            'etf_info': {
                'product_id': self.etf_info.product_id,
                'name': self.etf_info.name,
                'rise_and_fall': self.etf_info.rise_and_fall,
                'market_id': self.etf_info.market_id,
            } if self.etf_info else None,
            'fetch_time': self.fetch_time.isoformat()
        }

@dataclass
class MarketAnalysis:
    """盘面分析数据模型"""
    
    # 当日综合评分
    market_score: float = 0.0  # 市场评分(0-10)
    
    # 当日点评/情绪信号
    market_comment: str = ""  # 市场点评
    
    # 涨跌家数统计
    rise_count: int = 0  # 上涨家数
    fall_count: int = 0  # 下跌家数
    flat_count: int = 0  # 平盘家数
    
    # 涨跌停统计
    limit_up_count: int = 0  # 涨停数
    limit_down_count: int = 0  # 跌停数
    
    # 涨跌分布 {">10%": 3, "10~7%": 8, "7~5%": 26, ...}
    rise_distribution: Dict[str, int] = field(default_factory=dict)
    fall_distribution: Dict[str, int] = field(default_factory=dict)
    
    # 资金流向 (暗盘资金)
    inflow_sectors: List[Dict] = field(default_factory=list)  # 净流入 [{name, count}]
    outflow_sectors: List[Dict] = field(default_factory=list)  # 净流出 [{name, count}]
    
    # 市场成交额（当前时刻）
    today_volume: float = 0.0  # 今日当前成交额(亿)
    yesterday_volume: float = 0.0  # 昨日同时刻成交额(亿)
    
    # 抓取时间
    fetch_time: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'market_score': self.market_score,
            'market_comment': self.market_comment,
            'rise_count': self.rise_count,
            'fall_count': self.fall_count,
            'flat_count': self.flat_count,
            'limit_up_count': self.limit_up_count,
            'limit_down_count': self.limit_down_count,
            'rise_distribution': self.rise_distribution,
            'fall_distribution': self.fall_distribution,
            'inflow_sectors': self.inflow_sectors,
            'outflow_sectors': self.outflow_sectors,
            'today_volume': self.today_volume,
            'yesterday_volume': self.yesterday_volume,
            'fetch_time': self.fetch_time.isoformat()
        }
    
    def summary(self) -> str:
        """返回数据摘要"""
        return (
            f"盘面分析摘要 (抓取时间: {self.fetch_time.strftime('%Y-%m-%d %H:%M:%S')})\n"
            f"【综合评分】{self.market_score}分\n"
            f"【涨跌家数】上涨 {self.rise_count} / 下跌 {self.fall_count} / 平盘 {self.flat_count}\n"
            f"【涨跌停】涨停 {self.limit_up_count} / 跌停 {self.limit_down_count}\n"
            f"【成交额】今日 {self.today_volume}亿 / 昨日同期 {self.yesterday_volume}亿"
        )

@dataclass
class HotSpotData:
    """热点数据汇总"""
    hot_stocks_1h: list[HotStock] = field(default_factory=list)  # 1小时热股
    hot_stocks_24h: list[HotStock] = field(default_factory=list)  # 24小时热股
    hot_industry_sectors: list[HotSector] = field(default_factory=list)  # 热门行业板块
    hot_concept_sectors: list[HotSector] = field(default_factory=list)  # 热门概念板块
    market_analysis: Optional[MarketAnalysis] = None  # 盘面分析数据
    fetch_time: datetime = field(default_factory=datetime.now)  # 抓取时间
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'hot_stocks_1h': [s.to_dict() for s in self.hot_stocks_1h],
            'hot_stocks_24h': [s.to_dict() for s in self.hot_stocks_24h],
            'hot_industry_sectors': [s.to_dict() for s in self.hot_industry_sectors],
            'hot_concept_sectors': [s.to_dict() for s in self.hot_concept_sectors],
            'market_analysis': self.market_analysis.to_dict() if self.market_analysis else None,
            'fetch_time': self.fetch_time.isoformat()
        }
    
    def summary(self) -> str:
        """返回数据摘要"""
        summary_str = (
            f"热点数据摘要 (抓取时间: {self.fetch_time.strftime('%Y-%m-%d %H:%M:%S')})\n"
            f"- 1小时热股: {len(self.hot_stocks_1h)} 只\n"
            f"- 24小时热股: {len(self.hot_stocks_24h)} 只\n"
            f"- 热门行业板块: {len(self.hot_industry_sectors)} 个\n"
            f"- 热门概念板块: {len(self.hot_concept_sectors)} 个"
        )
        if self.market_analysis:
            summary_str += f"\n- 盘面分析: 已获取 (评分: {self.market_analysis.market_score}分)"
        return summary_str
