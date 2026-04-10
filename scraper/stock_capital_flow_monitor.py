"""
东方财富个股资金流向监控器
独立监控器，不依赖 eastmoney_scraper 包
"""

import pandas as pd
from typing import Optional, Callable
import threading
import time
from datetime import datetime
import logging

# 导入本地的爬虫模块
from .em_stock_capital_flow_scraper import StockCapitalFlowScraper, MarketType

# 获取日志记录器
logger = logging.getLogger(__name__)


class StockCapitalFlowMonitor:
    """
    个股资金流向监控器
    
    用于实时监控股票市场的资金流向数据，支持定时更新和回调通知。
    """
    
    def __init__(self, 
                 market_type: MarketType = MarketType.ALL, 
                 output_dir: Optional[str] = None, 
                 max_pages: Optional[int] = 10):
        """
        初始化监控器
        
        Args:
            market_type: 市场类型 (ALL/MAIN_BOARD/GEM/STAR/BSE)
            output_dir: 数据输出目录
            max_pages: 最大爬取页数
        """
        self.market_type = market_type
        self.max_pages = max_pages
        self.scraper = StockCapitalFlowScraper(market_type=market_type, output_dir=output_dir)
        
        # 监控状态控制
        self.is_running = False
        self.thread: Optional[threading.Thread] = None
        
        # 回调函数和数据存储
        self.callback: Optional[Callable[[pd.DataFrame], None]] = None
        self.interval = 10  # 默认更新间隔
        self.last_data: Optional[pd.DataFrame] = None
        
        logger.info(f"StockCapitalFlowMonitor 初始化完成 (市场: {market_type.value}, 最大页数: {self.max_pages})")
    
    def set_callback(self, callback: Callable[[pd.DataFrame], None]) -> None:
        """
        设置数据更新回调函数
        
        Args:
            callback: 回调函数，接收 DataFrame 参数
        """
        self.callback = callback
        logger.debug("数据更新回调函数已设置")
    
    def get_latest_data(self) -> Optional[pd.DataFrame]:
        """
        获取最新的个股资金流向数据
        
        Returns:
            最新数据的 DataFrame，如果没有数据则返回 None
        """
        return self.last_data
    
    def start(self, interval: int = 10) -> None:
        """
        启动监控器
        
        Args:
            interval: 数据更新间隔（秒）
        """
        if self.is_running:
            logger.warning("监控器已在运行中，无法重复启动")
            return
        
        self.interval = interval
        self.is_running = True
        
        # 创建并启动监控线程
        self.thread = threading.Thread(
            target=self._run,
            name="StockCapitalFlowMonitorThread",
            daemon=True
        )
        self.thread.start()
        
        logger.info(f"监控器已启动，数据更新间隔: {interval}秒")
    
    def stop(self) -> None:
        """停止监控器"""
        if not self.is_running:
            logger.info("监控器未在运行")
            return
        
        self.is_running = False
        
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("监控线程在5秒内未能正常结束")
            else:
                logger.info("监控线程已正常结束")
        
        self.thread = None
        logger.info("监控器已停止")
    
    def _run(self) -> None:
        """监控器主循环（内部方法）"""
        logger.info(f"监控循环已开始 (市场: {self.market_type.value}, 最大页数: {self.max_pages})")
        
        while self.is_running:
            try:
                # 获取个股资金流向数据
                df, filepath = self.scraper.run_once(max_pages=self.max_pages, save_format=None)
                
                if df is not None and not df.empty:
                    # 更新最新数据
                    self.last_data = df
                    
                    # 调用回调函数
                    if self.callback:
                        try:
                            self.callback(df.copy())
                        except Exception as e:
                            logger.exception(f"回调函数执行出错: {e}")
                    
                    if filepath:
                        logger.debug(f"成功获取 {len(df)} 只股票的资金流向数据，保存到: {filepath}")
                    else:
                        logger.debug(f"成功获取 {len(df)} 只股票的资金流向数据")
                else:
                    logger.warning("获取到的个股资金流向数据为空或获取失败")
                
                # 等待下次更新
                if self.is_running:
                    time.sleep(self.interval)
            
            except KeyboardInterrupt:
                logger.info("监控器收到键盘中断信号，正在退出...")
                break
            except Exception as e:
                logger.error(f"监控过程发生异常: {e}", exc_info=True)
                if self.is_running:
                    time.sleep(min(self.interval, 30))
        
        logger.info(f"监控循环已结束 (市场: {self.market_type.value})")
    
    def get_market_summary(self, df: Optional[pd.DataFrame] = None) -> dict:
        """
        获取市场概况统计
        
        Args:
            df: 数据 DataFrame，如果为 None 则使用最新数据
        
        Returns:
            包含市场统计信息的字典
        """
        if df is None:
            df = self.last_data
        
        if df is None or df.empty:
            return {}
        
        summary = {
            '总股票数': len(df),
            '上涨股票数': len(df[df['涨跌幅'] > 0]) if '涨跌幅' in df.columns else 0,
            '下跌股票数': len(df[df['涨跌幅'] < 0]) if '涨跌幅' in df.columns else 0,
            '主力净流入股票数': len(df[df['主力净流入'] > 0]) if '主力净流入' in df.columns else 0,
            '主力净流出股票数': len(df[df['主力净流入'] < 0]) if '主力净流入' in df.columns else 0,
            '市场总流入(万元)': df['主力净流入'].sum() if '主力净流入' in df.columns else 0,
            '平均流入(万元)': df['主力净流入'].mean() if '主力净流入' in df.columns else 0,
            '更新时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        return summary


def main():
    """示例用法"""
    import sys
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 创建监控器实例
    monitor = StockCapitalFlowMonitor(market_type=MarketType.ALL, max_pages=2)
    
    # 定义回调函数
    def on_data_update(df: pd.DataFrame):
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
        
        # 显示TOP5主力净流入
        if '主力净流入' in df.columns:
            top5 = df.nlargest(5, '主力净流入')[['股票代码', '股票名称', '最新价', '涨跌幅', '主力净流入']]
            print(f"\n【主力净流入TOP5】")
            print(top5.to_string(index=False))
        
        print(f"{'='*80}\n")
    
    
    def stock_capital_flow_callback(df_stock: pd.DataFrame):
        """个股资金流数据更新回调"""
        current_time = datetime.now().strftime("%H:%M:%S")
        logger.debug(f"💰 [{current_time}] 个股资金流更新：{len(df_stock)} 只股票")

        logger.debug(f'\n{df_stock.head()}')
        if df_stock.empty or df_stock['主力净流入'].isna().any():
            logger.warning("⚠️ 个股资金流数据异常，跳过处理")
            return

        # 大额流入统计
        large_inflow_count = len(df_stock[df_stock['主力净流入'] > 10000])
        logger.debug(f"   💸 大额流入(>1亿)：{large_inflow_count} 只")

        df_stock = df_stock.loc[df_stock['涨跌幅'] > 0]
        for _, row in df_stock.iterrows():
            msg = ''
            if row['主力净流入'] > 5000 and row['主力净流入占比'] > 0.1:
                msg += f'主力净流入: {row["主力净流入"]}, 主力净流入占比: {row["主力净流入占比"]}\t'

            if row['超大单净流入'] > 3000 and row['超大单净流入占比'] > 0.05:
                msg += f'超大单净流入: {row["超大单净流入"]}, 超大单净流入占比: {row["超大单净流入占比"]}\t'

            if msg:
                logger.debug(
                    f'[{row["股票名称"]}] {row["股票代码"]} 最新价: {row["最新价"]}, 涨跌幅: {row["涨跌幅"]}, 主力净流入: {row["主力净流入"]}, 主力净流入占比: {row["主力净流入占比"]}, 超大单净流入: {row["超大单净流入"]}, 超大单净流入占比: {row["超大单净流入占比"]}'
                )

    # 设置回调并启动
    monitor.set_callback(stock_capital_flow_callback)
    monitor.start(interval=30)
    
    try:
        # 保持运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n接收到退出信号...")
        monitor.stop()
        print("程序已退出")


if __name__ == "__main__":
    main()