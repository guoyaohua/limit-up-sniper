from xtquant import xtdata
import os
from datetime import datetime, time as dt_time
import time
from statistics import mean
from multiprocessing import Value, current_process, Process, Manager
import traceback
from queue import Empty, Full
from functools import partial
from threading import Thread
import sys
import pickle
import pandas as pd
import json
import threading
import schedule
from loguru import logger
from infra.utils import send_email, init_logger
'''保存沪深A股Tick数据到本地'''

# ================================== 全局配置 ================================== #
# 数据存储根目录 - 可配置
DATA_ROOT_DIR = os.getenv('TICK_DATA_DIR', os.path.join('output', 'tick_data'))
TODAY = datetime.now().strftime('%Y%m%d')
SAVE_FOLDER = os.path.join(DATA_ROOT_DIR, TODAY)
RAW_TICK_FILE = os.path.join(SAVE_FOLDER, f'raw_tick_data_{TODAY}.jsonl')
FEATHER_FILE = os.path.join(SAVE_FOLDER, f'tick_data_{TODAY}.feather')

# 时间配置
STOP_TIME = '15:05'  # 收盘后停止
MARKET_CLOSE_TIME = '15:00'  # 市场收盘时间
VERSION = 'A股Tick数据保存'

# 创建存储目录
os.makedirs(SAVE_FOLDER, exist_ok=True)

# ---------------------------------------------------------------------------- #

# 初始化日志记录器
init_logger(
    os.path.basename(__file__)[:-3],  # 使用脚本文件名作为日志名称
    log_dir=os.path.join(
        os.getenv('LIMIT_UP_LOG_DIR', os.path.join('logs', 'monitor')),
        f'{VERSION}_{TODAY}',
    ),
    verbose=True)  # 是否在控制台打印日志


def _calc_delay_time(time_stamp):
    """计算延迟时间"""
    return round(
        (time.time() - time.mktime(time.localtime(time_stamp / 1000))), 3)


def get_a_stock_candidates():
    """
    获取全部沪深A股列表
    :return: A股候选列表
    """
    try:
        # 下载板块分类信息
        logger.info('开始下载板块分类信息...')
        xtdata.download_sector_data()
        logger.info('下载完成')

        # 获取全部A股
        stock_pool = xtdata.get_stock_list_in_sector('沪深A股')  # 全部A股

        # 去掉科创板
        stock_pool = [
            stock for stock in stock_pool if not stock.startswith('68')
        ]
        # 去掉北京交易所
        stock_pool = [
            stock for stock in stock_pool if not stock.endswith('.BJ')
        ]
        # 去重并过滤
        stock_pool = list(set(stock_pool))

        # ------------------------------- 去掉当日停牌和ST的股票 ------------------------------- #
        invalid_stock_list = []
        for stock_code in stock_pool:
            try:
                stock_info = xtdata.get_instrument_detail(stock_code,
                                                          iscomplete=False)
                if stock_info['InstrumentStatus'] > 0:
                    '''停牌标记
                    0 - 正常
                    1 - 停牌
                    -1 - 当日起复牌
                    >1 - 停牌N天
                    '''
                    logger.debug(
                        f'{stock_code} 停牌 {stock_info["InstrumentStatus"]} {stock_info["InstrumentName"]}'
                    )
                    # 停牌
                    invalid_stock_list.append(stock_code)
                    continue
                if 'st' in stock_info['InstrumentName'].lower():
                    # ST股
                    invalid_stock_list.append(stock_code)
                    continue
                if stock_info['OpenDate'] == '19700101' or stock_info[
                        'OpenDate'] >= TODAY:
                    # 未上市/新上市
                    logger.debug(
                        f' 【未上市/新上市】 {stock_code} {stock_info["InstrumentName"]}'
                    )
                    invalid_stock_list.append(stock_code)
                    continue
                # 过滤掉上市时间小于100天的股票
                if (datetime.strptime(TODAY, '%Y%m%d') - datetime.strptime(
                        stock_info['OpenDate'], '%Y%m%d')).days < 100:
                    logger.debug(
                        f'【上市时间小于100天】 {stock_code} {stock_info["InstrumentName"]}'
                    )
                    invalid_stock_list.append(stock_code)
                    continue

                # 去掉股价小于2的股票
                if stock_info['PreClose'] < 2:
                    logger.debug(
                        f'【股价小于2】 {stock_code} {stock_info["InstrumentName"]}')
                    invalid_stock_list.append(stock_code)
                    continue
            except Exception as e:
                logger.exception(f'处理股票 {stock_code} 信息失败: {e}')
                # 将出错的股票加入无效列表，继续处理其他股票
                invalid_stock_list.append(stock_code)
                continue
        stock_pool = [
            stock for stock in stock_pool if stock not in invalid_stock_list
        ]

        logger.info(f'日期：{TODAY}，股票池大小：{len(stock_pool)}')

        return stock_pool

    except Exception as e:
        logger.error(f'获取A股列表失败：{e}\n{traceback.format_exc()}')
        return []


