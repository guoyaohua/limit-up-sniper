"""
测试盘面分析抓取功能
"""

import sys
import os
import json

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ths_scraper import THSHotSpotScraper, get_market_analysis

def test_market_analysis():
    """测试盘面分析功能"""
    print("="*60)
    print("测试盘面分析抓取功能")
    print("="*60)
    
    try:
        print("\n正在抓取盘面分析数据...")
        analysis = get_market_analysis(headless=True)
        
        if analysis:
            print("\n" + analysis.summary())
            print("\n详细数据:")
            print(json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2))
        else:
            print("获取盘面分析数据失败")
            
    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()

def test_full_scraper():
    """测试完整抓取功能"""
    print("\n" + "="*60)
    print("测试完整热点数据抓取（包含盘面分析）")
    print("="*60)
    
    try:
        with THSHotSpotScraper() as scraper:
            hot_data = scraper.get_all_hot_data(include_market_analysis=True)
            
            print("\n" + hot_data.summary())
            
            if hot_data.market_analysis:
                print("\n盘面分析详情:")
                print(hot_data.market_analysis.summary())
                
    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    # 测试盘面分析
    test_market_analysis()
    
    # 测试完整功能（包含热点数据和盘面分析）
    test_full_scraper()
