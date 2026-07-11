import pickle
import os
import time
import traceback
import threading
import concurrent.futures
from datetime import datetime
from multiprocessing import Value, Array, Manager
from loguru import logger

from data.helpers import get_safe_typecode
from infra.utils import send_email
from infra.task_manager import get_task_manager
from config import STRATEGY_NAME, STOP_TIME
from infra.common_enums import StockLimitStatusInt, StockOrderStatusInt


def deep_serialize(value):
    """
    深度递归序列化，将所有 multiprocessing.Manager 的代理对象、
    multiprocessing.Value 和 multiprocessing.Array 对象
    转换为可安全 pickle 的基本 Python 类型。
    """
    try:
        # 基本类型直接返回
        if isinstance(value, (int, float, str, bool, type(None))):
            return value
        # Synchronized wrapper 对象 (multiprocessing.Value/Array的包装器)
        elif 'Synchronized wrapper' in str(type(value)) or str(type(
                value)).startswith("<class 'multiprocessing.sharedctypes."):
            if hasattr(value, 'value') and hasattr(value, 'get_lock'):
                # 这是一个 multiprocessing.Value 的包装器
                try:
                    with value.get_lock():
                        typecode = get_safe_typecode(value)
                        return {
                            '_type_': 'multiprocessing.Value',
                            '_value_': value.value,
                            '_typecode_': typecode
                        }
                except Exception as e:
                    logger.error(
                        f"序列化 Synchronized wrapper Value 时出错: {e}, 类型: {type(value)}"
                    )
                    raise e

            elif hasattr(value, 'get_lock') and hasattr(value, '__len__'):
                # 这是一个 multiprocessing.Array 的包装器
                try:
                    with value.get_lock():
                        typecode = get_safe_typecode(value)
                        return {
                            '_type_': 'multiprocessing.Array',
                            '_value_': list(value[:]),
                            '_typecode_': typecode
                        }
                except Exception as e:
                    logger.warning(
                        f"序列化 Synchronized wrapper Array 时出错: {e}, 类型: {type(value)}"
                    )
                    raise e

        # Manager ValueProxy
        elif 'ValueProxy' in str(type(value)):
            return {
                '_type_': 'Value',
                '_value_': value.value,
                '_typecode_': get_safe_typecode(value)
            }

        # Manager DictProxy
        elif 'DictProxy' in str(type(value)):
            return {
                '_type_': 'Dict',
                '_value_': {k: deep_serialize(v)
                            for k, v in value.items()}
            }
        # Manager ListProxy
        elif 'ListProxy' in str(type(value)):
            return {
                '_type_': 'List',
                '_value_': [deep_serialize(v) for v in value]
            }
        # 普通字典
        elif isinstance(value, dict):
            return {k: deep_serialize(v) for k, v in value.items()}
        # 普通列表
        elif isinstance(value, list):
            return [deep_serialize(v) for v in value]
        # 其他无法识别的类型
        else:
            try:
                # 尝试调用，看是否是可序列化的对象
                pickle.dumps(value)
                return value
            except (pickle.PicklingError, TypeError):
                logger.warning(f"序列化时遇到未知或不可序列化类型: {type(value)}，将转换为字符串。")
                return str(value)
    except Exception as e:
        logger.exception(str(e) + '\n' + str(value))
        raise e


def _create_single_stock_signal(stock_code, signal_data=None):
    """
    创建单只股票的信号字典

    Args:
        stock_code: 股票代码
        signal_data: 序列化的信号数据（用于恢复），如果为None则创建新的

    Returns:
        tuple: (stock_code, signal_dict)
    """
    if signal_data is None:
        # 创建新的信号字典
        return (stock_code, {
            '股票状态': Value('i', StockLimitStatusInt.NOT_LIMIT_UP),
            '下单状态': Value('i', StockOrderStatusInt.NOT_ORDERED),
            '封单金额': Value('d', 0.0),
            '封单金额变化率': Value('d', 0.0),
            '前一价格': Value('d', 0.0),
            '拉板所需资金': Value('d', 0.0),
            '下单时成交量': Value('i', 0),
            '下单时封单量': Value('i', 0),
            '最高价': Value('d', 0.0),
            '止盈止损价格列表': Array('d', [0.0 for _ in range(10)]),
            '目标剩余仓位': Array('i', [0 for _ in range(10)]),
            # v3.0: 日内止盈档位触发标记
            '止盈_5pct': Value('i', 0),
            '止盈_8pct': Value('i', 0),
            '止盈_10pct': Value('i', 0),
        })
    else:
        # 从序列化数据恢复
        result = {}
        for field_name, field_value in signal_data.items():
            if isinstance(field_value, dict) and '_type_' in field_value:
                typecode = field_value.get('_typecode_', 'i')
                val = field_value['_value_']
                if field_value['_type_'] in ('multiprocessing.Value', 'Value'):
                    result[field_name] = Value(typecode, val)
                elif field_value['_type_'] == 'multiprocessing.Array':
                    result[field_name] = Array(typecode, val)
            else:
                result[field_name] = field_value
        return (stock_code, result)


