"""
GitHub Copilot 多模态客户端
支持文本和图片输入
"""
import base64
import json
import random
import time
import webbrowser
from typing import Optional, List, Union

import requests

from logger_config import logger
from .base import BaseLLMClient, VisionCapableMixin
from .config import (
    COPILOT_CLIENT_ID,
    COPILOT_DEVICE_CODE_URL,
    COPILOT_OAUTH_TOKEN_URL,
    COPILOT_COMPLETION_URL,
    COPILOT_AVAILABLE_MODELS,
    COPILOT_DEFAULT_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
)

# 默认重试配置
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_DELAY = 1.0  # 初始重试延迟（秒）
DEFAULT_RETRY_MULTIPLIER = 2.0  # 指数退避乘数
DEFAULT_RETRY_MAX_DELAY = 30.0  # 最大重试延迟（秒）


class CopilotVisionClient(BaseLLMClient, VisionCapableMixin):
    """
    GitHub Copilot 多模态客户端

    支持多种模型，包括 GPT、Claude、Gemini 等
    支持文本和图片输入
    支持自动重试机制
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
        初始化 Copilot 客户端

        参数:
            github_token: GitHub OAuth 令牌
            model: 使用的模型名称，如果为 None 则使用默认模型
            verbose: 是否打印详细日志
            max_retries: 最大重试次数，默认 3 次
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
        super().__init__(model)

    @property
    def default_model(self) -> str:
        return COPILOT_DEFAULT_MODEL

    @property
    def available_models(self) -> List[str]:
        return COPILOT_AVAILABLE_MODELS

    @staticmethod
    def get_github_token_interactive() -> Optional[str]:
        """
        通过设备流程交互式获取 GitHub OAuth 令牌

        返回:
            str: GitHub OAuth 令牌，失败返回 None
        """
        try:
            response = requests.post(
                COPILOT_DEVICE_CODE_URL,
                data={"client_id": COPILOT_CLIENT_ID, "scope": "copilot"},
                headers={"Accept": "application/json"},
                timeout=30
            )
            response.raise_for_status()
            device_data = response.json()

            user_code = device_data["user_code"]
            verification_uri = device_data["verification_uri"]
            device_code = device_data["device_code"]
            interval = device_data["interval"]

            logger.info(f"请在浏览器中打开: {verification_uri}")
            logger.info(f"并输入代码: {user_code}")
            webbrowser.open(verification_uri)

            while True:
                time.sleep(interval)
                token_response = requests.post(
                    COPILOT_OAUTH_TOKEN_URL,
                    data={
                        "client_id": COPILOT_CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
                    },
                    headers={"Accept": "application/json"},
                    timeout=30
                )

                token_data = token_response.json()
                if "access_token" in token_data:
                    logger.info("成功获取 GitHub 令牌。")
                    return token_data["access_token"]
                elif token_data.get("error") == "authorization_pending":
                    logger.info("等待用户授权...")
                    continue
                else:
                    logger.error(f"获取令牌失败: {token_data.get('error_description')}")
                    return None

        except requests.exceptions.RequestException as e:
            logger.exception(f"请求 GitHub 设备代码时出错: {e}")
            return None

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

    def _calculate_retry_delay(self, attempt: int) -> float:
        """
        计算重试延迟时间（指数退避 + 随机抖动）

        参数:
            attempt: 当前重试次数（从 0 开始）

        返回:
            float: 延迟时间（秒）
        """
        delay = self.retry_delay * (self.retry_multiplier ** attempt)
        # 添加随机抖动（±25%）
        jitter = delay * 0.25 * (random.random() * 2 - 1)
        delay = delay + jitter
        # 限制最大延迟
        return min(delay, self.retry_max_delay)

    def _call_api(
        self,
        messages: List[dict],
        stream: bool = True
    ) -> str:
        """
        调用 Copilot API（带重试机制）

        参数:
            messages: 消息列表
            stream: 是否使用流式输出

        返回:
            str: 模型响应文本
        """
        # 注意：Copilot API 需要始终设置 Copilot-Vision-Request 头部
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Content-Type": "application/json",
            "Copilot-Vision-Request": "true",
        }

        data = {
            "model": self.model,
            "messages": messages,
        }

        if self.verbose:
            logger.debug(f"使用模型: {self.model}")
            payload_preview = json.dumps(data, ensure_ascii=False)
            if len(payload_preview) > 500:
                logger.debug(f"请求数据预览: {payload_preview[:500]}...")
            else:
                logger.debug(f"请求数据: {payload_preview}")

        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    delay = self._calculate_retry_delay(attempt - 1)
                    if self.verbose:
                        logger.debug(f"第 {attempt} 次重试，等待 {delay:.2f} 秒...")
                    time.sleep(delay)

                response = requests.post(
                    COPILOT_COMPLETION_URL,
                    headers=headers,
                    json=data,
                    stream=stream,
                    timeout=DEFAULT_TIMEOUT
                )

                # 非 200 状态码，进行重试
                if response.status_code != 200:
                    error_msg = f"Copilot API 错误 [{response.status_code}]: {response.text}"
                    if self.verbose:
                        logger.warning(f"请求失败 (尝试 {attempt + 1}/{self.max_retries + 1}): {error_msg}")
                    last_error = Exception(error_msg)

                    # 如果还有重试机会，继续重试
                    if attempt < self.max_retries:
                        continue
                    else:
                        raise last_error

                # 成功响应，解析内容
                return self._parse_response(response, stream)

            except requests.exceptions.Timeout as e:
                last_error = e
                if self.verbose:
                    logger.warning(f"请求超时 (尝试 {attempt + 1}/{self.max_retries + 1})")
                if attempt >= self.max_retries:
                    logger.error("请求超时，已达到最大重试次数")
                    return ""

            except requests.exceptions.RequestException as e:
                last_error = e
                if self.verbose:
                    logger.warning(f"请求异常 (尝试 {attempt + 1}/{self.max_retries + 1}): {e}")
                if attempt >= self.max_retries:
                    logger.exception(f"调用 Copilot API 时出错: {e}")
                    return ""

            except Exception as e:
                last_error = e
                if self.verbose:
                    logger.warning(f"未知错误 (尝试 {attempt + 1}/{self.max_retries + 1}): {e}")
                if attempt >= self.max_retries:
                    logger.exception(f"调用 Copilot API 时发生未知错误: {e}")
                    return ""

        # 不应该到达这里，但为了安全起见
        if last_error:
            raise last_error
        return ""

    def _parse_response(self, response: requests.Response, stream: bool) -> str:
        """
        解析 API 响应

        参数:
            response: HTTP 响应对象
            stream: 是否是流式响应

        返回:
            str: 解析后的响应文本
        """
        full_response = ""
        response_text = response.text.strip()

        # 检查是否是直接的 JSON 响应（非流式）
        if response_text.startswith('{'):
            try:
                response_data = json.loads(response_text)
                full_response = (
                    response_data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
            except json.JSONDecodeError:
                full_response = response_text
        else:
            # 流式 SSE 响应处理
            for line in response.iter_lines():
                if line:
                    decoded_line = line.decode('utf-8')
                    if decoded_line.startswith('data: '):
                        json_str = decoded_line[6:]
                        if json_str.strip() == '[DONE]':
                            break
                        try:
                            json_data = json.loads(json_str)
                            content = (
                                json_data.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content")
                            )
                            if content:
                                full_response += content
                        except json.JSONDecodeError:
                            if self.verbose:
                                logger.warning(f"无法解析的 JSON: {json_str}")

        return full_response

    def chat(
        self,
        prompt: str,
        stream: bool = True,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        """
        发送纯文本聊天请求

        参数:
            prompt: 用户提示文本
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数（当前 API 不支持）
            temperature: 温度参数（当前 API 不支持）

        返回:
            str: 模型的响应文本
        """
        # 使用多模态消息格式，即使是纯文本也需要
        user_content = self._build_multimodal_message(prompt, None)
        messages = [{"role": "user", "content": user_content}]
        return self._call_api(messages, stream=stream)

    def chat_with_images(
        self,
        prompt: str,
        image_paths: Optional[Union[str, List[str]]] = None,
        stream: bool = True,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        """
        发送带图片的聊天请求（简单模式：先文本后图片）

        参数:
            prompt: 用户提示文本
            image_paths: 图片路径，可以是单个字符串或字符串列表
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数（当前 API 不支持）
            temperature: 温度参数（当前 API 不支持）

        返回:
            str: 模型的响应文本
        """
        # 确保 image_paths 是列表
        if isinstance(image_paths, str):
            image_paths = [image_paths]

        user_content = self._build_multimodal_message(prompt, image_paths)
        messages = [{"role": "user", "content": user_content}]

        return self._call_api(messages, stream=stream)

    def chat_multimodal(
        self,
        content_parts: List[Union[str, dict]],
        stream: bool = True,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
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
            max_tokens: 最大生成 token 数（当前 API 不支持）
            temperature: 温度参数（当前 API 不支持）

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

            # 示例 2: 多图对比
            response = client.chat_multimodal([
                "对比以下图片的差异：",
                {"image": "before.png"},
                {"image": "after.png"},
            ])
        """
        contents = self._build_flexible_content(content_parts)
        messages = [{"role": "user", "content": contents}]
        return self._call_api(messages, stream=stream)

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

    def chat_with_raw_content(
        self,
        content: List[dict],
        stream: bool = True,
        **kwargs
    ) -> str:
        """
        使用原始内容格式发送请求（最灵活的方式）

        参数:
            content: 原始内容列表，直接传递给 API
                格式: [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "..."}}]
            stream: 是否使用流式输出

        返回:
            str: 模型的响应文本

        使用示例:
            response = client.chat_with_raw_content([
                {"type": "text", "text": "分析这张图片"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
            ])
        """
        messages = [{"role": "user", "content": content}]
        return self._call_api(messages, stream=stream)
