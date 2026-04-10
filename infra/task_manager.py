"""
task_manager.py - 任务管理器模块 (v2.4 新增)

功能：
1. TaskManager: 统一管理所有子进程和子线程的生命周期
2. 心跳监控机制：实时检测任务健康状态
3. 自动重启机制：当任务中断时自动重启
4. 告警通知：任务异常时发送 CRITICAL 日志和邮件
5. 优雅退出：使用 signal 处理退出信号

使用方法：
    from task_manager import TaskManager, TaskInfo, get_task_manager
    
    # 获取全局任务管理器
    manager = get_task_manager()
    
    # 注册任务
    manager.register_task(TaskInfo(
        name="my_task",
        target=my_function,
        args=(arg1, arg2),
        task_type="process",  # 或 "thread"
        restart_on_failure=True,
        max_restart_count=5
    ))
    
    # 启动所有任务
    manager.start_all()
    
    # 等待完成或停止信号
    manager.wait_for_completion()
"""

import os
import sys
import time
import signal
import traceback
import threading
from threading import Thread, Event
from multiprocessing import Process, Value
from datetime import datetime, time as dt_time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, Union
from loguru import logger

# 尝试导入邮件发送函数
try:
    from infra.utils import send_email
except ImportError:
    def send_email(subject, content):
        logger.warning(f"[邮件] {subject}: {content[:100]}...")

# ---------------------------------------------------------------------------- #
#                                    配置常量                                   #
# ---------------------------------------------------------------------------- #

# 任务监控配置
TASK_HEARTBEAT_TIMEOUT = 60  # 任务心跳超时时间（秒）
TASK_MAX_RESTART_COUNT = 5   # 任务最大重启次数
TASK_RESTART_DELAY = 5       # 任务重启延迟（秒）
CALLBACK_HEARTBEAT_TIMEOUT = 30  # 回调心跳超时时间（秒）

# 默认停止时间
DEFAULT_STOP_TIME = dt_time(15, 1)


class TaskStatus(Enum):
    """任务状态枚举"""
    PENDING = "pending"        # 等待启动
    RUNNING = "running"        # 运行中
    STOPPED = "stopped"        # 已停止
    FAILED = "failed"          # 失败
    RESTARTING = "restarting"  # 重启中


@dataclass
class TaskInfo:
    """任务信息数据类"""
    name: str                                    # 任务名称
    target: Callable                             # 目标函数
    args: tuple = field(default_factory=tuple)   # 位置参数
    kwargs: dict = field(default_factory=dict)   # 关键字参数
    task_type: str = "process"                   # 任务类型: "process" 或 "thread"
    daemon: bool = False                         # 是否为守护进程/线程
    restart_on_failure: bool = True              # 失败时是否重启
    max_restart_count: int = TASK_MAX_RESTART_COUNT  # 最大重启次数
    heartbeat_timeout: float = TASK_HEARTBEAT_TIMEOUT  # 心跳超时时间
    
    # 运行时状态（不参与初始化）
    status: TaskStatus = field(default=TaskStatus.PENDING, init=False)
    instance: Union[Process, Thread, None] = field(default=None, init=False)
    restart_count: int = field(default=0, init=False)
    last_heartbeat: float = field(default_factory=time.time, init=False)
    start_time: Optional[float] = field(default=None, init=False)
    error_message: Optional[str] = field(default=None, init=False)


