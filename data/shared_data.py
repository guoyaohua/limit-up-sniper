import time
import traceback
import concurrent.futures
from multiprocessing import Value, Array, Manager
from loguru import logger

from config import (VERSION, DEBUG_MODE, STRATEGY_NAME,
                    STOCK_TO_CONCEPT_MAPPING_FILE, STOCK_TO_INDUSTRY_MAPPING_FILE)
from scraper.em_scraper_api import SectorType
from infra.utils import send_email
from data.helpers import transform_dict_mapping
from data.serialization import (load_shared_data, _batch_create_stock_signals,
                                start_shared_data_backup_task)
from data.sector_mapping import (load_sector_mapping,
                                 load_yesterday_first_limit_up_stock_list,
                                 load_yesterday_limit_up_stock_list)


def print_data_summary(data):
    """
    打印数据摘要信息
    """
    logger.info("\n" + "=" * 50)
    logger.info("数据摘要:")
    logger.info("=" * 50)

    for key, value in data.items():
        try:
            if isinstance(value, (list, dict)):
                logger.info(f"{key}: {type(value).__name__} 长度={len(value)}")
            else:
                logger.info(f"{key}: {type(value).__name__} = {value}")
        except Exception:
            logger.info(f"{key}: {type(value).__name__}")

    logger.info("=" * 50)


def setup_shared_data_config(shared_data):
    """设置共享数据中的配置信息"""
    shared_data['VERSION'] = VERSION
    shared_data['DEBUG_MODE'] = DEBUG_MODE
    shared_data['STRATEGY_NAME'] = STRATEGY_NAME