def on_tick_data(datas, tick_queue):
    """分笔行情回调函数

    tick - 分笔数据
        'time'                  #时间戳
        'lastPrice'             #最新价
        'open'                  #开盘价
        'high'                  #最高价
        'low'                   #最低价
        'lastClose'             #前收盘价
        'amount'                #成交总额
        'volume'                #成交总量
        'pvolume'               #原始成交总量
        'stockStatus'           #证券状态
        'openInt'               #持仓量
        'lastSettlementPrice'   #前结算
        'askPrice'              #委卖价
        'bidPrice'              #委买价
        'askVol'                #委卖量
        'bidVol'                #委买量
        'transactionNum'		#成交笔数
    """
    try:
        if datas:
            latency = _calc_delay_time(
                mean([v['time'] for v in datas.values()]))
            queue_size = tick_queue.qsize()
            msg = f'【回调】【Tick】延迟：{latency}s，数据大小：{len(datas)}，队列大小：{queue_size}'

            # 减少日志输出频率，只在延迟较高或队列积压时输出
            if latency > 5 or queue_size > 1000:
                logger.warning(msg)
            elif len(datas) % 1000 == 0:  # 每1000条数据输出一次debug信息
                logger.debug(msg)

            # 延迟过高的数据
            if latency > 30:  # 30秒延迟阈值
                logger.warning(f'【丢弃数据】延迟过高：{latency}s，数据大小：{len(datas)}')

            # 数据入队
            tick_queue.put(datas, timeout=1)

    except Exception as e:
        logger.error(
            f'Tick数据入队错误：{e}, 数据大小：{len(datas) if datas else 0}\t{traceback.format_exc()}'
        )


def process_tick_data(tick_queue, file_lock):
    """
    全推行情数据处理函数，流式保存到单个文件
    """
    logger.info('开启tick数据流式保存进程...')
    processed_count = 0

    while True:
        try:
            datas = tick_queue.get(timeout=1)
            timestamp_now = datetime.now()

            # 准备数据行
            data_rows = []
            for stock_code, tick_data in datas.items():
                # 将tick数据转换为扁平结构
                row = {
                    'timestamp': timestamp_now.isoformat(),
                    'stock_code': stock_code,
                    'receive_time': timestamp_now.timestamp(),
                    **tick_data  # 展开所有tick字段
                }
                data_rows.append(row)

            # 流式写入文件（使用jsonl格式，每行一个json）
            with file_lock:
                with open(RAW_TICK_FILE, 'a', encoding='utf-8') as f:
                    for row in data_rows:
                        f.write(json.dumps(row, ensure_ascii=False) + '\n')

            processed_count += len(data_rows)

            # 每处理1000条数据输出一次统计
            if processed_count % 1000 == 0:
                logger.info(f'已处理tick数据：{processed_count}条')

        except Empty:
            time.sleep(0.1)
            current_time = datetime.now().strftime('%H:%M')
            if current_time >= STOP_TIME:
                logger.warning(
                    f'【进程退出】{current_process().name}，总处理：{processed_count}条')
                return

        except Exception as e:
            logger.error(f'处理tick数据失败：{e}\n{traceback.format_exc()}')


