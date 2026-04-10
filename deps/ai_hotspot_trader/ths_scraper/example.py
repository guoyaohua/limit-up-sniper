"""
同花顺热点抓取模块使用示例
"""

import sys
import os

# 添加父目录到路径，以便作为模块运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from ths_scraper.scraper import THSHotSpotScraper

def main():
    """主函数演示如何使用抓取器"""
    
    # 方式1: 使用上下文管理器（推荐）
    print("=" * 70)
    print("同花顺热点数据抓取示例")
    print("=" * 70)
    
    with THSHotSpotScraper() as scraper:
        # 获取所有热点数据
        hot_data = scraper.get_all_hot_data()
        
        # 打印热点数据摘要
        print("\n" + "=" * 70)
        print("数据详情")
        print("=" * 70)
        
        # 显示1小时热股Top10（详细信息）
        print("\n【1小时热股 Top 10】")
        print("-" * 70)
        for stock in hot_data.hot_stocks_1h[:10]:
            change_str = f"+{stock.change_percent:.2f}%" if stock.change_percent > 0 else f"{stock.change_percent:.2f}%"
            rank_chg = ""
            if stock.hot_rank_change is not None:
                if stock.hot_rank_change > 0:
                    rank_chg = f"↑{stock.hot_rank_change}"
                elif stock.hot_rank_change < 0:
                    rank_chg = f"↓{abs(stock.hot_rank_change)}"
                else:
                    rank_chg = "→"
            
            print(f"  {stock.rank:2d}. [{stock.market_name}] {stock.code} {stock.name}")
            print(f"      热度: {stock.hot_value:,} | 涨跌: {change_str} | 排名变化: {rank_chg}")
            
            if stock.popularity_tag:
                print(f"      热度标签: {stock.popularity_tag}")
            if stock.concept_tags:
                print(f"      概念标签: {', '.join(stock.concept_tags)}")
            if stock.analyse_title:
                print(f"      分析标签: {stock.analyse_title}")
            if stock.topic:
                print(f"      热点话题: {stock.topic.title}")
            print()
        
        # 显示24小时热股Top5（简要信息）
        print("\n【24小时热股 Top 5】")
        print("-" * 70)
        for stock in hot_data.hot_stocks_24h[:5]:
            change_str = f"+{stock.change_percent:.2f}%" if stock.change_percent > 0 else f"{stock.change_percent:.2f}%"
            tags = ', '.join(stock.concept_tags[:2]) if stock.concept_tags else ''
            pop_tag = f"[{stock.popularity_tag}]" if stock.popularity_tag else ''
            print(f"  {stock.rank:2d}. {stock.code} {stock.name:<8s} | 热度:{stock.hot_value:>10,} | 涨跌:{change_str:>8s} | {pop_tag} {tags}")
        
        # 显示热门行业板块Top10
        print("\n【热门行业板块 Top 10】")
        print("-" * 70)
        for sector in hot_data.hot_industry_sectors[:10]:
            change_str = f"+{sector.change_percent:.2f}%" if sector.change_percent > 0 else f"{sector.change_percent:.2f}%"
            hot_tag = f"[{sector.hot_tag}]" if sector.hot_tag else ''
            rise_info = f"| {sector.rise_stop_info}" if sector.rise_stop_info else ''
            etf_info = ""
            if sector.etf_info:
                etf_change = f"+{sector.etf_info.rise_and_fall:.2f}%" if sector.etf_info.rise_and_fall > 0 else f"{sector.etf_info.rise_and_fall:.2f}%"
                etf_info = f"| ETF: {sector.etf_info.name}({etf_change})"
            print(f"  {sector.rank:2d}. {sector.name:<10s} | 热度:{sector.hot_value:>6,} | 涨跌:{change_str:>8s} {hot_tag} {rise_info} {etf_info}")
        
        # 显示热门概念板块Top10
        print("\n【热门概念板块 Top 10】")
        print("-" * 70)
        for sector in hot_data.hot_concept_sectors[:10]:
            change_str = f"+{sector.change_percent:.2f}%" if sector.change_percent > 0 else f"{sector.change_percent:.2f}%"
            hot_tag = f"[{sector.hot_tag}]" if sector.hot_tag else ''
            rise_info = f"| {sector.rise_stop_info}" if sector.rise_stop_info else ''
            etf_info = ""
            if sector.etf_info:
                etf_change = f"+{sector.etf_info.rise_and_fall:.2f}%" if sector.etf_info.rise_and_fall > 0 else f"{sector.etf_info.rise_and_fall:.2f}%"
                etf_info = f"| ETF: {sector.etf_info.name}({etf_change})"
            print(f"  {sector.rank:2d}. {sector.name:<12s} | 热度:{sector.hot_value:>6,} | 涨跌:{change_str:>8s} {hot_tag} {rise_info} {etf_info}")
        
        # 导出为JSON
        print("\n" + "=" * 70)
        print("导出数据为JSON格式")
        print("=" * 70)
        
        json_data = hot_data.to_dict()
        
        # 保存到文件
        output_file = "hot_data_output.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        print(f"\n数据已保存到: {output_file}")
        
        # 显示单条记录的完整信息示例
        print("\n" + "=" * 70)
        print("单条热股完整数据示例 (第一名)")
        print("=" * 70)
        if hot_data.hot_stocks_1h:
            stock = hot_data.hot_stocks_1h[0]
            print(f"""
代码: {stock.code}
名称: {stock.name}
市场: {stock.market_name} (ID: {stock.market})
排名: {stock.rank}
热度值: {stock.hot_value:,}
涨跌幅: {stock.change_percent:.2f}%
排名变化: {stock.hot_rank_change}
概念标签: {stock.concept_tags}
热度标签: {stock.popularity_tag}
分析标题: {stock.analyse_title}
话题: {stock.topic.title if stock.topic else '无'}
AI分析: {stock.analyse[:200] + '...' if len(stock.analyse) > 200 else stock.analyse or '无'}
""")

def example_single_api():
    """演示单独调用各个API"""
    
    scraper = THSHotSpotScraper()
    
    try:
        # 只获取1小时热股
        print("\n只获取1小时热股 (Top 5):")
        stocks_1h = scraper.get_hot_stocks_1h(limit=5)
        for stock in stocks_1h:
            print(f"  {stock.rank}. {stock.code} {stock.name} - {stock.popularity_tag or '无标签'}")
        
        # 只获取热门概念板块
        print("\n只获取热门概念板块 (Top 5):")
        concepts = scraper.get_hot_concept_sectors(limit=5)
        for sector in concepts:
            print(f"  {sector.rank}. {sector.name} - {sector.hot_tag} {sector.rise_stop_info}")
            
    finally:
        scraper.close()

if __name__ == "__main__":
    # 运行主示例
    main()
    
    # 运行单独API调用示例
    # example_single_api()
