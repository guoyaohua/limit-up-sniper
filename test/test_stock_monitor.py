"""
测试个股资金流监控器
"""
import sys
import logging
from scraper.stock_capital_flow_monitor import StockCapitalFlowMonitor, MarketType
import time

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)


def main():
    """测试监控器"""
    logger.info("开始测试个股资金流监控器...")
    
    # 创建监控器实例 - 只获取1页数据用于测试
    monitor = StockCapitalFlowMonitor(
        market_type=MarketType.ALL, 
        max_pages=1  # 测试时只获取1页
    )
    
    # 定义回调函数
    def on_data_update(df):
        """数据更新回调"""
        print(f"\n{'='*80}")
        print(f"💰 个股资金流数据更新：{len(df)} 只股票")
        print(f"{'='*80}")
        
        # 显示市场概况
        summary = monitor.get_market_summary(df)
        if summary:
            print(f"\n【市场概况】 {summary['更新时间']}")
            print(f"  总股票数: {summary['总股票数']}")
            print(f"  上涨: {summary['上涨股票数']} | 下跌: {summary['下跌股票数']}")
            print(f"  资金流入股票: {summary['主力净流入股票数']} | 流出: {summary['主力净流出股票数']}")
            print(f"  市场总流入: {summary['市场总流入(万元)']:.2f}万元")
        
        # 显示TOP3主力净流入
        if '主力净流入' in df.columns and len(df) > 0:
            top3 = df.nlargest(3, '主力净流入')[['股票代码', '股票名称', '最新价', '涨跌幅', '主力净流入']]
            print(f"\n【主力净流入TOP3】")
            print(top3.to_string(index=False))
        
        print(f"{'='*80}\n")
    
    # 设置回调
    monitor.set_callback(on_data_update)
    
    # 启动监控 - 测试时使用60秒间隔避免频繁请求
    monitor.start(interval=10)
    
    logger.info("监控器已启动，按 Ctrl+C 停止...")
    
    try:
        # 运行60秒后自动停止（测试用）
        time.sleep(200)
        logger.info("测试完成，准备停止...")
    except KeyboardInterrupt:
        logger.info("接收到退出信号...")
    finally:
        monitor.stop()
        logger.info("测试结束")


if __name__ == "__main__":
    main()