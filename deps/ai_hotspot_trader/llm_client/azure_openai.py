"""
Azure AI Foundry - 通用 OpenAI 兼容客户端
支持多种 Azure 上的 LLM 模型（DeepSeek、Grok 等）
功能：纯文本和多模态（Vision）对话

使用 OpenAI SDK 调用 Azure OpenAI 兼容 API
"""
import base64
import functools
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional, List, Union, Callable, TypeVar, Any

import httpx
from openai import OpenAI

from logger_config import logger
from .base import BaseLLMClient, VisionCapableMixin
from .config import (
    AZURE_ENDPOINT,
    AZURE_API_KEY,
    AZURE_DEFAULT_MODEL,
    AZURE_AVAILABLE_MODELS,
    AZURE_VISION_MODELS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
)

# 默认重试配置
DEFAULT_MAX_RETRIES = 5

# 类型变量，用于装饰器
T = TypeVar('T')

# 全局线程池，避免每次调用都创建新的线程池
# 关键改进：不使用 with 语句，避免等待子线程完成导致超时失效
_global_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="azure_api_")


def timeout_decorator(timeout_seconds: float) -> Callable:
    """
    超时装饰器：限制函数的整体执行时间
    
    使用全局 ThreadPoolExecutor 实现，确保函数在指定时间内完成，
    否则抛出 TimeoutError。
    
    关键改进：
    - 使用全局线程池，不使用 with 语句，避免等待子线程完成
    - 超时后立即返回，不会阻塞在线程池的 __exit__ 中
    - 注意：超时后子线程仍会继续执行直到完成，但调用者不会等待
    
    参数:
        timeout_seconds: 超时时间（秒）
    
    返回:
        装饰器函数
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            # 从实例获取超时时间（如果是方法调用）
            actual_timeout = timeout_seconds
            if args and hasattr(args[0], 'timeout'):
                actual_timeout = args[0].timeout
            
            # 使用全局线程池，不使用 with 语句，避免等待子线程完成
            future = _global_executor.submit(func, *args, **kwargs)
            try:
                return future.result(timeout=actual_timeout)
            except FuturesTimeoutError:
                # 超时后，尝试取消 future（如果尚未开始执行）
                # 注意：如果已经开始执行，cancel() 不会中断正在运行的线程
                future.cancel()
                raise TimeoutError(
                    f"函数 {func.__name__} 执行超时（超过 {actual_timeout} 秒）"
                )
        return wrapper
    return decorator


class AzureOpenAIClient(BaseLLMClient, VisionCapableMixin):
    """
    Azure AI Foundry 通用 OpenAI 兼容客户端

    使用 OpenAI SDK 调用 Azure OpenAI 兼容 API
    支持多种模型：DeepSeek、Grok 等
    支持纯文本和多模态（Vision）输入输出
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        verbose: bool = False,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """
        初始化 Azure OpenAI 客户端

        参数:
            api_key: Azure API 密钥，如果为 None 则使用配置文件中的默认值
            endpoint: Azure 端点，如果为 None 则使用配置文件中的默认值
            model: 使用的模型/部署名称，如果为 None 则使用默认模型
            verbose: 是否打印详细日志
            max_retries: 最大重试次数，默认 5 次
            timeout: 请求超时时间（秒），默认使用配置值
        """
        self.api_key = api_key or AZURE_API_KEY
        self.endpoint = endpoint or AZURE_ENDPOINT
        self.verbose = verbose
        self.timeout = timeout
        
        # 配置细粒度超时：连接超时、读取超时、写入超时、总超时
        # 这确保 HTTP 层面的超时也能正确触发
        http_timeout = httpx.Timeout(
            connect=30.0,      # 连接超时 30 秒
            read=timeout,      # 读取超时使用配置的总超时
            write=30.0,        # 写入超时 30 秒
            pool=10.0,         # 连接池等待超时 10 秒
        )
        
        # 初始化 OpenAI 客户端，使用细粒度超时配置
        self._client = OpenAI(
            base_url=self.endpoint,
            api_key=self.api_key,
            max_retries=max_retries,
            timeout=http_timeout,
        )
        
        super().__init__(model)

    @property
    def default_model(self) -> str:
        return AZURE_DEFAULT_MODEL

    @property
    def available_models(self) -> List[str]:
        return AZURE_AVAILABLE_MODELS

    @property
    def supports_vision(self) -> bool:
        """当前模型是否支持 Vision"""
        return self.model in AZURE_VISION_MODELS

    @staticmethod
    def _encode_image_to_base64(image_path: str) -> Optional[tuple]:
        """
        把本地图片文件编码成 base64 字符串

        参数:
            image_path: 图片文件路径

        返回:
            tuple: (base64_string, mime_type)，失败返回 None
        """
        try:
            with open(image_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")

            # 根据文件扩展名确定 MIME 类型
            ext = image_path.lower().split('.')[-1]
            mime_type = {
                'jpg': 'image/jpeg',
                'jpeg': 'image/jpeg',
                'png': 'image/png',
                'gif': 'image/gif',
                'webp': 'image/webp',
            }.get(ext, 'image/jpeg')

            return b64_data, mime_type
        except OSError as e:
            logger.exception(f"读取图片失败: {e}")
            return None

    @staticmethod
    def _get_filename(image_path: str) -> str:
        """从路径中提取文件名"""
        return image_path.replace('\\', '/').split('/')[-1]

    def _build_multimodal_message(
        self,
        prompt: str,
        image_paths: Optional[List[str]] = None
    ) -> List[dict]:
        """
        构造多模态 messages content

        参数:
            prompt: 用户提示文本
            image_paths: 图片路径列表

        返回:
            List[dict]: 多模态内容列表
        """
        contents = [{"type": "text", "text": prompt}]

        if not image_paths:
            return contents

        # 确保 image_paths 是列表
        if isinstance(image_paths, str):
            image_paths = [image_paths]

        for i, image_path in enumerate(image_paths, 1):
            result = self._encode_image_to_base64(image_path)
            if result:
                b64, mime_type = result
                image_url = f"data:{mime_type};base64,{b64}"
                filename = self._get_filename(image_path)

                # 添加图片标注文本
                label = f"[图片{i}: {filename}]" if len(image_paths) > 1 else f"[图片: {filename}]"
                contents.append({"type": "text", "text": label})

                # 添加图片数据
                contents.append({
                    "type": "image_url",
                    "image_url": {"url": image_url},
                })
            else:
                logger.warning(f"图片 '{image_path}' 编码失败，已跳过。")

        return contents

    def _build_flexible_content(
        self,
        content_parts: List[Union[str, dict]]
    ) -> List[dict]:
        """
        构建灵活的多模态内容

        参数:
            content_parts: 内容部分列表

        返回:
            List[dict]: 多模态内容列表
        """
        contents = []
        image_count = 0

        for part in content_parts:
            if isinstance(part, str):
                # 文本部分
                contents.append({"type": "text", "text": part})
            elif isinstance(part, dict) and "image" in part:
                # 图片部分
                image_path = part["image"]
                result = self._encode_image_to_base64(image_path)

                if result:
                    image_count += 1
                    b64, mime_type = result
                    image_url = f"data:{mime_type};base64,{b64}"

                    # 获取标签
                    if "label" in part:
                        label = f"[{part['label']}]"
                    else:
                        filename = self._get_filename(image_path)
                        label = f"[图片{image_count}: {filename}]"

                    # 添加图片标签
                    contents.append({"type": "text", "text": label})

                    # 添加图片数据
                    contents.append({
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    })
                else:
                    logger.warning(f"图片 '{image_path}' 编码失败，已跳过。")
            else:
                # 未知类型，尝试转换为文本
                contents.append({"type": "text", "text": str(part)})

        return contents

    @timeout_decorator(DEFAULT_TIMEOUT)
    def _call_api(
        self,
        messages: List[dict],
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        """
        调用 Azure API（使用 OpenAI SDK，内置重试机制）

        参数:
            messages: 消息列表
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 模型响应文本
        """
        if self.verbose:
            logger.debug(f"使用模型: {self.model}")
            logger.debug(f"API 端点: {self.endpoint}")
            logger.debug(f"支持 Vision: {self.supports_vision}")

        try:
            if stream:
                # 流式输出
                full_response = ""
                stream_response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=True,
                )
                for chunk in stream_response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        if self.verbose:
                            logger.debug(content)
                if self.verbose:
                    pass  # 换行
                return full_response
            else:
                # 非流式输出
                completion = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=False,
                )
                return completion.choices[0].message.content or ""
                
        except Exception as e:
            logger.exception(f"调用 Azure API 时出错: {e}")
            raise

    def chat(
        self,
        prompt: str,
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        """
        发送纯文本聊天请求

        参数:
            prompt: 用户提示文本
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数，控制随机性

        返回:
            str: 模型的响应文本
        """
        if self.verbose:
            prompt_preview = prompt[:200] + "..." if len(prompt) > 200 else prompt
            logger.debug(f"用户输入: {prompt_preview}")

        messages = [{"role": "user", "content": prompt}]
        return self._call_api(messages, stream=stream, max_tokens=max_tokens, temperature=temperature)

    def chat_with_history(
        self,
        messages: List[dict],
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        """
        发送带历史记录的聊天请求

        参数:
            messages: 消息历史列表，格式为 [{"role": "user/assistant", "content": "..."}]
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数，控制随机性

        返回:
            str: 模型的响应文本
        """
        if self.verbose:
            logger.debug(f"消息数量: {len(messages)}")

        return self._call_api(messages, stream=stream, max_tokens=max_tokens, temperature=temperature)

    def chat_with_images(
        self,
        prompt: str,
        image_paths: Optional[Union[str, List[str]]] = None,
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        """
        发送带图片的聊天请求（简单模式：先文本后图片）

        注意：仅当模型支持 Vision 时才会处理图片，否则仅发送文本

        参数:
            prompt: 用户提示文本
            image_paths: 图片路径，可以是单个字符串或字符串列表
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 模型的响应文本
        """
        if not self.supports_vision:
            if self.verbose:
                logger.warning(f"警告: 模型 {self.model} 不支持 Vision，将忽略图片")
            return self.chat(prompt, stream=stream, max_tokens=max_tokens, temperature=temperature)

        # 确保 image_paths 是列表
        if isinstance(image_paths, str):
            image_paths = [image_paths]

        user_content = self._build_multimodal_message(prompt, image_paths)
        messages = [{"role": "user", "content": user_content}]

        return self._call_api(messages, stream=stream, max_tokens=max_tokens, temperature=temperature)

    def chat_multimodal(
        self,
        content_parts: List[Union[str, dict]],
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        """
        发送多模态聊天请求（高级模式：支持任意组合的文本和图片）

        注意：仅当模型支持 Vision 时才会处理图片，否则仅发送文本

        支持灵活的内容组合，例如：
        - 文本1 -> 图片1 -> 文本2 -> 图片2 -> ...

        参数:
            content_parts: 内容部分列表，每个元素可以是：
                - str: 文本内容
                - dict: 图片配置，格式为 {"image": "path/to/image.png"} 或
                        {"image": "path/to/image.png", "label": "自定义标签"}
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 模型的响应文本

        使用示例:
            # 示例 1: 文本 -> 图片 -> 文本 -> 图片
            response = client.chat_multimodal([
                "请分析以下两张股票K线图：",
                {"image": "stock1.png", "label": "股票A"},
                "上图是股票A的走势。下面是股票B：",
                {"image": "stock2.png", "label": "股票B"},
                "请对比分析这两只股票，给出投资建议。"
            ])
        """
        if not self.supports_vision:
            if self.verbose:
                logger.warning(f"警告: 模型 {self.model} 不支持 Vision，将仅发送文本内容")
            # 只提取文本内容
            text_parts = [part for part in content_parts if isinstance(part, str)]
            combined_text = "\n".join(text_parts)
            return self.chat(combined_text, stream=stream, max_tokens=max_tokens, temperature=temperature)

        contents = self._build_flexible_content(content_parts)
        messages = [{"role": "user", "content": contents}]
        return self._call_api(messages, stream=stream, max_tokens=max_tokens, temperature=temperature)