def convert_to_feather():
    """
    收盘后将JSONL文件转换为Pandas DataFrame并保存为feather格式
    """
    try:
        logger.info('开始转换tick数据为feather格式...')

        if not os.path.exists(RAW_TICK_FILE):
            logger.warning(f'原始数据文件不存在：{RAW_TICK_FILE}')
            return

        # 读取JSONL文件
        data_list = []
        with open(RAW_TICK_FILE, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                try:
                    if line.strip():
                        data = json.loads(line.strip())
                        data_list.append(data)

                        # 每读取10000行输出一次进度
                        if line_num % 10000 == 0:
                            logger.info(f'已读取{line_num}行数据')

                except json.JSONDecodeError as e:
                    logger.warning(f'第{line_num}行JSON解析失败：{e}')
                    continue

        if not data_list:
            logger.warning('没有有效的tick数据可转换')
            return

        # 转换为DataFrame
        logger.info(f'开始转换为DataFrame，总数据量：{len(data_list)}条')
        df = pd.DataFrame(data_list)

        # 数据类型优化
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'], format='ISO8601')

        # 数值列类型优化
        numeric_columns = [
            'lastPrice', 'open', 'high', 'low', 'lastClose', 'amount',
            'volume', 'pvolume', 'time', 'receive_time'
        ]
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 保存为feather格式
        logger.info(f'保存为feather格式：{FEATHER_FILE}')
        df.to_feather(FEATHER_FILE)

        # 输出统计信息
        logger.info(f'数据转换完成！')
        logger.info(f'- 原始文件：{RAW_TICK_FILE}')
        logger.info(f'- Feather文件：{FEATHER_FILE}')
        logger.info(f'- 数据形状：{df.shape}')
        logger.info(
            f'- 股票数量：{df["stock_code"].nunique() if "stock_code" in df.columns else "未知"}'
        )
        logger.info(f'- 时间范围：{df["timestamp"].min()} 到 {df["timestamp"].max()}'
                    if "timestamp" in df.columns else '')

        # 发送完成通知邮件
        try:
            send_email(subject=f'A股Tick数据处理完成 - {TODAY}',
                       content=f'''A股Tick数据处理完成！

处理日期：{TODAY}
数据形状：{df.shape}
股票数量：{df["stock_code"].nunique() if "stock_code" in df.columns else "未知"}
存储路径：{FEATHER_FILE}

数据已成功转换为feather格式，可用于后续分析。
''')
        except Exception as e:
            logger.warning(f'发送邮件通知失败：{e}')

    except Exception as e:
        logger.error(f'转换feather文件失败：{e}\n{traceback.format_exc()}')
        # 发送错误通知邮件
        try:
            send_email(
                subject=f'A股Tick数据处理失败 - {TODAY}',
                body=
                f'A股Tick数据处理过程中出现错误：\n\n{str(e)}\n\n{traceback.format_exc()}')
        except:
            pass


# def schedule_post_market_processing():
#     """
#     安排收盘后的数据处理任务
#     """
#     def job():
#         logger.info('开始执行收盘后数据处理...')
#         convert_to_feather()

#     # 安排在收盘后执行数据转换
#     schedule.every().day.at(STOP_TIME).do(job)

#     # 后台运行调度器
#     def run_scheduler():
#         while True:
#             schedule.run_pending()
#             time.sleep(60)  # 每分钟检查一次

#     scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
#     scheduler_thread.start()
#     logger.info(f'收盘后数据处理任务已安排在{STOP_TIME}执行')


def create_whole_quote_task(stock_pool, tick_queue):
    """
    创建全推行情订阅任务
    """
    # 先断开，重新连接
    xtdata.disconnect()
    subscribe_id = -1
    retry_count = 0
    max_retries = 5

    try:
        start_time = time.time()
        partial_on_tick_data = partial(on_tick_data, tick_queue=tick_queue)

        while subscribe_id < 0 and retry_count < max_retries:
            subscribe_id = xtdata.subscribe_whole_quote(
                stock_pool, callback=partial_on_tick_data)

            if subscribe_id < 0:
                retry_count += 1
                logger.error(
                    f'[全推行情订阅] 订阅失败，重试中... ({retry_count}/{max_retries})')
                time.sleep(min(retry_count * 2, 10))  # 指数退避

        if subscribe_id < 0:
            raise Exception(f'订阅失败，已重试{max_retries}次')

        # ----------------------------------- 订阅成功 ----------------------------------- #
        timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        cost_time = round(time.time() - start_time, 2)
        msg = f'【订阅成功】【全推行情】股票数量：{len(stock_pool)}，总耗时：{cost_time}秒，开始时间：{timestamp_now}'
        logger.info(msg)

    except Exception as e:
        logger.error(f'[全推行情订阅] 数据订阅失败：{e}\n{traceback.format_exc()}')
        # 发送错误通知
        try:
            send_email(
                subject=f'A股Tick数据订阅失败 - {TODAY}',
                body=f'A股Tick数据订阅失败：\n\n{str(e)}\n\n{traceback.format_exc()}')
        except:
            pass
        return

    # 主循环
    while True:
        time.sleep(1)
        current_time = datetime.now().strftime('%H:%M')
        if current_time >= STOP_TIME:
            logger.warning(f'【退出】全推行情订阅任务，当前时间：{current_time}')

            # 取消订阅
            try:
                xtdata.unsubscribe_quote(subscribe_id)
                logger.info('已取消行情订阅')
            except Exception as e:
                logger.warning(f'取消订阅失败：{e}')

            return


def main():
    """主函数"""
    logger.info(f'开始启动A股Tick数据采集系统 - {TODAY}')
    logger.info(f'数据存储目录：{SAVE_FOLDER}')
    logger.info(f'原始数据文件：{RAW_TICK_FILE}')
    logger.info(f'Feather文件：{FEATHER_FILE}')

    try:
        # 获取A股候选列表
        stock_list = get_a_stock_candidates()
        if not stock_list:
            logger.error('未获取到股票列表，程序退出')
            return

        # # 安排收盘后处理任务
        # schedule_post_market_processing()

        p_list = []

        with Manager() as manager:
            # 数据队列
            tick_queue = manager.Queue()  # 限制队列大小防止内存溢出
            file_lock = manager.Lock()  # 文件写入锁

            # 启动Tick数据处理进程
            for _ in range(1):
                p_list.append(
                    Process(target=process_tick_data,
                            args=(tick_queue, file_lock),
                            daemon=True,
                            name='tick数据处理进程'))

            for p in p_list:
                p.start()

            logger.info('Tick数据处理进程已启动')

            # 订阅全推行情
            create_whole_quote_task(stock_list, tick_queue)

            for p in p_list:
                p.join()

            # 安排收盘后处理任务
            convert_to_feather()

    except KeyboardInterrupt:
        logger.info('接收到中断信号，正在退出...')
    except Exception as e:
        logger.error(f'程序运行异常：{e}\n{traceback.format_exc()}')
        # 发送错误通知
        try:
            send_email(subject=f'A股Tick数据采集系统异常 - {TODAY}',
                       body=f'程序运行异常：\n\n{str(e)}\n\n{traceback.format_exc()}')
        except:
            pass
    finally:
        logger.info('A股Tick数据采集系统已退出')


if __name__ == '__main__':
    main()
