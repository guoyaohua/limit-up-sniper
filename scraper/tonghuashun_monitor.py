"""
同花顺板块实时监控器
用于监控指定板块的实时行情
"""
import threading
import time
from typing import Dict, List, Optional, Callable, Union
from datetime import datetime
from loguru import logger
from scraper.tonghuashun_scraper_combined import TonghuashunAPI


class TonghuashunMonitor:
    """
    同花顺板块实时行情监控器（简化版）
    
    主要功能:
    - 定时获取板块实时行情信息
    - 通过回调函数处理数据
    - 线程安全的启停控制
    """
    def __init__(self,
                 sector_codes: Union[str, List[str]],
                 headless: bool = True,
                 edge_driver_path: Optional[str] = None):
        """
        初始化监控器
        
        Args:
            sector_codes: 板块代码或板块代码列表
            headless: 是否使用无头模式
            edge_driver_path: EdgeDriver路径
        """
        # 处理板块代码参数
        if isinstance(sector_codes, str):
            self.sector_codes = [sector_codes]
        else:
            self.sector_codes = list(sector_codes)

        # API配置
        self.headless = headless
        self.edge_driver_path = edge_driver_path
        self.api: Optional[TonghuashunAPI] = None

        # 监控状态
        self.is_running = False
        self.thread: Optional[threading.Thread] = None
        self.interval = 30  # 默认30秒更新一次

        # 回调函数
        self.callback: Optional[Callable[[str, Dict], None]] = None

        logger.info(f"同花顺监控器已初始化，监控板块: {self.sector_codes}")

    def set_callback(self, callback: Callable[[str, Dict], None]) -> None:
        """
        设置数据更新回调函数
        
        Args:
            callback: 板块信息更新回调 (sector_code, sector_info)
        """
        self.callback = callback
        logger.debug("回调函数已设置")

    def start(self, interval: int = 30) -> None:
        """
        启动监控器
        
        Args:
            interval: 更新间隔（秒）
        """
        if self.is_running:
            logger.warning("监控器已在运行中")
            return

        self.interval = interval
        self.is_running = True

        # 创建API实例
        self.api = TonghuashunAPI(headless=self.headless,
                                  edge_driver_path=self.edge_driver_path)

        # 创建监控线程
        self.thread = threading.Thread(target=self._run,
                                       name="TonghuashunMonitorThread",
                                       daemon=True)
        self.thread.start()

        logger.info(f"监控器已启动，更新间隔: {interval}秒")

    def stop(self) -> None:
        """停止监控器"""
        if not self.is_running:
            logger.info("监控器未在运行")
            return

        self.is_running = False

        # 等待线程结束
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                logger.warning("监控线程未能在10秒内结束")

        # 关闭API
        if self.api:
            self.api.close()
            self.api = None

        self.thread = None
        logger.info("监控器已停止")

    def _run(self) -> None:
        """监控主循环"""
        logger.info("监控循环已开始")

        while self.is_running:
            loop_start = time.time()

            try:
                # 遍历所有板块
                for sector_code in self.sector_codes:
                    if not self.is_running:
                        break

                    try:
                        # 获取板块信息
                        sector_info = self.api.get_sector_info(sector_code)

                        # 调用回调函数
                        if sector_info and self.callback:
                            self.callback(sector_code, sector_info)

                    except Exception as e:
                        logger.error(f"获取板块 {sector_code} 数据时出错: {e}")

                    # 板块之间短暂延迟，避免请求过快
                    if self.is_running and len(self.sector_codes) > 1:
                        time.sleep(1)

                # 计算本次循环耗时
                loop_duration = time.time() - loop_start

                # 计算需要等待的时间
                wait_time = max(0, self.interval - loop_duration)

                if wait_time > 0 and self.is_running:
                    logger.debug(f"等待 {wait_time:.1f} 秒后进行下次更新")
                    # 使用小间隔检查，以便快速响应停止信号
                    for _ in range(int(wait_time * 10)):
                        if not self.is_running:
                            break
                        time.sleep(0.1)

            except KeyboardInterrupt:
                logger.info("监控器收到中断信号")
                break
            except Exception as e:
                logger.error(f"监控循环出错: {e}", exc_info=True)
                # 出错后等待一段时间再继续
                if self.is_running:
                    time.sleep(min(self.interval, 30))

        logger.info("监控循环已结束")

    def __enter__(self):
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.stop()
        return False


def main():
    """主函数，演示监控器的使用"""
    print("同花顺板块监控器 - 使用示例")
    print("=" * 60)

    # 要监控的板块代码
    sector_codes = ["883993", "881101"]  # 可以监控多个板块

    # 定义回调函数
    def on_sector_update(sector_code: str, sector_info: Dict):
        """板块信息更新回调"""
        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] 板块 {sector_code} - {sector_info.get('sector_name', '')}"
        )
        print(f"  当前价格: {sector_info.get('current_price', 0):.2f}")
        print(f"  涨跌幅: {sector_info.get('price_change', 0):+.2f}%")
        print(f"  成交额: {sector_info.get('turnover', 0):.2f}亿")
        print(
            f"  涨跌家数: ↑{sector_info.get('rise_count', 0)} ↓{sector_info.get('fall_count', 0)}"
        )
        print(f"  资金净流入: {sector_info.get('net_inflow', 0):+.2f}亿")

    # 创建监控器
    with TonghuashunMonitor(
            sector_codes=sector_codes,
            headless=True  # 使用无头模式
    ) as monitor:

        # 设置回调函数
        monitor.set_callback(on_sector_update)

        # 启动监控
        monitor.start(interval=10)  # 每10秒更新一次

        print(f"开始监控板块: {sector_codes}")
        print("按 Ctrl+C 停止监控...\n")

        try:
            # 保持运行
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在停止监控...")

    print("监控已结束")


if __name__ == "__main__":
    main()
