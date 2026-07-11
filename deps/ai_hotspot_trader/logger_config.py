"""
统一日志配置模块

使用 loguru 替代标准 logging，提供美化的日志输出。
支持按客户端隔离日志文件。
"""

import os
import sys
from datetime import datetime
from loguru import logger

# 日志根目录
LOG_ROOT = os.getenv(
    'AI_HOTSPOT_LOG_DIR', os.path.join('logs', 'ai_hotspot_trader')
)

# 当前客户端标识（运行时通过 setup_logger 设置）
_current_client_key = None

def setup_logger(client_key: str = None):
    """
    配置 loguru 日志系统
    
    Args:
        client_key: 客户端标识（如 'zjsp', 'gjmn', 'simulate'），用于隔离日志目录。
                    如果为 None，仅配置控制台输出，不创建文件日志。
                    文件日志仅在指定 client_key 后才会创建。
    
    特性：
    - 控制台输出带颜色和美化格式
    - 文件日志按日期分割（需要 client_key）
    - 错误日志单独记录（需要 client_key）
    - 自动处理中文编码
    - 显示文件名、函数名、行号等调试信息
    - 按客户端隔离日志文件
    """
    global _current_client_key
    _current_client_key = client_key
    
    # 移除默认的 handler
    logger.remove()
    
    # ==================== 控制台输出格式 ====================
    # 详细格式：时间 | 级别 | 进程ID | 文件名:函数名:行号 | 消息
    console_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{process}</cyan> | "
        "<magenta>{name}</magenta>:<cyan>{function}</cyan>:<yellow>{line}</yellow> | "
        "<level>{message}</level>"
    )
    
    # 添加控制台 handler（带颜色）- INFO 及以下级别
    logger.add(
        sys.stdout,
        format=console_format,
        level="DEBUG",
        colorize=True,
        backtrace=True,
        diagnose=True,
        filter=lambda record: record["level"].no < 40,  # DEBUG, INFO, WARNING
    )
    
    # 错误级别单独输出到 stderr（红色高亮）
    logger.add(
        sys.stderr,
        format=console_format,
        level="ERROR",
        colorize=True,
        backtrace=True,
        diagnose=True,
    )
    
    # ==================== 文件日志（仅在指定 client_key 时创建） ====================
    if client_key is None:
        # 没有 client_key 时只配置控制台输出，不创建文件日志
        return logger
    
    # 确定日志目录（按客户端隔离）
    log_dir = os.path.join(LOG_ROOT, client_key)
    
    # 确保日志目录存在
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # 日志文件名（按日期）
    log_date = datetime.now().strftime("%Y%m%d")
    log_file = os.path.join(log_dir, f"strategy_{log_date}.log")
    error_file = os.path.join(log_dir, f"error_{log_date}.log")
    
    # ==================== 文件日志格式 ====================
    # 文件格式包含更完整的信息：时间戳、级别、进程ID、线程ID、模块:函数:行号、消息
    file_format = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "PID:{process} TID:{thread} | "
        "{name}:{function}:{line} | "
        "{message}"
    )
    
    # 添加文件 handler（所有日志）
    logger.add(
        log_file,
        format=file_format,
        level="DEBUG",
        rotation="00:00",  # 每天午夜轮转
        retention="30 days",  # 保留30天
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )
    
    # 添加错误日志文件 handler（含完整异常堆栈）
    logger.add(
        error_file,
        format=file_format,
        level="ERROR",
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )
    
    if client_key:
        logger.info(f"日志系统初始化完成，客户端: {client_key}，日志目录: {log_dir}")
    
    return logger

def get_logger(name: str = None):
    """
    获取带模块名的 logger
    
    Args:
        name: 模块名，默认为 None（使用调用者的模块名）
        
    Returns:
        配置好的 logger 实例
    """
    if name:
        return logger.bind(name=name)
    return logger

def get_current_client_key() -> str:
    """
    获取当前客户端标识
    
    Returns:
        客户端标识字符串，未设置时返回 None
    """
    return _current_client_key

# 初始化日志配置（默认不带客户端隔离，在 main 中会重新调用 setup_logger）
setup_logger()

# 导出
__all__ = ['logger', 'get_logger', 'setup_logger', 'get_current_client_key']
