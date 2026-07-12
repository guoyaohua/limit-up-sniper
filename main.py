"""
main.py - 打板策略入口

启动策略主函数，初始化所有模块并开始交易。
"""

import sys
import os
import time
import traceback
from datetime import datetime
from multiprocessing import Queue
from loguru import logger

# 确保项目根目录和依赖在 sys.path 中（进程 fork 前设置）
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
# 添加 deps 目录
DEPS_DIR = os.path.join(ROOT_DIR, 'deps', 'ai_hotspot_trader')
if DEPS_DIR not in sys.path:
    sys.path.insert(0, DEPS_DIR)

from config import (
    VERSION, DEBUG_MODE, IS_LIVE_TRADING, ENABLE_SHADOW_SIGNAL,
    ENABLE_PRE_MARKET_LLM_ANALYSIS, IP, PORT, STOP_TIME, TODAY,
    STRATEGY_NAME, MONITOR_LOG_PATH, SECTOR_DATA_SOURCE,
    AUTO_REFRESH_THS_SECTOR_MAPPING, IWENCAI_SECTOR_URL,
    IWENCAI_DOWNLOAD_DIR, IWENCAI_BROWSER_USER_DATA_DIR,
    IWENCAI_HEADLESS,
    IWENCAI_PAGE_SIZE, IWENCAI_MAX_PAGES,
    CLIENT_NAME, CLIENT_PATH, STOCK_ACCOUNT,
    TICK_PROCESSOR_COUNT, SHADOW_TICK_PROCESSOR_COUNT,
)
from infra.common_enums import *
from infra.utils import send_email, init_logger
from infra.data_helpers import xtdata_connect, get_pretrade_date
from infra.trade_log import save_daily_limit_up_list
from infra.task_manager import TaskManager, TaskInfo, get_task_manager
from data.shared_data import init_shared_data
from core.stock_pool import init_stock_pool
from core.gene_calculator import get_strong_stocks
from engine.tick_processor import process_tick_data, create_whole_quote_task
from engine.trader import run_xt_trader_task
from engine.simulator import run_xt_trader_simulator
from monitor.sector_monitor import sector_and_capitalflow_monitor_task
from monitor.sentiment_task import market_sentiment_monitor_task_manual


# 全局回调心跳监控器（用于监控 xtdata 回调是否正常）
_callback_heartbeat_monitor = None


def get_callback_heartbeat_monitor(timeout: float = 30):
    """获取全局回调心跳监控器实例"""
    from infra.task_manager import CallbackHeartbeatMonitor
    global _callback_heartbeat_monitor
    if _callback_heartbeat_monitor is None:
        _callback_heartbeat_monitor = CallbackHeartbeatMonitor(
            name="xtdata_whole_quote_callback", timeout=timeout)
    return _callback_heartbeat_monitor


def refresh_sector_mapping_if_needed():
    """按配置刷新 THS 问财行业/概念映射。"""
    if SECTOR_DATA_SOURCE != 'THS' or not AUTO_REFRESH_THS_SECTOR_MAPPING:
        return

    try:
        from scraper.ths_sector_parser import (THSSectorParser,
                                               refresh_ths_sector_mappings)

        logger.info('[板块映射] 开始刷新 THS 问财行业/概念映射...')
        refreshed = refresh_ths_sector_mappings(
            url=IWENCAI_SECTOR_URL,
            download_dir=IWENCAI_DOWNLOAD_DIR,
            user_data_dir=IWENCAI_BROWSER_USER_DATA_DIR,
            headless=IWENCAI_HEADLESS,
            fallback_to_latest=True,
            per_page=IWENCAI_PAGE_SIZE,
            max_pages=IWENCAI_MAX_PAGES)
        if refreshed:
            logger.info('[板块映射] THS 问财行业/概念映射刷新完成')
        elif THSSectorParser.output_files_exist():
            logger.warning('[板块映射] 自动刷新失败，继续使用现有 THS 映射文件')
        else:
            raise RuntimeError('THS 问财映射刷新失败，且本地无可用映射文件')
    except Exception as exc:
        logger.exception(f'[板块映射] 刷新 THS 问财映射失败: {exc}')
        send_email('【关键错误】THS问财板块映射刷新失败',
                   f'THS 问财板块映射刷新失败: {exc}\n{traceback.format_exc()}')
        raise