class TaskManager:
    """
    任务管理器 - 统一管理所有子进程和子线程
    
    功能：
    1. 统一管理进程/线程的生命周期
    2. 心跳监控，检测任务健康状态
    3. 自动重启失败的任务
    4. 优雅退出，确保资源正确释放
    5. 告警通知
    """
    
    def __init__(self, stop_time: dt_time = DEFAULT_STOP_TIME):
        """
        初始化任务管理器
        
        Args:
            stop_time: 自动停止时间
        """
        self._tasks: Dict[str, TaskInfo] = {}
        self._stop_event = Event()  # 全局停止事件
        self._monitor_thread: Optional[Thread] = None
        self._lock = threading.Lock()
        self._started = False
        self._stop_time = stop_time
        
        # 注册信号处理器
        self._setup_signal_handlers()
        
        logger.info("[TaskManager] 任务管理器初始化完成")
    
    def _setup_signal_handlers(self):
        """设置信号处理器，用于优雅退出"""
        def signal_handler(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.warning(f"[TaskManager] 收到信号 {sig_name}，开始优雅退出...")
            self.shutdown()
        
        # 注册常见的退出信号
        try:
            signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
            signal.signal(signal.SIGTERM, signal_handler)  # kill 命令
            if hasattr(signal, 'SIGBREAK'):
                signal.signal(signal.SIGBREAK, signal_handler)  # Windows: Ctrl+Break
        except Exception as e:
            logger.warning(f"[TaskManager] 信号处理器设置失败: {e}")
    
    def set_stop_time(self, stop_time: dt_time):
        """设置停止时间"""
        self._stop_time = stop_time
    
    def register_task(self, task_info: TaskInfo) -> str:
        """
        注册任务
        
        Args:
            task_info: TaskInfo 实例
            
        Returns:
            任务名称
        """
        with self._lock:
            if task_info.name in self._tasks:
                logger.warning(f"[TaskManager] 任务 {task_info.name} 已存在，将被覆盖")
            
            self._tasks[task_info.name] = task_info
            
            logger.info(f"[TaskManager] 注册任务: {task_info.name} (类型: {task_info.task_type})")
            return task_info.name
    
    def start_task(self, task_name: str) -> bool:
        """
        启动单个任务
        
        Args:
            task_name: 任务名称
            
        Returns:
            是否成功启动
        """
        with self._lock:
            if task_name not in self._tasks:
                logger.error(f"[TaskManager] 任务 {task_name} 未注册")
                return False
            
            task = self._tasks[task_name]
            
            if task.status == TaskStatus.RUNNING and task.instance and task.instance.is_alive():
                logger.warning(f"[TaskManager] 任务 {task_name} 已在运行中")
                return True
            
            try:
                # 注意：不使用心跳包装器，因为嵌套函数无法被pickle序列化
                # 对于进程任务，通过检查进程是否存活来判断健康状态
                # 如果需要更细粒度的心跳，进程内部可以自己实现
                wrapped_target = task.target
                
                # 创建任务实例
                if task.task_type == "process":
                    task.instance = Process(
                        target=wrapped_target,
                        args=task.args,
                        kwargs=task.kwargs,
                        daemon=task.daemon,
                        name=task_name
                    )
                else:
                    task.instance = Thread(
                        target=wrapped_target,
                        args=task.args,
                        kwargs=task.kwargs,
                        daemon=task.daemon,
                        name=task_name
                    )
                
                # 启动任务
                task.instance.start()
                task.status = TaskStatus.RUNNING
                task.start_time = time.time()
                task.last_heartbeat = time.time()
                
                pid_tid = getattr(task.instance, 'pid', None) or getattr(task.instance, 'ident', 'N/A')
                logger.info(f"[TaskManager] 任务 {task_name} 启动成功 (PID/TID: {pid_tid})")
                return True
                
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error_message = str(e)
                logger.critical(f"[TaskManager] 任务 {task_name} 启动失败: {e}")
                send_email(
                    f'【任务启动失败】{task_name}',
                    f'任务 {task_name} 启动失败:\n{e}\n{traceback.format_exc()}'
                )
                return False
    
    def stop_task(self, task_name: str, timeout: float = 10.0) -> bool:
        """
        停止单个任务
        
        Args:
            task_name: 任务名称
            timeout: 等待超时时间（秒）
            
        Returns:
            是否成功停止
        """
        with self._lock:
            if task_name not in self._tasks:
                logger.error(f"[TaskManager] 任务 {task_name} 未注册")
                return False
            
            task = self._tasks[task_name]
            
            if task.instance is None or not task.instance.is_alive():
                task.status = TaskStatus.STOPPED
                logger.info(f"[TaskManager] 任务 {task_name} 已停止")
                return True
            
            try:
                # 尝试优雅停止
                if task.task_type == "process":
                    task.instance.terminate()
                    task.instance.join(timeout=timeout)
                    
                    if task.instance.is_alive():
                        logger.warning(f"[TaskManager] 任务 {task_name} 未能在 {timeout}s 内停止，强制终止")
                        task.instance.kill()
                        task.instance.join(timeout=5)
                else:
                    # 线程无法强制终止，只能等待
                    task.instance.join(timeout=timeout)
                
                task.status = TaskStatus.STOPPED
                logger.info(f"[TaskManager] 任务 {task_name} 已停止")
                return True
                
            except Exception as e:
                logger.error(f"[TaskManager] 停止任务 {task_name} 失败: {e}")
                return False
    
    def restart_task(self, task_name: str) -> bool:
        """
        重启任务
        
        Args:
            task_name: 任务名称
            
        Returns:
            是否成功重启
        """
        task = self._tasks.get(task_name)
        if task is None:
            logger.error(f"[TaskManager] 任务 {task_name} 未注册")
            return False
        
        # 检查重启次数
        if task.restart_count >= task.max_restart_count:
            logger.critical(f"[TaskManager] 任务 {task_name} 重启次数已达上限 ({task.max_restart_count})，停止重启")
            task.status = TaskStatus.FAILED
            send_email(
                f'【任务重启失败】{task_name}',
                f'任务 {task_name} 重启次数已达上限 ({task.max_restart_count})，请检查系统状态'
            )
            return False
        
        task.status = TaskStatus.RESTARTING
        task.restart_count += 1
        
        logger.warning(f"[TaskManager] 正在重启任务 {task_name} (第 {task.restart_count} 次)")
        
        # 先停止旧任务
        self.stop_task(task_name, timeout=5)
        
        # 等待一段时间再重启
        time.sleep(TASK_RESTART_DELAY)
        
        # 启动新任务
        success = self.start_task(task_name)
        
        if success:
            logger.info(f"[TaskManager] 任务 {task_name} 重启成功")
            send_email(
                f'【任务重启成功】{task_name}',
                f'任务 {task_name} 第 {task.restart_count} 次重启成功'
            )
        else:
            logger.critical(f"[TaskManager] 任务 {task_name} 重启失败")
        
        return success
    
    def check_task_health(self, task_name: str) -> bool:
        """
        检查任务健康状态
        
        由于 Windows 上多进程使用 spawn 模式，嵌套函数无法被 pickle 序列化，
        因此不使用心跳包装器。健康状态检查只基于进程/线程是否存活。
        
        如果需要更细粒度的心跳监控（如 xtdata 回调），
        应该在进程/回调内部使用 CallbackHeartbeatMonitor 类。
        
        Args:
            task_name: 任务名称
            
        Returns:
            任务是否健康
        """
        task = self._tasks.get(task_name)
        if task is None:
            return False
        
        # 检查任务实例是否存活
        if task.instance is None or not task.instance.is_alive():
            logger.warning(f"[TaskManager] 任务 {task_name} 实例已不存活")
            return False
        
        # 注意：由于不使用心跳包装器，这里不检查心跳时间戳
        # 心跳时间戳只在进程启动时设置，不会持续更新
        # 如果需要更细粒度的心跳监控，请在进程内部实现
        
        return True
    
    def _monitor_loop(self):
        """监控循环 - 定期检查所有任务状态"""
        logger.info("[TaskManager] 监控循环启动")
        
        last_summary_time = time.time()
        
        while not self._stop_event.is_set():
            try:
                current_time = datetime.now()
                
                # 检查是否到达停止时间
                if current_time.time() >= self._stop_time:
                    logger.info("[TaskManager] 到达停止时间，准备关闭")
                    self.shutdown()
                    break
                
                # 遍历检查所有任务
                for task_name, task in list(self._tasks.items()):
                    if task.status not in [TaskStatus.RUNNING, TaskStatus.RESTARTING]:
                        continue
                    
                    # 检查任务健康状态
                    if not self.check_task_health(task_name):
                        logger.critical(f"[TaskManager] 任务 {task_name} 异常，状态: {task.status}")
                        
                        # 发送告警
                        send_email(
                            f'【任务异常】{task_name}',
                            f'任务 {task_name} 检测到异常\n'
                            f'状态: {task.status}\n'
                            f'重启次数: {task.restart_count}\n'
                            f'错误信息: {task.error_message or "未知"}'
                        )
                        
                        # 尝试重启
                        if task.restart_on_failure:
                            self.restart_task(task_name)
                
                # 每60秒输出状态摘要
                if time.time() - last_summary_time >= 60:
                    self._log_status_summary()
                    last_summary_time = time.time()
                
            except Exception as e:
                logger.exception(f"[TaskManager] 监控循环异常: {e}")
            
            # 监控间隔
            time.sleep(5)
        
        logger.info("[TaskManager] 监控循环结束")
    
    def _log_status_summary(self):
        """输出任务状态摘要"""
        running_count = sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)
        failed_count = sum(1 for t in self._tasks.values() if t.status == TaskStatus.FAILED)
        
        logger.info(f"[TaskManager] 状态摘要 - 总任务: {len(self._tasks)}, 运行中: {running_count}, 失败: {failed_count}")
        
        for task_name, task in self._tasks.items():
            logger.debug(f"  - {task_name}: {task.status.value}, 重启次数: {task.restart_count}")
    
    def start_all(self):
        """启动所有已注册的任务"""
        if self._started:
            logger.warning("[TaskManager] 任务管理器已启动")
            return
        
        self._started = True
        logger.info(f"[TaskManager] 开始启动所有任务 (共 {len(self._tasks)} 个)")
        
        for task_name in self._tasks:
            self.start_task(task_name)
        
        # 启动监控线程
        self._monitor_thread = Thread(
            target=self._monitor_loop,
            daemon=True,
            name="TaskManager-Monitor"
        )
        self._monitor_thread.start()
        
        logger.info("[TaskManager] 所有任务启动完成，监控已开启")
    
    def shutdown(self):
        """关闭所有任务"""
        logger.warning("[TaskManager] 开始关闭所有任务...")
        
        # 设置停止事件
        self._stop_event.set()
        
        # 停止所有任务
        for task_name in list(self._tasks.keys()):
            self.stop_task(task_name, timeout=10)
        
        logger.info("[TaskManager] 所有任务已关闭")
    
    def wait_for_completion(self):
        """等待所有任务完成或停止信号"""
        try:
            while not self._stop_event.is_set():
                # 检查是否到达停止时间
                if datetime.now().time() >= self._stop_time:
                    logger.info("[TaskManager] 到达停止时间")
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            logger.warning("[TaskManager] 收到键盘中断")
        finally:
            self.shutdown()
    
    def is_stopped(self) -> bool:
        """检查是否已停止"""
        return self._stop_event.is_set()
    
    def get_stop_event(self) -> Event:
        """获取停止事件"""
        return self._stop_event
    
    def get_task_status(self, task_name: str) -> Optional[TaskStatus]:
        """获取任务状态"""
        task = self._tasks.get(task_name)
        return task.status if task else None
    
    def get_all_task_status(self) -> Dict[str, TaskStatus]:
        """获取所有任务状态"""
        return {name: task.status for name, task in self._tasks.items()}


