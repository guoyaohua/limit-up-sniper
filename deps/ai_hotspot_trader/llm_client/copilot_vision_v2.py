"""
GitHub Copilot 多模态客户端 V2
使用 LiteLLM 接口调用 Copilot 模型，替代直接 requests 调用

LiteLLM 统一了不同 LLM 提供商的 API 调用接口，
通过 github_copilot/ 前缀路由到 GitHub Copilot API。
"""
import base64
import os
import random
import time
from typing import Optional, List, Union

import litellm

from logger_config import logger
from .base import BaseLLMClient, VisionCapableMixin
from .config import (
    COPILOT_AVAILABLE_MODELS,
    COPILOT_DEFAULT_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    DEFAULT_TOP_P,
)

# 默认重试配置
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_RETRY_MULTIPLIER = 2.0
DEFAULT_RETRY_MAX_DELAY = 30.0

# LiteLLM 模型前缀
LITELLM_COPILOT_PREFIX = "github_copilot"

# 需要使用 Responses API 的模型关键词（如 codex 系列）
RESPONSES_API_MODEL_KEYWORDS = ["codex", "gpt-5.4"]

litellm.suppress_debug_info = True
litellm.drop_params = True

class CopilotVisionClientV2(BaseLLMClient, VisionCapableMixin):
    """
    GitHub Copilot 多模态客户端 V2（基于 LiteLLM）

    与 CopilotVisionClient 接口一致，但使用 LiteLLM 的 completion() 替代
    requests.post 直接调用 Copilot API。

    优势:
    - 统一的 API 接口，与其他 LLM 提供商一致
    - 内置的错误处理和重试逻辑
    - 更好的流式响应支持
    - 自动 token 使用统计
    """

    def __init__(
        self,
        github_token: str,
        model: Optional[str] = None,
        verbose: bool = False,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        retry_multiplier: float = DEFAULT_RETRY_MULTIPLIER,
        retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
    ):
        """
        初始化 Copilot Vision V2 客户端

        参数:
            github_token: GitHub OAuth 令牌
            model: 使用的模型名称，如果为 None 则使用默认模型
            verbose: 是否打印详细日志
            max_retries: 最大重试次数，默认 5 次
            retry_delay: 初始重试延迟（秒），默认 1 秒
            retry_multiplier: 指数退避乘数，默认 2.0
            retry_max_delay: 最大重试延迟（秒），默认 30 秒
        """
        self.github_token = github_token
        self.verbose = verbose
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.retry_multiplier = retry_multiplier
        self.retry_max_delay = retry_max_delay

        # 设置环境变量供 LiteLLM 使用
        os.environ["GITHUB_TOKEN"] = self.github_token

        super().__init__(model)

        logger.info(
            f"CopilotVisionClientV2 初始化完成，模型: {self.model}, "
            f"LiteLLM 模型: {self.litellm_model}"
        )

    @property
    def default_model(self) -> str:
        return COPILOT_DEFAULT_MODEL

    @property
    def available_models(self) -> List[str]:
        return COPILOT_AVAILABLE_MODELS

    @property
    def litellm_model(self) -> str:
        """返回 LiteLLM 格式的模型名称（带 github_copilot/ 前缀）"""
        return f"{LITELLM_COPILOT_PREFIX}/{self.model}"

    @property
    def use_responses_api(self) -> bool:
        """判断当前模型是否需要使用 Responses API（如 codex 系列模型）"""
        model_lower = self.model.lower()
        return any(kw in model_lower for kw in RESPONSES_API_MODEL_KEYWORDS)

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

            ext = image_path.lower().split(".")[-1]
            mime_type = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "gif": "image/gif",
                "webp": "image/webp",
            }.get(ext, "image/jpeg")

            return b64_data, mime_type
        except OSError as e:
            logger.exception(f"读取图片失败: {e}")
            return None

    @staticmethod
    def _get_filename(image_path: str) -> str:
        """从路径中提取文件名"""
        return image_path.replace("\\", "/").split("/")[-1]

    def _build_multimodal_message(
        self,
        prompt: str,
        image_paths: Optional[List[str]] = None,
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

        if isinstance(image_paths, str):
            image_paths = [image_paths]

        for i, image_path in enumerate(image_paths, 1):
            result = self._encode_image_to_base64(image_path)
            if result:
                b64, mime_type = result
                image_url = f"data:{mime_type};base64,{b64}"
                filename = self._get_filename(image_path)

                label = (
                    f"[图片{i}: {filename}]"
                    if len(image_paths) > 1
                    else f"[图片: {filename}]"
                )
                contents.append({"type": "text", "text": label})
                contents.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    }
                )
            else:
                logger.warning(f"图片 '{image_path}' 编码失败，已跳过。")

        return contents

    def _build_flexible_content(
        self,
        content_parts: List[Union[str, dict]],
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
                contents.append({"type": "text", "text": part})
            elif isinstance(part, dict) and "image" in part:
                image_path = part["image"]
                result = self._encode_image_to_base64(image_path)

                if result:
                    image_count += 1
                    b64, mime_type = result
                    image_url = f"data:{mime_type};base64,{b64}"

                    if "label" in part:
                        label = f"[{part['label']}]"
                    else:
                        filename = self._get_filename(image_path)
                        label = f"[图片{image_count}: {filename}]"

                    contents.append({"type": "text", "text": label})
                    contents.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        }
                    )
                else:
                    logger.warning(f"图片 '{image_path}' 编码失败，已跳过。")
            else:
                contents.append({"type": "text", "text": str(part)})

        return contents

    def _calculate_retry_delay(self, attempt: int) -> float:
        """
        计算重试延迟时间（指数退避 + 随机抖动）

        参数:
            attempt: 当前重试次数（从 0 开始）

        返回:
            float: 延迟时间（秒）
        """
        delay = self.retry_delay * (self.retry_multiplier**attempt)
        jitter = delay * 0.25 * (random.random() * 2 - 1)
        delay = delay + jitter
        return min(delay, self.retry_max_delay)

    def _call_api(
        self,
        messages: List[dict],
        stream: bool = True,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
    ) -> str:
        """
        通过 LiteLLM 调用 Copilot API（带重试机制）

        自动根据模型类型选择合适的 API：
        - 普通模型（gpt-4o, claude 等）: 使用 litellm.completion() Chat Completion API
        - Codex 模型（gpt-5.1-codex 等）: 使用 litellm.responses() Responses API

        参数:
            messages: 消息列表
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 模型响应文本
        """
        if self.verbose:
            logger.debug(f"使用模型: {self.litellm_model}")
            logger.debug(f"流式输出: {stream}")
            logger.debug(f"使用 Responses API: {self.use_responses_api}")

        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    delay = self._calculate_retry_delay(attempt - 1)
                    if self.verbose:
                        logger.debug(f"第 {attempt} 次重试，等待 {delay:.2f} 秒...")
                    time.sleep(delay)

                # 根据模型类型选择 API
                if self.use_responses_api:
                    return self._call_responses_api(messages, max_tokens, temperature, top_p)
                elif stream:
                    return self._call_api_stream(messages, max_tokens, temperature, top_p)
                else:
                    return self._call_api_non_stream(messages, max_tokens, temperature, top_p)

            except TimeoutError as e:
                last_error = e
                if self.verbose:
                    logger.warning(
                        f"请求超时 (尝试 {attempt + 1}/{self.max_retries + 1})"
                    )
                if attempt >= self.max_retries:
                    logger.error("请求超时，已达到最大重试次数")
                    return ""

            except Exception as e:
                last_error = e
                error_msg = str(e)
                if self.verbose:
                    logger.warning(
                        f"API 调用失败 (尝试 {attempt + 1}/{self.max_retries + 1}): {error_msg}"
                    )
                if attempt >= self.max_retries:
                    logger.exception(f"调用 Copilot API (LiteLLM) 时出错: {e}")
                    return ""

        if last_error:
            raise last_error
        return ""

    def _call_responses_api(
        self,
        messages: List[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
    ) -> str:
        """
        使用 LiteLLM Responses API 调用（用于 Codex 等模型）

        参数:
            messages: 消息列表（将转换为 Responses API 的 input 格式）
            max_tokens: 最大生成 token 数
            temperature: 温度参数
            top_p: top_p 参数

        返回:
            str: 模型响应文本
        """
        # 将 Chat Completion 格式的 messages 转换为 Responses API 的 input
        input_content = self._convert_messages_to_responses_input(messages)

        if self.verbose:
            input_preview = str(input_content)[:200]
            logger.debug(f"Responses API input: {input_preview}...")

        response = litellm.responses(
            model=self.litellm_model,
            input=input_content,
            max_output_tokens=max_tokens,
            timeout=DEFAULT_TIMEOUT,
        )

        # 从 Responses API 响应中提取文本
        result = self._extract_responses_text(response)

        # 记录 token 使用情况
        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            input_tokens = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", 0) or (input_tokens + output_tokens)
            logger.info(
                f"Token 使用 - input: {input_tokens}, "
                f"output: {output_tokens}, "
                f"total: {total_tokens}"
            )

        if self.verbose:
            logger.debug(f"响应长度: {len(result)}")

        return result

    def _convert_messages_to_responses_input(self, messages: List[dict]) -> Union[str, List[dict]]:
        """
        将 Chat Completion 格式的 messages 转换为 Responses API 的 input 格式

        参数:
            messages: Chat Completion 格式的消息列表

        返回:
            Responses API 的 input 参数
        """
        # 如果只有一条用户消息且 content 是纯文本字符串，直接返回文本
        if len(messages) == 1 and messages[0]["role"] == "user":
            content = messages[0]["content"]
            if isinstance(content, str):
                return content
            # 如果 content 是列表（多模态），转换格式
            if isinstance(content, list):
                # 检查是否只有一个文本元素
                if len(content) == 1 and content[0].get("type") == "text":
                    return content[0]["text"]
                # 多模态内容，转换为 Responses API 格式
                return self._convert_content_to_responses_format(messages)

        # 多条消息，使用完整的消息格式
        return self._convert_content_to_responses_format(messages)

    def _convert_content_to_responses_format(self, messages: List[dict]) -> List[dict]:
        """
        将多条消息转换为 Responses API 的 input 格式

        参数:
            messages: Chat Completion 格式的消息列表

        返回:
            Responses API 格式的 input 列表
        """
        responses_input = []

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                responses_input.append({
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": content}],
                })
            elif isinstance(content, list):
                # 转换多模态内容
                converted_content = []
                for item in content:
                    if item.get("type") == "text":
                        converted_content.append({
                            "type": "input_text",
                            "text": item["text"],
                        })
                    elif item.get("type") == "image_url":
                        image_url = item.get("image_url", {}).get("url", "")
                        converted_content.append({
                            "type": "input_image",
                            "image_url": image_url,
                        })
                responses_input.append({
                    "type": "message",
                    "role": role,
                    "content": converted_content,
                })

        return responses_input

    def _extract_responses_text(self, response) -> str:
        """
        从 Responses API 的响应中提取文本内容

        参数:
            response: LiteLLM Responses API 的响应对象

        返回:
            str: 提取的文本内容
        """
        result = ""

        # Responses API 返回的格式与 Chat Completion 不同
        # 尝试多种方式提取文本
        if hasattr(response, "output"):
            output = response.output
            if isinstance(output, str):
                result = output
            elif isinstance(output, list):
                for item in output:
                    if hasattr(item, "content"):
                        content = item.content
                        if isinstance(content, str):
                            result += content
                        elif isinstance(content, list):
                            for c in content:
                                if hasattr(c, "text"):
                                    result += c.text
                    elif hasattr(item, "text"):
                        result += item.text
        elif hasattr(response, "choices") and response.choices:
            result = response.choices[0].message.content or ""

        return result

    def _call_api_non_stream(
        self,
        messages: List[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
    ) -> str:
        """
        非流式调用 LiteLLM

        参数:
            messages: 消息列表
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 模型响应文本
        """
        response = litellm.completion(
            model=self.litellm_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            timeout=DEFAULT_TIMEOUT,
        )

        result = response.choices[0].message.content or ""

        # 记录 token 使用情况
        if hasattr(response, "usage") and response.usage:
            logger.info(
                f"Token 使用 - prompt: {response.usage.prompt_tokens}, "
                f"completion: {response.usage.completion_tokens}, "
                f"total: {response.usage.total_tokens}"
            )

        if self.verbose:
            logger.debug(f"响应长度: {len(result)}")

        return result

    def _call_api_stream(
        self,
        messages: List[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
    ) -> str:
        """
        流式调用 LiteLLM

        参数:
            messages: 消息列表
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 完整的模型响应文本
        """
        stream_response = litellm.completion(
            model=self.litellm_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=True,
            timeout=DEFAULT_TIMEOUT,
        )

        full_response = ""
        for chunk in stream_response:
            if chunk.choices and chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_response += content
                if self.verbose:
                    logger.debug(content)

        if self.verbose:
            logger.debug(f"流式响应完成，总长度: {len(full_response)}")

        return full_response

    def chat(
        self,
        prompt: str,
        stream: bool = True,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs,
    ) -> str:
        """
        发送纯文本聊天请求

        参数:
            prompt: 用户提示文本
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 模型的响应文本
        """
        user_content = self._build_multimodal_message(prompt, None)
        messages = [{"role": "user", "content": user_content}]
        return self._call_api(messages, stream=stream, max_tokens=max_tokens, temperature=temperature, top_p=DEFAULT_TOP_P)

    def chat_with_images(
        self,
        prompt: str,
        image_paths: Optional[Union[str, List[str]]] = None,
        stream: bool = True,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs,
    ) -> str:
        """
        发送带图片的聊天请求（简单模式：先文本后图片）

        参数:
            prompt: 用户提示文本
            image_paths: 图片路径，可以是单个字符串或字符串列表
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 模型的响应文本
        """
        if isinstance(image_paths, str):
            image_paths = [image_paths]

        user_content = self._build_multimodal_message(prompt, image_paths)
        messages = [{"role": "user", "content": user_content}]
        return self._call_api(messages, stream=stream, max_tokens=max_tokens, temperature=temperature, top_p=DEFAULT_TOP_P)

    def chat_multimodal(
        self,
        content_parts: List[Union[str, dict]],
        stream: bool = True,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs,
    ) -> str:
        """
        发送多模态聊天请求（高级模式：支持任意组合的文本和图片）

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
            response = client.chat_multimodal([
                "请分析以下两张股票K线图：",
                {"image": "stock1.png", "label": "股票A"},
                "上图是股票A的走势。下面是股票B：",
                {"image": "stock2.png", "label": "股票B"},
                "请对比分析这两只股票，给出投资建议。"
            ])
        """
        contents = self._build_flexible_content(content_parts)
        messages = [{"role": "user", "content": contents}]
        return self._call_api(messages, stream=stream, max_tokens=max_tokens, temperature=temperature, top_p=DEFAULT_TOP_P)

    def chat_with_raw_content(
        self,
        content: List[dict],
        stream: bool = True,
        **kwargs,
    ) -> str:
        """
        使用原始内容格式发送请求（最灵活的方式）

        参数:
            content: 原始内容列表，直接传递给 API
                格式: [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "..."}}]
            stream: 是否使用流式输出

        返回:
            str: 模型的响应文本
        """
        messages = [{"role": "user", "content": content}]
        return self._call_api(messages, stream=stream)