def main():
    """
    打板策略主函数 - 首板涨停策略

    核心流程：
        1. 初始化阶段：加载股票池、计算涨停基因、加载板块映射
        2. 实时监控阶段：订阅全市场tick、监控板块资金流、计算市场情绪
        3. 交易执行阶段：排板买入、扫板买入、止损卖出、动态撤单
    """
    # 初始化日志
    init_logger(STRATEGY_NAME, MONITOR_LOG_PATH)

    missing_client_config = [
        name for name, value in {
            'CLIENT_PATH': CLIENT_PATH,
            'STOCK_ACCOUNT': STOCK_ACCOUNT,
        }.items() if not value
    ]
    if missing_client_config:
        env_prefix = 'CICC' if CLIENT_NAME == 'CICC_LIVE' else 'GJ_SIM'
        missing = ', '.join(missing_client_config)
        raise RuntimeError(
            f'交易客户端配置缺失：{missing}。请设置环境变量 '
            f'{env_prefix}_QMT_CLIENT_PATH 和 {env_prefix}_STOCK_ACCOUNT。'
        )

    PRE_TRADE_DATE = get_pretrade_date(TODAY)
    try:
        from xtquant import xtdata

        # 显示当前交易客户端信息
        mode_label = '实盘' if IS_LIVE_TRADING else '模拟'
        print('=' * 50)
        print(f'  交易客户端 : {CLIENT_NAME}')
        print('  客户端路径 : 已配置（已隐藏）')
        masked_account = (
            f'{STOCK_ACCOUNT[:2]}***{STOCK_ACCOUNT[-2:]}'
            if len(STOCK_ACCOUNT) >= 5 else '***'
        )
        print(f'  资金账号   : {masked_account}')
        print(f'  交易模式   : {mode_label}')
        print(f'  策略版本   : {VERSION}')
        print('=' * 50)

        # 实盘交易
        if IS_LIVE_TRADING:
            if input('注意这是实盘：输入yes继续\n') != 'yes':
                exit()

        xtdata_connect(IP, PORT)
        logger.info("连接到XTQuant数据服务成功")

        # 初始化股票列表
        stock_pool, stock_info_dict, new_stock_list = init_stock_pool(
            PRE_TRADE_DATE)
        logger.info(
            f"初始股票池包含 {len(stock_pool)} 只股票，新股 {len(new_stock_list)} 只")
        strong_stocks = get_strong_stocks(stock_pool, stock_info_dict,
                                          PRE_TRADE_DATE)
        logger.info(f"强势股票池包含 {len(strong_stocks)} 只股票")
    except Exception as e:
        logger.exception(f"【关键错误】主函数初始化阶段失败: {e}")
        send_email('【关键错误】策略初始化失败',
                   f'策略初始化时发生异常: {e}\n{traceback.format_exc()}')
        raise e

    # 数据队列
    # 同一股票固定路由到同一 FIFO 队列，避免多个消费者把连续 Tick
    # 乱序写入共享状态；不同股票仍可并行处理。
    tick_queue = [
        Queue(maxsize=10000 // TICK_PROCESSOR_COUNT)
        for _ in range(TICK_PROCESSOR_COUNT)
    ]
    order_queue = Queue(maxsize=100)
    paper_market_queue = Queue(maxsize=64) if not IS_LIVE_TRADING else None

    # 影子模式
    shadow_tick_queue = ([
        Queue(maxsize=10000 // SHADOW_TICK_PROCESSOR_COUNT)
        for _ in range(SHADOW_TICK_PROCESSOR_COUNT)
    ] if ENABLE_SHADOW_SIGNAL else None)
    shadow_order_queue = Queue(maxsize=100) if ENABLE_SHADOW_SIGNAL else None
    shadow_market_queue = Queue(maxsize=64) if ENABLE_SHADOW_SIGNAL else None

    # 初始化 TaskManager
    task_manager = get_task_manager(stop_time=STOP_TIME)

    refresh_sector_mapping_if_needed()

    shared_data = init_shared_data(stock_pool,
                                   stock_info_dict,
                                   strong_stocks,
                                   PRE_TRADE_DATE,
                                   new_stock_list=new_stock_list)

    # U7升级：盘前 LLM 板块预判
    sector_priority = {}
    exploration_candidates = []
    if ENABLE_PRE_MARKET_LLM_ANALYSIS:
        try:
            from analysis.pre_market_analysis import (
                get_exploration_candidate_codes, run_pre_market_analysis,
            )
            sector_priority = run_pre_market_analysis()
            exploration_candidates = get_exploration_candidate_codes(
                sector_priority, stock_pool)
            priority_dict = shared_data['板块优先级']
            for sector, weight in sector_priority.get('priority_sectors', {}).items():
                priority_dict[sector] = str(weight)
            logger.info(
                f'[盘前分析] 市场展望: {sector_priority.get("market_outlook", "未知")}，'
                f'优先板块: {list(sector_priority.get("priority_sectors", {}).keys())}，'
                f'回避板块: {sector_priority.get("avoid_sectors", [])}，'
                f'影子探索候选: {len(exploration_candidates)}只'
            )
            if not IS_LIVE_TRADING and exploration_candidates:
                # Simulation may measure the broader discovery layer directly;
                # every candidate still passes all real-time decision filters.
                expanded = list(dict.fromkeys(
                    list(shared_data['强势股票']) + exploration_candidates))
                shared_data['强势股票'] = expanded
        except Exception as e:
            logger.warning(f'[盘前分析] 失败，策略正常运行: {e}')

    # 注册 Tick 数据处理进程
    logger.info(f'注册 {TICK_PROCESSOR_COUNT} 个Tick数据处理进程...')
    for idx in range(TICK_PROCESSOR_COUNT):
        task_manager.register_task(
            TaskInfo(name=f'Tick数据处理进程-{idx}',
                     target=process_tick_data,
                     args=(shared_data, tick_queue[idx], order_queue),
                     task_type="process",
                     daemon=True,
                     restart_on_failure=True,
                     max_restart_count=5,
                     heartbeat_timeout=60))

    # 注册交易模块
    if IS_LIVE_TRADING:
        task_manager.register_task(
            TaskInfo(
                name='交易模块',
                target=run_xt_trader_task,
                args=(order_queue, shared_data),
                task_type="thread",
                daemon=True,
                restart_on_failure=False
            ))
        logger.info('注册交易模块（实盘）')
    else:
        task_manager.register_task(
            TaskInfo(name='交易模块',
                     target=run_xt_trader_simulator,
                     args=(order_queue, shared_data, False, paper_market_queue),
                     task_type="thread",
                     daemon=True,
                     restart_on_failure=False))
        logger.info('注册交易模块（模拟）')

    # 影子模式
    if ENABLE_SHADOW_SIGNAL:
        logger.info('[影子模式] 开始初始化（进程数优化：4个）...')

        shadow_strong_stocks = list(dict.fromkeys(
            list(strong_stocks) + exploration_candidates))
        shadow_shared_data = init_shared_data(stock_pool,
                                              stock_info_dict,
                                              shadow_strong_stocks,
                                              PRE_TRADE_DATE,
                                              shadow_signal_mode=True,
                                              base_shared_data=shared_data,
                                              new_stock_list=new_stock_list)

        shadow_process_count = SHADOW_TICK_PROCESSOR_COUNT
        logger.info(f'[影子模式] 注册 {shadow_process_count} 个Tick数据处理进程...')
        for idx in range(shadow_process_count):
            task_manager.register_task(
                TaskInfo(name=f'[影子模式] Tick数据处理进程-{idx}',
                         target=process_tick_data,
                         args=(shadow_shared_data, shadow_tick_queue[idx],
                               shadow_order_queue, True),
                         task_type="process",
                         daemon=True,
                         restart_on_failure=True,
                         max_restart_count=5,
                         heartbeat_timeout=60))

        task_manager.register_task(
            TaskInfo(name='[影子模式] 交易模块',
                     target=run_xt_trader_simulator,
                     args=(shadow_order_queue, shadow_shared_data, True,
                           shadow_market_queue),
                     task_type="thread",
                     daemon=True,
                     restart_on_failure=False))

        logger.info('[影子模式] 初始化完成')

    # 东方财富数据监控
    task_manager.register_task(
        TaskInfo(name='板块和个股资金流数据监控',
                 target=sector_and_capitalflow_monitor_task,
                 args=(shared_data, ),
                 task_type="process",
                 daemon=True,
                 restart_on_failure=True,
                 max_restart_count=3,
                 heartbeat_timeout=120))

    # 市场情绪监控
    task_manager.register_task(
        TaskInfo(name='市场情绪监控',
                 target=market_sentiment_monitor_task_manual,
                 args=(shared_data, ),
                 task_type="process",
                 daemon=True,
                 restart_on_failure=True,
                 max_restart_count=3,
                 heartbeat_timeout=120))

    # 启动所有任务
    task_manager.start_all()
    logger.info('TaskManager 已启动所有任务')

    # 订阅全推行情
    try:
        while True:
            create_whole_quote_task(
                stock_pool, stock_info_dict, tick_queue, shadow_tick_queue,
                paper_market_queue, shadow_market_queue)
            if datetime.now().time() >= STOP_TIME:
                logger.warning('程序已到达停止时间，退出全市场行情订阅')
                break
            else:
                logger.warning(f'【重新订阅】全市场行情...')
    except KeyboardInterrupt:
        logger.warning('收到 Ctrl+C 中断信号，正在退出...')
    except Exception as e:
        logger.exception(f'主循环异常退出: {e}')

    # 保存当日涨停列表（必须在 shutdown 之前）
    logger.info("开始保存当日涨停列表...")
    try:
        save_daily_limit_up_list(shared_data)
    except Exception as save_error:
        logger.error(f'保存当日涨停列表失败: {save_error}')

    # 清理
    logger.info('开始执行清理操作...')
    try:
        task_manager.shutdown()
        logger.info('TaskManager 已关闭所有任务')
    except Exception as cleanup_error:
        logger.error(f'TaskManager 关闭时发生错误: {cleanup_error}')

    # 复盘模块
    import subprocess

    logger.info('开始运行复盘模块...')
    result = subprocess.run([
        'python', './analysis/post_market_review.py', '--date', f'{TODAY}',
        '--strategy-version', f'{VERSION}'
    ],
                            capture_output=True,
                            text=True)

    if result.stdout:
        logger.info(f'[复盘模块-stdout]\n{result.stdout}')
    if result.stderr:
        logger.warning(f'[复盘模块-stderr]\n{result.stderr}')
    if result.returncode != 0:
        logger.error(f'[复盘模块] 执行失败，返回码: {result.returncode}')
    else:
        logger.info('[复盘模块] 执行完成')


if __name__ == '__main__':
    main()
