"""
测试同花顺板块成分股抓取（带登录功能）
"""
import sys
sys.path.append('d:\\Project\\Quant\\src\\AI\\打板策略')

from scraper.tonghuashun_scraper_combined import TonghuashunAPI
import logging

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_sector_with_login():
    """测试获取板块成分股（可能需要登录）"""
    
    # 测试板块代码
    # 883993 - 昨日首板表现（可能超过5页）
    # 881101 - 5G（通常有很多成分股）
    sector_code = "881101"  # 5G板块，通常有较多成分股
    
    print("=" * 60)
    print(f"测试获取同花顺板块 {sector_code} 的成分股")
    print("注意：如果板块成分股超过5页（约250只），需要登录")
    print("=" * 60)
    
    try:
        # 使用非无头模式，以便用户可以登录
        with TonghuashunAPI(headless=False) as api:
            # 获取板块基本信息
            print(f"\n1. 获取板块 {sector_code} 的基本信息...")
            sector_info = api.get_sector_info(sector_code)
            if sector_info:
                print("\n板块基本信息：")
                for key, value in sector_info.items():
                    print(f"  {key}: {value}")
            
            # 获取成分股
            print(f"\n2. 获取板块 {sector_code} 的成分股...")
            print("提示：如果板块成分股超过5页，程序会打开登录页面")
            print("请在浏览器中完成登录，然后按回车键继续\n")
            
            stocks_df = api.get_sector_stocks(sector_code)
            
            if not stocks_df.empty:
                print(f"\n成功获取 {len(stocks_df)} 只成分股")
                print("\n前10只股票：")
                print(stocks_df.head(10)[['代码', '名称', '最新价', '涨跌幅']].to_string(index=False))
                
                # 显示最后10只股票（验证是否获取了后续页面）
                if len(stocks_df) > 20:
                    print("\n最后10只股票：")
                    print(stocks_df.tail(10)[['代码', '名称', '最新价', '涨跌幅']].to_string(index=False))
                
                # 保存数据
                filename = f"test_sector_{sector_code}_stocks.csv"
                stocks_df.to_csv(filename, index=False, encoding='utf-8-sig')
                print(f"\n数据已保存到: {filename}")
                
                # 检查是否获取了超过5页的数据
                if len(stocks_df) > 250:
                    print("\n✅ 成功获取超过5页的数据，登录功能正常工作！")
                
    except KeyboardInterrupt:
        print("\n用户中断了操作")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_sector_with_login()
    print("\n测试完成")