def _batch_create_stock_signals(stock_codes, signals_data=None, max_workers=None):
    """
    批量并行创建股票信号字典（v2.4.1 优化）

    使用线程池并行创建，显著加速初始化过程。
    注意：每只股票仍然有独立的 Value/Array 对象，运行时不会有锁争夺问题。

    Args:
        stock_codes: 股票代码列表
        signals_data: 序列化的信号数据字典（用于恢复），如果为None则创建新的
        max_workers: 最大工作线程数，默认根据股票数量自动调整

    Returns:
        dict: {stock_code: signal_dict}
    """
    if not stock_codes:
        return {}

    num_stocks = len(stock_codes)
    start_time = time.time()
    logger.info(f"[批量创建股票信号] 开始为 {num_stocks} 只股票创建信号对象...")

    # 根据股票数量动态调整线程数
    # 线程池主要用于并行化 I/O 等待和系统调用
    if max_workers is None:
        # 每个线程处理约100只股票，最多32个线程
        max_workers = min(32, max(4, num_stocks // 100))

    result = {}

    # 准备任务参数
    if signals_data is None:
        # 创建新的信号
        task_args = [(code, None) for code in stock_codes]
    else:
        # 从序列化数据恢复
        task_args = [(code, signals_data.get(code)) for code in stock_codes]

    # 使用线程池并行创建
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有创建任务
        futures = [
            executor.submit(_create_single_stock_signal, code, data)
            for code, data in task_args
        ]

        # 收集结果并显示进度
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            try:
                stock_code, signal_dict = future.result()
                result[stock_code] = signal_dict
                completed += 1
                # 每1000只股票输出一次进度
                if completed % 1000 == 0:
                    elapsed = time.time() - start_time
                    logger.info(f"[批量创建股票信号] 进度: {completed}/{num_stocks} ({elapsed:.1f}s)")
            except Exception as e:
                logger.error(f"[批量创建股票信号] 创建信号失败: {e}")

    elapsed = time.time() - start_time
    logger.info(f"[批量创建股票信号] 完成！{num_stocks} 只股票，总耗时 {elapsed:.2f}s，"
                f"平均 {elapsed/num_stocks*1000:.2f}ms/只")

    return result


def _parallel_restore_manager_proxies(items_to_restore):
    """
    并行恢复多个 Manager 代理对象（v2.4.1 优化）

    Args:
        items_to_restore: list of (key, serialized_value) 元组

    Returns:
        dict: {key: restored_proxy}
    """
    if not items_to_restore:
        return {}

    def restore_single_proxy(item):
        """恢复单个 Manager 代理"""
        key, value = item
        type_ = value['_type_']
        val = value['_value_']

        if type_ == 'Dict':
            # 递归恢复字典内容
            restored_dict = {}
            for k, v in val.items():
                restored_dict[k] = deep_restore(v, depth=2)
            return (key, Manager().dict(restored_dict))
        elif type_ == 'List':
            # 递归恢复列表内容
            restored_list = [deep_restore(v, depth=2) for v in val]
            return (key, Manager().list(restored_list))
        else:
            return (key, None)

    result = {}
    num_items = len(items_to_restore)
    max_workers = min(num_items, 16)

    start_time = time.time()
    logger.info(f"[并行恢复Manager代理] 开始恢复 {num_items} 个代理对象...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(restore_single_proxy, item) for item in items_to_restore]

        for future in concurrent.futures.as_completed(futures):
            try:
                key, proxy = future.result(timeout=30)
                if proxy is not None:
                    result[key] = proxy
            except Exception as e:
                logger.error(f"[并行恢复Manager代理] 恢复失败: {e}")

    elapsed = time.time() - start_time
    logger.info(f"[并行恢复Manager代理] 完成！{num_items} 个代理，耗时 {elapsed:.2f}s")

    return result


def deep_restore(value, depth=0, manager=None):
    """
    深度递归恢复，将序列化的数据结构转换回原始的
    multiprocessing 对象和 Manager 对象

    v2.4.1 优化：
    1. 对股票状态信号字典使用批量并行恢复
    2. 对顶层 Manager 代理对象使用并行恢复
    """
    # 所有 Manager 代理必须由同一个长期存活的 SyncManager 创建。
    # 为每个嵌套对象临时调用 Manager() 会在 Windows 上产生无法互相
    # 序列化的同步对象，并让 Manager 生命周期不可控。
    if manager is None:
        manager = Manager()

    # 检查是否是我们的自定义序列化字典
    if isinstance(value, dict) and '_type_' in value and '_value_' in value:
        type_ = value['_type_']
        val = value['_value_']
        typecode = value.get('_typecode_', 'i')

        if type_ == 'multiprocessing.Value':
            return Value(typecode, val)
        elif type_ == 'multiprocessing.Array':
            return Array(typecode, val)
        elif type_ == 'Value':
            # 向后兼容：改用 multiprocessing.Value（比 Manager().Value 更快）
            return Value(typecode, val)
        elif type_ == 'Dict':
            restored_dict = {}
            for k, v in val.items():
                restored_dict[k] = deep_restore(v, depth + 1, manager)
            # shared_data 的顶层容器应保持普通 dict：其中包含原生
            # multiprocessing.Value/Array，Windows 不允许把这些对象再
            # pickle 进 Manager.dict。嵌套的原始 DictProxy 仍恢复为代理。
            return restored_dict if depth == 0 else manager.dict(restored_dict)
        elif type_ == 'List':
            restored_list = [deep_restore(v, depth + 1, manager) for v in val]
            return manager.list(restored_list)

    # 递归处理普通字典
    elif isinstance(value, dict):
        # 特殊优化1：检测股票状态信号字典（depth=0 时检测）
        if depth == 0 and len(value) > 500:
            sample_key = next(iter(value), None)
            if sample_key and isinstance(value.get(sample_key), dict):
                sample_val = value[sample_key]
                # 检测是否包含典型的股票信号字段结构
                is_stock_signals = (
                    '股票状态' in sample_val or
                    '下单状态' in sample_val or
                    '封单金额' in sample_val
                )
                if is_stock_signals:
                    logger.info(f"[deep_restore] 检测到股票状态信号字典（{len(value)} 只股票），启用批量并行恢复")
                    return _batch_create_stock_signals(
                        stock_codes=list(value.keys()),
                        signals_data=value
                    )

        # 特殊优化2：顶层字典（shared_data）中的 Manager 代理并行恢复
        if depth == 0:
            # 收集需要并行恢复的 Manager 代理
            manager_proxy_items = []
            simple_items = {}

            for k, v in value.items():
                if isinstance(v, dict) and '_type_' in v and v['_type_'] in ('Dict', 'List'):
                    manager_proxy_items.append((k, v))
                else:
                    # 其他项目直接递归恢复
                    simple_items[k] = deep_restore(v, depth + 1, manager)

            # 并行恢复 Manager 代理
            if manager_proxy_items:
                logger.info(f"[deep_restore] 检测到 {len(manager_proxy_items)} 个 Manager 代理需要恢复")
                restored_proxies = {}
                for key, proxy_value in manager_proxy_items:
                    restored_proxies[key] = deep_restore(
                        proxy_value, depth + 1, manager
                    )
                simple_items.update(restored_proxies)

            return simple_items

        # 普通字典递归处理
        return {
            k: deep_restore(v, depth + 1, manager)
            for k, v in value.items()
        }

    # 递归处理普通列表
    elif isinstance(value, list):
        return [deep_restore(v, depth + 1, manager) for v in value]

    # 如果不是以上任何一种，直接返回值
    return value


def save_shared_data(shared_data, data_dir="./data_backup", prefix=''):
    """
    将共享数据以原子操作方式保存到文件，避免数据损坏。
    修复了序列化和文件写入逻辑。

    Args:
        shared_data (dict): 包含所有共享数据的字典。
        data_dir (str, optional): 保存数据文件的目录。 Defaults to "./data_backup".
    """
    import tempfile
    import shutil

    logger.info("开始保存共享数据...")

    try:
        data_dir = os.path.join(data_dir, STRATEGY_NAME)
        # 确保备份目录存在
        os.makedirs(data_dir, exist_ok=True)

        # 序列化数据
        serializable_data = deep_serialize(shared_data)

        # 目标文件名
        # 获取当前日期
        today = datetime.now().strftime('%Y%m%d')
        target_file = os.path.join(data_dir,
                                   f"{prefix}shared_data_backup_{today}.pkl")

        # 使用原子写操作
        temp_fd, temp_file_path = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
        try:
            with os.fdopen(temp_fd, 'wb') as temp_f:
                pickle.dump(serializable_data, temp_f)
                # Force Python's buffer to be written to the OS
                temp_f.flush()
                # Force the OS to write the file to disk
                os.fsync(temp_f.fileno())

            # 备份旧文件（如果存在）
            backup_file = None
            if os.path.exists(target_file):
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_file = f"{target_file}.backup_{timestamp}"
                try:
                    shutil.copy2(target_file, backup_file)
                    logger.info(f"备份旧数据文件到: {backup_file}")
                except Exception as e:
                    logger.error(f"备份旧数据文件失败: {e}")

            # 原子地移动/重命名临时文件到目标文件
            # shutil.move 在大多数情况下是原子的，并且可以覆盖现有文件
            shutil.move(temp_file_path, target_file)
            if backup_file:
                # 如果有备份文件，删除临时文件
                os.remove(backup_file)
            logger.info(f"共享数据成功保存到: {target_file}")
            return True

        except Exception as e:
            logger.exception(f"【关键错误】保存共享数据失败: {e}")
            send_email('【关键错误】保存共享数据失败',
                       f'保存共享数据时发生异常: {e}\n{traceback.format_exc()}')
            # 如果发生错误，尝试删除临时文件
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except OSError as e_rem:
                    logger.error(f"删除临时文件失败: {e_rem}")
            return False

    except Exception as e:
        logger.exception(f"【关键错误】保存共享数据时发生未知错误: {e}")
        send_email('【关键错误】保存共享数据时发生未知错误',
                   f'保存共享数据时发生未知异常: {e}\n{traceback.format_exc()}')
        return False
    except BaseException as e:
        logger.exception(f"【关键错误】保存共享数据时发生严重错误: {e}")
        send_email('【关键错误】保存共享数据时发生严重错误',
                   f'保存共享数据时发生严重异常: {e}\n{traceback.format_exc()}')
        return False


def load_shared_data(data_dir="./data_backup", prefix=''):
    """
    从本地文件加载shared_data

    Args:
        data_dir (str): 数据保存目录

    Returns:
        dict: 恢复的共享数据字典，如果没有找到备份文件则返回None
    """
    try:
        # 获取当前日期
        today = datetime.now().strftime('%Y%m%d')
        backup_file = os.path.join(data_dir, STRATEGY_NAME,
                                   f"{prefix}shared_data_backup_{today}.pkl")

        if not os.path.exists(backup_file):
            logger.info(f"未找到今日备份文件: {backup_file}")
            return None

        # 加载数据
        with open(backup_file, 'rb') as f:
            serializable_data = pickle.load(f)

        # 恢复共享数据
        restored_data = deep_restore(serializable_data)

        logger.info(f"共享数据已从 {backup_file} 恢复，共 {len(restored_data)} 个项目")
        return restored_data

    except Exception as e:
        logger.exception(f"【关键错误】加载共享数据失败: {e}")
        send_email('【关键错误】加载共享数据失败',
                   f'加载共享数据时发生异常: {e}\n{traceback.format_exc()}')
        return None


def start_shared_data_backup_task(shared_data,
                                  backup_interval=2,
                                  prefix='',
                                  stop_event=None):
    """
    启动共享数据备份定时任务

    Args:
        shared_data (dict): 共享数据字典
        backup_interval (int): 备份间隔（秒）
        prefix (str): 备份文件前缀
        stop_event (threading.Event, optional): 停止事件。如果提供，线程会在事件被设置时退出。
                                                  如果不提供，会尝试从 TaskManager 获取。

    Returns:
        threading.Thread: 备份线程对象

    Note:
        优雅退出机制：
        1. 优先使用传入的 stop_event
        2. 其次尝试使用 TaskManager 的全局停止事件
        3. 通过捕获 BrokenPipeError 等异常来处理程序退出时的情况
    """
    # 获取停止事件
    _stop_event = stop_event
    if _stop_event is None:
        try:
            # 尝试从 TaskManager 获取全局停止事件
            task_manager = get_task_manager()
            _stop_event = task_manager.get_stop_event()
            logger.debug("备份任务使用 TaskManager 的停止事件")
        except Exception:
            # 如果无法获取，创建一个本地事件（备用方案）
            _stop_event = threading.Event()
            logger.debug("备份任务使用本地停止事件（无法获取 TaskManager）")

    def backup_task():
        logger.info(f"共享数据备份线程已启动 (TID: {threading.current_thread().ident})")

        while True:
            try:
                # 检查停止事件（优先级最高）
                if _stop_event is not None and _stop_event.is_set():
                    logger.warning("【退出】共享数据备份任务（收到停止信号）")
                    break

                # 检查停止时间
                if datetime.now().time() >= STOP_TIME:
                    logger.warning("【退出】共享数据备份任务（已到达停止时间）")
                    break

                # 执行备份
                save_shared_data(shared_data, prefix=prefix)

                # 使用带超时的等待（同时作为 sleep 使用）
                # 如果 stop_event 被设置，会立即返回 True 并退出循环
                if _stop_event is not None:
                    if _stop_event.wait(timeout=backup_interval):
                        logger.warning("【退出】共享数据备份任务（等待期间收到停止信号）")
                        break
                else:
                    time.sleep(backup_interval)

            except (BrokenPipeError, ConnectionResetError, EOFError) as e:
                # 这些错误通常发生在程序退出时，Manager 连接已关闭
                logger.warning(
                    f"【退出】共享数据备份任务（管道/连接已关闭）: {type(e).__name__}: {e}")
                break
            except (OSError, IOError) as e:
                # Windows 上的管道错误可能表现为 OSError
                if 'pipe' in str(e).lower() or '232' in str(e):
                    logger.warning(f"【退出】共享数据备份任务（管道错误）: {e}")
                    break
                # 其他 OS 错误继续记录
                logger.exception(f"【错误】共享数据备份任务 OS 错误: {e}")
                if _stop_event is not None and _stop_event.wait(
                        timeout=backup_interval):
                    break
            except KeyboardInterrupt:
                logger.warning("【退出】共享数据备份任务（键盘中断）")
                break
            except Exception as e:
                # 检查是否是因为 Manager 关闭导致的错误
                error_msg = str(e).lower()
                if any(keyword in error_msg for keyword in
                       ['pipe', 'closed', 'connection', 'shutdown']):
                    logger.warning(f"【退出】共享数据备份任务（连接已关闭）: {e}")
                    break

                # 其他异常记录但继续运行
                logger.exception(f"【错误】共享数据备份任务异常: {e}")
                # 不在退出过程中发送邮件，避免产生更多错误
                if _stop_event is None or not _stop_event.is_set():
                    try:
                        send_email(
                            '【关键错误】共享数据备份任务异常',
                            f'共享数据备份任务发生异常: {e}\n{traceback.format_exc()}')
                    except Exception:
                        pass  # 忽略邮件发送错误

                # 等待一段时间后继续
                if _stop_event is not None:
                    if _stop_event.wait(timeout=backup_interval):
                        break
                else:
                    time.sleep(backup_interval)

        logger.info("共享数据备份线程已退出")

    # 启动后台线程执行备份任务
    backup_thread = threading.Thread(target=backup_task,
                                     daemon=True,
                                     name="SharedDataBackup")
    backup_thread.start()
    logger.info(f"共享数据备份任务已启动，每 {backup_interval} 秒备份一次")
    return backup_thread
