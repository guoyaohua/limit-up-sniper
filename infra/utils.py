# encoding=utf-8
import os
import smtplib
import datetime
from loguru import logger
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.header import Header
import concurrent.futures

# --- 邮件配置 ---
# SMTP 端点可使用公开默认值；账户、授权码与收发件地址只从环境变量读取。
MAIL_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
MAIL_USER = os.getenv("SMTP_USERNAME", "")
MAIL_PASS = os.getenv("SMTP_PASSWORD") or os.getenv("QQ_MAIL_TOKEN")
SENDER_EMAIL = os.getenv("SMTP_SENDER", MAIL_USER)
RECEIVER_EMAIL = os.getenv("SMTP_RECIPIENT", "")


def init_logger(name, log_dir, verbose=False):
    """
    初始化日志记录器实例。

    Args:
        name (str): 日志记录器的名称。
        log_dir (str): 日志文件的目录。
        verbose (bool, optional): 是否启用详细日志记录。默认为True。
    """

    # 检查日志目录是否存在，如果不存在则创建
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    # 如果不是详细模式，则移除默认的控制台输出
    if not verbose:
        logger.remove()

    # 获取当前日期，用于生成日志文件名
    today = datetime.datetime.now().strftime(r"%Y-%m-%d")

    # 配置不同级别的日志文件
    # format="<green>{time:HH:mm:ss:SSSS}</green> | {module} line:{line} {function} |{level} | {message}"

    # 添加 DEBUG 级别日志
    logger.add(
        os.path.join(log_dir, "DEBUG", f"Debug_{name}_{today}.log"),
        level="DEBUG",
        encoding="utf-8",
        enqueue=True,  # 异步写入，防止阻塞
    )
    # 添加 INFO 级别日志
    logger.add(
        os.path.join(log_dir, "INFO", f"Info_{name}_{today}.log"),
        level="INFO",
        encoding="utf-8",
        enqueue=True,
    )
    # 添加 WARNING 级别日志
    logger.add(
        os.path.join(log_dir, "WARNING", f"Warn_{name}_{today}.log"),
        level="WARNING",
        encoding="utf-8",
        enqueue=True,
    )
    # 添加 ERROR 级别日志
    logger.add(
        os.path.join(log_dir, "ERROR", f"Error_{name}_{today}.log"),
        level="ERROR",
        encoding="utf-8",
        enqueue=True,
    )
    # 添加 CRITICAL 级别日志
    logger.add(
        os.path.join(log_dir, "CRITICAL", f"Critical_{name}_{today}.log"),
        level="CRITICAL",
        encoding="utf-8",
        enqueue=True,
    )


def send_email(subject, content, add_timestamp=True):
    """
    发送纯文本邮件通知。

    Args:
        subject (str): 邮件主题。
        content (str): 邮件正文。
        add_timestamp (bool, optional): 是否在邮件内容前添加时间戳。默认为 True。

    Returns:
        str: 如果成功返回 "Success"，如果失败返回 "Failed" 及错误信息。
    """
    from datetime import datetime

    # 如果需要，在邮件内容前添加当前时间戳
    if add_timestamp:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = f"发送时间: {timestamp}\n\n{content}"

    try:
        # 创建纯文本邮件对象
        message = MIMEText(content, "plain", "utf-8")
        message["Subject"] = subject  # 设置邮件主题
        message["From"] = SENDER_EMAIL  # 设置发件人
        message["To"] = RECEIVER_EMAIL  # 设置收件人

        # 使用 SSL 加密方式连接 QQ 邮箱的 SMTP 服务器
        # smtpObj = smtplib.SMTP() # 普通连接
        # smtpObj.connect(mail_host, 587) # 普通连接端口
        smtpObj = smtplib.SMTP_SSL(MAIL_HOST, 465)  # 建立 SSL 安全连接

        # 登录邮箱
        smtpObj.login(MAIL_USER, MAIL_PASS)

        # 发送邮件
        smtpObj.sendmail(SENDER_EMAIL, [RECEIVER_EMAIL], message.as_string())

        # 关闭连接
        smtpObj.quit()

        return "Success"
    except Exception as e:
        # 捕获并返回异常信息
        return "Failed" + str(e)