class CallbackHeartbeatMonitor:
    """
    回调心跳监控器 - 监控 xtdata 回调函数的健康状态
    
    功能：
    1. 记录每次回调的时间戳
    2. 检测回调是否超时
    3. 超时时触发重新订阅
    
    使用方法：
        # 创建监控器
        monitor = CallbackHeartbeatMonitor(timeout=30)
        
        # 在回调中更新心跳
        def on_data(datas):
            monitor.update()
            # ... 处理数据
        
        # 在监控线程中检查健康状态
        if not monitor.is_healthy():
            # 重新订阅
            pass
    """
    
    def __init__(self, name: str = "callback", timeout: float = CALLBACK_HEARTBEAT_TIMEOUT):
        self.name = name
        self.timeout = timeout
        self.last_callback_time = Value('d', time.time())
        self.callback_count = Value('i', 0)
        self.error_count = Value('i', 0)
        self._lock = threading.Lock()
        self._unhealthy_notified = False  # 避免重复通知
    
    def update(self):
        """更新回调时间戳"""
        try:
            with self.last_callback_time.get_lock():
                self.last_callback_time.value = time.time()
            with self.callback_count.get_lock():
                self.callback_count.value += 1
            self._unhealthy_notified = False  # 恢复后重置通知标志
        except Exception as e:
            logger.error(f"[{self.name}] 更新心跳失败: {e}")
    
    def record_error(self):
        """记录错误"""
        with self.error_count.get_lock():
            self.error_count.value += 1
    
    def is_healthy(self) -> bool:
        """检查回调是否健康"""
        with self.last_callback_time.get_lock():
            last_time = self.last_callback_time.value
        return (time.time() - last_time) < self.timeout
    
    def get_last_callback_age(self) -> float:
        """获取距离上次回调的时间（秒）"""
        with self.last_callback_time.get_lock():
            return time.time() - self.last_callback_time.value
    
    def get_callback_count(self) -> int:
        """获取回调次数"""
        with self.callback_count.get_lock():
            return self.callback_count.value
    
    def get_error_count(self) -> int:
        """获取错误次数"""
        with self.error_count.get_lock():
            return self.error_count.value
    
    def reset(self):
        """重置监控器"""
        with self.last_callback_time.get_lock():
            self.last_callback_time.value = time.time()
        with self.callback_count.get_lock():
            self.callback_count.value = 0
        with self.error_count.get_lock():
            self.error_count.value = 0
        self._unhealthy_notified = False
    
    def check_and_notify(self) -> bool:
        """
        检查健康状态，如果不健康且尚未通知则返回True
        
        Returns:
            bool: 是否需要处理（不健康且未通知过）
        """
        if not self.is_healthy() and not self._unhealthy_notified:
            self._unhealthy_notified = True
            age = self.get_last_callback_age()
            logger.critical(
                f"[{self.name}] 回调心跳超时! 距离上次回调: {age:.1f}s > {self.timeout}s, "
                f"总回调次数: {self.get_callback_count()}, 错误次数: {self.get_error_count()}"
            )
            return True
        return False
    
    def get_status_dict(self) -> dict:
        """获取状态字典，用于监控报告"""
        return {
            'name': self.name,
            'healthy': self.is_healthy(),
            'last_callback_age': self.get_last_callback_age(),
            'callback_count': self.get_callback_count(),
            'error_count': self.get_error_count(),
            'timeout': self.timeout
        }