def init_shared_data(stock_pool,
                     stock_info_dict,
                     strong_stocks,
                     pre_trade_date,
                     shadow_signal_mode=False,
                     base_shared_data=None,
                     new_stock_list=None):

    # ---------------------------------------------------------------------------- #
    #                                      实盘                                     #
    # ---------------------------------------------------------------------------- #
    if not shadow_signal_mode:
        # ----------------------------------- 共享数据初始化 ----------------------------------- #
        try:
            # 尝试从备份文件恢复shared_data
            logger.info("尝试从备份文件恢复shared_data...")
            restored_shared_data = load_shared_data()

            if restored_shared_data:
                logger.info("成功从备份文件恢复shared_data")
                shared_data = restored_shared_data
                # 实时派生数据不能跨进程重启直接复用。监控任务会重新填充
                # 内容和时间戳；在此之前买入逻辑保持 fail-closed。
                for realtime_key in (
                        '概念板块效应', '行业板块效应', '个股资金流入'):
                    realtime_data = shared_data.get(realtime_key)
                    if realtime_data is not None:
                        realtime_data.clear()
                for timestamp_key in (
                        '概念板块更新时间', '行业板块更新时间',
                        '个股资金流入更新时间'):
                    timestamp_obj = shared_data.get(timestamp_key)
                    if timestamp_obj is not None and hasattr(timestamp_obj,
                                                              'get_lock'):
                        with timestamp_obj.get_lock():
                            timestamp_obj.value = 0.0
                print_data_summary(shared_data)
            else:
                logger.info("未找到备份文件，创建新的shared_data")

                # ----------------------------------- 共享变量 ----------------------------------- #
                # 信号共享字典 - v2.4.1 优化：使用批量并行创建
                logger.info(f"开始批量创建股票信号对象（{len(stock_pool)} 只股票）...")
                stock_signals = _batch_create_stock_signals(stock_pool)
                logger.info(f"股票信号对象创建完成")

                # ----------------------------------- 板块信息 ----------------------------------- #
                # 股票行业板块映射
                industry_sector_dict = {}
                # 股票概念板块映射
                concept_sector_dict = {}

                # 加载概念板块和行业板块映射（排除新股）
                concept_sector_dict = load_sector_mapping(
                    SectorType.CONCEPT,
                    STOCK_TO_CONCEPT_MAPPING_FILE,
                    exclude_stocks=new_stock_list)

                industry_sector_dict = load_sector_mapping(
                    SectorType.INDUSTRY,
                    STOCK_TO_INDUSTRY_MAPPING_FILE,
                    exclude_stocks=new_stock_list)

                # 昨日首次涨停
                yesterday_first_limit_up_stocks = load_yesterday_first_limit_up_stock_list(
                    pre_trade_date, stock_pool)
                # 昨日涨停
                yesterday_limit_up_stocks = load_yesterday_limit_up_stock_list(
                    pre_trade_date, stock_pool)

                # v2.4.1 优化：并行创建 Manager 代理对象
                # 每个 Manager() 独立创建以减少运行时锁争夺，但串行创建太慢（约2分钟）
                # 使用线程池并行创建，预期从2分钟优化到10-20秒
                logger.info("开始并行创建 Manager 代理对象...")
                mgr_start = time.time()

                # 定义需要创建的 Manager 代理及其类型
                manager_proxy_specs = [
                    ('持仓状态', 'dict'),
                    ('委托状态', 'dict'),
                    ('盘前持仓', 'list'),
                    ('概念板块效应', 'dict'),
                    ('行业板块效应', 'dict'),
                    ('个股资金流入', 'dict'),
                    ('个股资金流入_原始数据', 'list'),
                    ('涨停池', 'dict'),
                    ('炸板池', 'dict'),
                    ('最大开板回封时间', 'dict'),
                    ('开板次数', 'dict'),
                    ('黑名单', 'dict'),
                    ('观察名单', 'dict'),
                    ('观察名单元数据', 'dict'),
                    ('炸板episode状态', 'dict'),
                    ('盘中特征快照', 'dict'),
                    ('决策原因标签', 'dict'),
                    ('复盘统计计数器', 'dict'),
                    ('盘中事件流', 'list'),
                    ('板块优先级', 'dict'),
                ]

                def create_manager_proxy(spec):
                    """创建单个 Manager 代理"""
                    name, proxy_type = spec
                    mgr = Manager()
                    if proxy_type == 'dict':
                        return (name, mgr.dict())
                    else:
                        return (name, mgr.list())

                # 并行创建 Manager 代理
                manager_proxies = {}
                with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
                    futures = {executor.submit(create_manager_proxy, spec): spec[0]
                               for spec in manager_proxy_specs}
                    for future in concurrent.futures.as_completed(futures):
                        name, proxy = future.result()
                        manager_proxies[name] = proxy

                logger.info(f"Manager 代理对象创建完成，耗时 {time.time() - mgr_start:.2f}s")

                # 创建共享数据字典
                shared_data = {
                    '信号来源': 'primary',
                    '股票信息': stock_info_dict,  # 股票信息字典
                    '持仓状态': manager_proxies['持仓状态'],  # 股票代码 -> 持仓状态字典JSON string
                    '委托状态': manager_proxies['委托状态'],  # 委托状态(可撤委托)，股票代码 -> 委托状态
                    '盘前持仓': manager_proxies['盘前持仓'],  # 盘前持仓列表 TODO: 检查一下这里的逻辑
                    '概念板块': concept_sector_dict,  # 股票->概念板块
                    '行业板块': industry_sector_dict,  # 股票->行业板块
                    '概念板块成分股': transform_dict_mapping(
                        concept_sector_dict),  # 概念板块成分股，板块代码 -> 成分股列表
                    '行业板块成分股':
                    transform_dict_mapping(industry_sector_dict),  # 行业板块成分股
                    '昨日首板股票': yesterday_first_limit_up_stocks,  # 昨日首板股票列表
                    '昨日涨停股票': yesterday_limit_up_stocks,  # 昨日涨停股票列表
                    '概念板块效应': manager_proxies['概念板块效应'],  # 有概念板块联动的股票，value为概念板块详情
                    '概念板块更新时间': Value('d', 0.0),  # 概念板块数据最后更新时间（使用long存储时间戳）
                    '行业板块效应': manager_proxies['行业板块效应'],  # 有行业板块联动的股票，value为行业板块详情
                    '行业板块更新时间': Value('d', 0.0),  # 行业板块数据最后更新时间（使用long存储时间戳）
                    '个股资金流入': manager_proxies['个股资金流入'],  # 资金流入满足的股票，value为资金流详情
                    '个股资金流入_原始数据': manager_proxies['个股资金流入_原始数据'],  # 个股资金流入原始数据（用于报告显示TOP20）
                    '个股资金流入更新时间': Value('d',
                                        0.0),  # 个股资金流入数据最后更新时间（使用long存储时间戳）
                    '涨停池':
                    manager_proxies['涨停池'],  # key 为股票代码，value为一个string，涨停/回封时间戳用逗号分隔
                    '炸板池':
                    manager_proxies['炸板池'],  # key 为股票代码，value为一个string，炸板时间戳用逗号分隔
                    '最大开板回封时间':
                    manager_proxies['最大开板回封时间'],  # 单位为秒，key为股票代码，value为最大开板回封时间
                    '开板次数': manager_proxies['开板次数'],  # key为股票代码，value为开板次数
                    '股票状态信号': stock_signals,  # 股票代码 -> 信号字典
                    '市场情绪_涨停板数量': Value('i', 0),  # 全市场涨停板数量
                    '市场情绪_炸板数量': Value('i', 0),  # 全市场炸板数量
                    '市场情绪_炸板率': Value('d', 0.0),  # 全市场炸板率
                    '市场情绪_昨日首板连板率': Value('d', 0.0),  # 昨日首版连板率
                    '市场情绪_昨日首板连板个数': Value('i', 0),  # 昨日首版连板个数
                    '市场情绪_昨日涨停连板率': Value('d', 0.0),  # 昨日涨停连板率
                    '市场情绪_昨日涨停连板个数': Value('i', 0),  # 昨日涨停连板个数
                    '市场情绪_昨日首板表现': Value('d', 0.0),  # 昨日首板表现
                    '市场情绪_昨日涨停表现': Value('d', 0.0),  # 昨日涨停表现
                    '上证指数涨跌幅': Value('d', 0.0),  # 上证指数涨跌幅
                    '沪深300涨跌幅': Value('d', 0.0),  # 沪深300涨跌幅
                    '创业板指涨跌幅': Value('d', 0.0),  # 创业板指涨跌幅
                    '深证成指涨跌幅': Value('d', 0.0),  # 深证成指涨跌幅
                    '大盘指数更新时间': Value('d', 0.0),  # 大盘指数数据最后更新时间（使用double存储时间戳）
                    '昨日涨停表现更新时间': Value('d',
                                        0.0),  # 昨日涨停表现数据最后更新时间（使用double存储时间戳）
                    '市场情绪_评分': Value('d', 0),  # 市场情绪评分
                    '黑名单': manager_proxies['黑名单'],  # 股票代码 -> 黑名单原因
                    '观察名单': manager_proxies['观察名单'],  # 股票代码 -> 观察名单信息
                    '观察名单元数据': manager_proxies['观察名单元数据'],  # 股票代码 -> 观察名单结构化元数据
                    '炸板episode状态': manager_proxies['炸板episode状态'],  # 股票代码 -> 炸板episode结构化状态
                    '盘中特征快照': manager_proxies['盘中特征快照'],  # 股票代码 -> 最近一次结构化事件/特征快照
                    '决策原因标签': manager_proxies['决策原因标签'],  # 股票代码 -> 最近一次决策标签
                    '复盘统计计数器': manager_proxies['复盘统计计数器'],  # 统计计数器
                    '盘中事件流': manager_proxies['盘中事件流'],  # 盘中事件缓冲区
                    '板块优先级': manager_proxies['板块优先级'],  # LLM盘前板块预判结果
                    '强势股票': strong_stocks,  # 强势股票列表, 涨停基因好的股票
                    '撤单次数': Value('i', 0),  # 撤单次数
                }

            # 设置策略配置信息
            setup_shared_data_config(shared_data)

            # ---------------------------------- 启动备份任务 ---------------------------------- #
            logger.info("启动shared_data备份任务...")
            start_shared_data_backup_task(shared_data, backup_interval=2)

            return shared_data
        except Exception as e:
            logger.exception(f"【关键错误】共享数据初始化失败: {e}")
            send_email('【关键错误】共享数据初始化失败',
                       f'共享数据初始化时发生异常: {e}\n{traceback.format_exc()}')
            raise e

    # ---------------------------------------------------------------------------- #
    #                                     影子模式                                  #
    # ---------------------------------------------------------------------------- #
    else:
        # ----------------------------------- 共享数据初始化 ----------------------------------- #
        try:
            prefix = 'shadow_'
            # 尝试从备份文件恢复shared_data
            logger.info("[影子模式] 尝试从备份文件恢复shared_data...")
            restored_shared_data = load_shared_data(prefix=prefix)

            if restored_shared_data:
                logger.info("[影子模式] 成功从备份文件恢复shared_data")
                shared_data = restored_shared_data
                print_data_summary(shared_data)
            else:
                logger.info("[影子模式] 未找到备份文件，创建新的shared_data")

                if base_shared_data is None:
                    raise Exception(
                        '[影子模式] 影子模式 shared_data 需基于实盘 base_shared_data 创建')

                # ----------------------------------- 共享变量 ----------------------------------- #
                # 信号共享字典 - v2.4.1 优化：使用批量并行创建
                logger.info(f"[影子模式] 开始批量创建股票信号对象（{len(stock_pool)} 只股票）...")
                stock_signals = _batch_create_stock_signals(stock_pool)
                logger.info(f"[影子模式] 股票信号对象创建完成")

                # v2.4.1 优化：并行创建 Manager 代理对象
                logger.info("[影子模式] 开始并行创建 Manager 代理对象...")
                shadow_mgr_start = time.time()

                shadow_manager_proxy_specs = [
                    ('持仓状态', 'dict'),
                    ('委托状态', 'dict'),
                    ('涨停池', 'dict'),
                    ('炸板池', 'dict'),
                    ('最大开板回封时间', 'dict'),
                    ('开板次数', 'dict'),
                    ('黑名单', 'dict'),
                    ('观察名单', 'dict'),
                    ('观察名单元数据', 'dict'),
                    ('炸板episode状态', 'dict'),
                    ('盘中特征快照', 'dict'),
                    ('决策原因标签', 'dict'),
                    ('复盘统计计数器', 'dict'),
                    ('盘中事件流', 'list'),
                    ('板块优先级', 'dict'),
                ]

                def create_shadow_manager_proxy(spec):
                    """创建单个 Manager 代理"""
                    name, proxy_type = spec
                    mgr = Manager()
                    if proxy_type == 'dict':
                        return (name, mgr.dict())
                    else:
                        return (name, mgr.list())

                shadow_manager_proxies = {}
                with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
                    futures = {executor.submit(create_shadow_manager_proxy, spec): spec[0]
                               for spec in shadow_manager_proxy_specs}
                    for future in concurrent.futures.as_completed(futures):
                        name, proxy = future.result()
                        shadow_manager_proxies[name] = proxy

                logger.info(f"[影子模式] Manager 代理对象创建完成，耗时 {time.time() - shadow_mgr_start:.2f}s")

                shared_data = {
                    '信号来源': 'shadow',
                    '股票信息': stock_info_dict,  # 股票信息字典
                    '持仓状态': shadow_manager_proxies['持仓状态'],  # 股票代码 -> 持仓状态字典JSON string
                    '委托状态': shadow_manager_proxies['委托状态'],  # 委托状态(可撤委托)，股票代码 -> 委托状态
                    '盘前持仓': [],  # 盘前持仓列表 NOTE: 影子模式不需要
                    '概念板块': base_shared_data['概念板块'],  # 股票->概念板块
                    '行业板块': base_shared_data['行业板块'],  # 股票->行业板块
                    '概念板块成分股':
                    base_shared_data['概念板块成分股'],  # 概念板块成分股，板块代码 -> 成分股列表
                    '行业板块成分股': base_shared_data['行业板块成分股'],  # 行业板块成分股
                    '昨日首板股票': base_shared_data['昨日首板股票'],  # 昨日首板股票列表
                    '昨日涨停股票': base_shared_data['昨日涨停股票'],  # 昨日涨停股票列表
                    '概念板块效应':
                    base_shared_data['概念板块效应'],  # 有概念板块联动的股票，value为概念板块详情
                    '概念板块更新时间':
                    base_shared_data['概念板块更新时间'],
                    '行业板块效应':
                    base_shared_data['行业板块效应'],  # 有行业板块联动的股票，value为行业板块详情
                    '行业板块更新时间':
                    base_shared_data['行业板块更新时间'],
                    '个股资金流入':
                    base_shared_data['个股资金流入'],  # 资金流入满足的股票，value为资金流详情
                    '个股资金流入更新时间':
                    base_shared_data['个股资金流入更新时间'],
                    '涨停池':
                    shadow_manager_proxies['涨停池'],  # key 为股票代码，value为一个string，涨停/回封时间戳用逗号分隔
                    '炸板池':
                    shadow_manager_proxies['炸板池'],  # key 为股票代码，value为一个string，炸板时间戳用逗号分隔
                    '最大开板回封时间':
                    shadow_manager_proxies['最大开板回封时间'],  # 单位为秒，key为股票代码，value为最大开板回封时间
                    '开板次数': shadow_manager_proxies['开板次数'],  # key为股票代码，value为开板次数
                    '股票状态信号': stock_signals,  # 股票代码 -> 信号字典
                    '市场情绪_涨停板数量': base_shared_data['市场情绪_涨停板数量'],  # 全市场涨停板数量
                    '市场情绪_炸板数量': base_shared_data['市场情绪_炸板数量'],  # 全市场炸板数量
                    '市场情绪_炸板率': base_shared_data['市场情绪_炸板率'],  # 全市场炸板率
                    '市场情绪_昨日首板连板率':
                    base_shared_data['市场情绪_昨日首板连板率'],  # 昨日首版连板率
                    '市场情绪_昨日首板连板个数':
                    base_shared_data['市场情绪_昨日首板连板个数'],  # 昨日首版连板个数
                    '市场情绪_昨日涨停连板率':
                    base_shared_data['市场情绪_昨日涨停连板率'],  # 昨日涨停连板率
                    '市场情绪_昨日涨停连板个数':
                    base_shared_data['市场情绪_昨日涨停连板个数'],  # 昨日涨停连板个数
                    '市场情绪_昨日首板表现': base_shared_data['市场情绪_昨日首板表现'],  # 昨日首板表现
                    '市场情绪_昨日涨停表现': base_shared_data['市场情绪_昨日涨停表现'],  # 昨日涨停表现
                    '上证指数涨跌幅': base_shared_data['上证指数涨跌幅'],  # 上证指数涨跌幅
                    '沪深300涨跌幅': base_shared_data['沪深300涨跌幅'],  # 沪深300涨跌幅
                    '创业板指涨跌幅': base_shared_data['创业板指涨跌幅'],  # 创业板指涨跌幅
                    '深证成指涨跌幅': base_shared_data['深证成指涨跌幅'],  # 深证成指涨跌幅
                    '市场情绪_评分': base_shared_data['市场情绪_评分'],  # 市场情绪评分
                    '黑名单': shadow_manager_proxies['黑名单'],  # 股票代码 -> 黑名单原因
                    '观察名单': shadow_manager_proxies['观察名单'],  # 股票代码 -> 观察名单信息
                    '观察名单元数据': shadow_manager_proxies['观察名单元数据'],  # 股票代码 -> 观察名单结构化元数据
                    '炸板episode状态': shadow_manager_proxies['炸板episode状态'],  # 股票代码 -> 炸板episode结构化状态
                    '盘中特征快照': shadow_manager_proxies['盘中特征快照'],  # 股票代码 -> 最近一次结构化事件/特征快照
                    '决策原因标签': shadow_manager_proxies['决策原因标签'],  # 股票代码 -> 最近一次决策标签
                    '复盘统计计数器': shadow_manager_proxies['复盘统计计数器'],  # 统计计数器
                    '盘中事件流': shadow_manager_proxies['盘中事件流'],  # 盘中事件缓冲区
                    '板块优先级': shadow_manager_proxies['板块优先级'],  # LLM盘前板块预判结果
                    '强势股票': strong_stocks,  # 强势股票列表, 涨停基因好的股票
                    '撤单次数': Value('i', 0),  # 撤单次数
                }

            # 设置策略配置信息
            # setup_shared_data_config(shared_data)

            # ---------------------------------- 启动备份任务 ---------------------------------- #
            logger.info("[影子模式] 启动shared_data备份任务...")
            start_shared_data_backup_task(shared_data,
                                          backup_interval=2,
                                          prefix=prefix)

            return shared_data
        except Exception as e:
            logger.exception(f"【关键错误】[影子模式] 共享数据初始化失败: {e}")
            send_email('【关键错误】[影子模式] 共享数据初始化失败',
                       f'[影子模式] 共享数据初始化时发生异常: {e}\n{traceback.format_exc()}')
            raise e