def send_html_email(subject,
                    html_content,
                    sender_email=SENDER_EMAIL,
                    RECEIVER_EMAIL=RECEIVER_EMAIL,
                    plain_text=None,
                    smtp_server=MAIL_HOST,
                    smtp_port=465,
                    username=MAIL_USER,
                    password=MAIL_PASS,
                    attachments=None):
    """
    发送 HTML 格式的邮件
    
    参数:
    sender_email: 发件人邮箱地址
    RECEIVER_EMAIL: 收件人邮箱地址或地址列表
    subject: 邮件主题
    html_content: HTML 格式的邮件正文
    plain_text: 纯文本格式的邮件正文（HTML 不可用时的备选）
    smtp_server: SMTP 服务器地址
    smtp_port: SMTP 服务器端口
    username: SMTP 认证用户名
    password: SMTP 认证密码
    attachments: 附件文件路径列表
    """
    # 检查 SMTP 密码是否存在，这是必需的参数
    if not password:
        print("错误: 未提供SMTP认证密码")
        return False

    # 创建一个 MIMEMultipart 对象，'related' 类型允许邮件包含内联资源（如图片）
    msg = MIMEMultipart('related')

    # --- 设置邮件头部信息 ---
    msg['From'] = Header(sender_email)  # 发件人

    # 处理收件人列表，可以是单个地址或多个地址的列表
    if isinstance(RECEIVER_EMAIL, list):
        msg['To'] = ', '.join(RECEIVER_EMAIL)  # 多个收件人
    else:
        msg['To'] = RECEIVER_EMAIL  # 单个收件人

    msg['Subject'] = Header(subject, 'utf-8')  # 邮件主题，使用UTF-8编码以支持中文

    # --- 构建邮件正文 ---
    # 创建一个 'alternative' 类型的 MIMEMultipart 对象，用于提供HTML和纯文本两种格式
    msg_alternative = MIMEMultipart('alternative')
    msg.attach(msg_alternative)

    # 如果提供了纯文本内容，则添加为邮件的一部分
    if plain_text:
        msg_text = MIMEText(plain_text, 'plain', 'utf-8')
        msg_alternative.attach(msg_text)

    # 添加 HTML 内容
    msg_html = MIMEText(html_content, 'html', 'utf-8')
    msg_alternative.attach(msg_html)

    # --- 添加附件 ---
    if attachments:
        for file_path in attachments:
            if os.path.isfile(file_path):  # 确认文件存在
                with open(file_path, 'rb') as file:  # 以二进制读取模式打开文件
                    # 创建 MIMEApplication 对象作为附件
                    part = MIMEApplication(file.read(),
                                           Name=os.path.basename(file_path))
                    # 设置 Content-Disposition 头，让邮件客户端知道这是一个附件
                    part[
                        'Content-Disposition'] = f'attachment; filename="{os.path.basename(file_path)}"'
                    msg.attach(part)

    # --- 发送邮件 ---
    try:
        print(f"正在连接到SMTP服务器: {smtp_server}:{smtp_port}")
        # 使用 SMTP_SSL 连接到服务器，提供更安全的连接
        server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        # server.set_debuglevel(1)  # 取消注释此行以开启调试模式，查看详细的SMTP通信过程

        print("正在进行SMTP认证...")
        # 如果提供了用户名和密码，则登录服务器
        if username and password:
            server.login(username, password)
            print("SMTP认证成功")

        print("正在发送邮件...")
        # 发送构建好的邮件消息
        server.send_message(msg)
        server.quit()  # 关闭连接
        print("邮件发送成功！")
        return True
    # --- 异常处理 ---
    except smtplib.SMTPAuthenticationError as e:
        print(f"SMTP认证失败: {str(e)}")
        print("请检查用户名和密码是否正确")
        return False
    except smtplib.SMTPConnectError as e:
        print(f"SMTP连接失败: {str(e)}")
        print("请检查SMTP服务器地址和端口是否正确")
        return False
    except smtplib.SMTPException as e:
        print(f"SMTP错误: {str(e)}")
        return False
    except Exception as e:
        print(f"邮件发送失败，发生未知错误: {str(e)}")
        return False


def run_with_timeout(func, args=(), kwargs=None, timeout=5):
    """
    带超时的函数执行包装器
    
    Args:
        func: 要执行的函数
        args: 位置参数
        kwargs: 关键字参数
        timeout: 超时时间(秒)
        
    Returns:
        函数执行结果
        
    Raises:
        TimeoutError: 如果执行超时
        Exception: 其他异常
    """
    if kwargs is None:
        kwargs = {}
        
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(func, *args, **kwargs)
        result = future.result(timeout=timeout)
        # 正常执行完成，等待资源回收
        executor.shutdown(wait=True)
        return result
    except concurrent.futures.TimeoutError:
        # 关键修复：超时发生时，强制不等待线程结束，直接返回
        # 避免因任务卡死导致整个进程在 shutdown 时无限挂起
        executor.shutdown(wait=False)
        raise TimeoutError(f"函数 {func.__name__} 执行超时 ({timeout}s)")
    except Exception as e:
        # 其他异常说明任务已结束（报错也是结束），正常回收
        executor.shutdown(wait=True)
        raise e