# ProcessHealthMonitor 类已被移除
# 
# 说明：ProcessHealthMonitor 的功能与 TaskManager 完全重叠，
# 统一使用 TaskManager 进行进程/线程的生命周期管理。
#
# TaskManager 提供以下功能：
# 1. 注册进程/线程任务
# 2. 检查任务健康状态（is_alive）
# 3. 自动重启失败的任务
# 4. 发送告警邮件
# 5. 优雅退出（信号处理）


# 全局任务管理器实例
_task_manager: Optional[TaskManager] = None

def get_task_manager(stop_time: dt_time = DEFAULT_STOP_TIME) -> TaskManager:
    """
    获取全局任务管理器实例
    
    Args:
        stop_time: 自动停止时间
        
    Returns:
        TaskManager 实例
    """
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager(stop_time)
    return _task_manager


def reset_task_manager():
    """重置全局任务管理器（主要用于测试）"""
    global _task_manager
    if _task_manager is not None:
        _task_manager.shutdown()
    _task_manager = None


if __name__ == "__main__":
    # 简单测试
    import multiprocessing
    
    def test_task(name, duration=10):
        """测试任务"""
        logger.info(f"[{name}] 任务开始，将运行 {duration} 秒")
        for i in range(duration):
            if i % 2 == 0:
                logger.debug(f"[{name}] 运行中... {i}/{duration}")
            time.sleep(1)
        logger.info(f"[{name}] 任务完成")
    
    def test_failing_task():
        """会失败的测试任务"""
        logger.info("[failing_task] 任务开始，将在 3 秒后失败")
        time.sleep(3)
        raise RuntimeError("测试错误")
    
    # 创建任务管理器
    manager = TaskManager(stop_time=dt_time(23, 59))
    
    # 注册任务
    manager.register_task(TaskInfo(
        name="test_process_1",
        target=test_task,
        args=("Process1", 15),
        task_type="process",
        restart_on_failure=True
    ))
    
    manager.register_task(TaskInfo(
        name="test_thread_1",
        target=test_task,
        args=("Thread1", 10),
        task_type="thread",
        daemon=True
    ))
    
    # 启动所有任务
    manager.start_all()
    
    # 等待一段时间
    logger.info("等待 20 秒...")
    time.sleep(20)
    
    # 关闭
    manager.shutdown()
    logger.info("测试完成")
