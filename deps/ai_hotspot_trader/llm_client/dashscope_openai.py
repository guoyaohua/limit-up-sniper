"""
阿里云百炼（DashScope Coding）OpenAI 兼容客户端

使用 OpenAI SDK 调用阿里云百炼的 OpenAI 兼容接口，支持：
- 纯文本对话
- 多模态图片对话（仅 Vision 模型）
"""
import base64
from typing import Optional, List, Union

import httpx
from openai import OpenAI

from logger_config import logger
from .base import BaseLLMClient, VisionCapableMixin
from .config import (
    DASHSCOPE_ENDPOINT,
    DASHSCOPE_API_KEY,
    DASHSCOPE_DEFAULT_MODEL,
    DASHSCOPE_AVAILABLE_MODELS,
    DASHSCOPE_VISION_MODELS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
)

DEFAULT_MAX_RETRIES = 5


class DashScopeOpenAIClient(BaseLLMClient, VisionCapableMixin):
    """阿里云百炼 OpenAI 兼容客户端"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        verbose: bool = False,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key or DASHSCOPE_API_KEY
        self.endpoint = endpoint or DASHSCOPE_ENDPOINT
        self.verbose = verbose
        self.timeout = timeout

        http_timeout = httpx.Timeout(
            connect=30.0,
            read=timeout,
            write=30.0,
            pool=10.0,
        )

        self._client = OpenAI(
            base_url=self.endpoint,
            api_key=self.api_key,
            max_retries=max_retries,
            timeout=http_timeout,
        )

        super().__init__(model)

    @property
    def default_model(self) -> str:
        return DASHSCOPE_DEFAULT_MODEL

    @property
    def available_models(self) -> List[str]:
        return DASHSCOPE_AVAILABLE_MODELS

    @property
    def supports_vision(self) -> bool:
        return self.model in DASHSCOPE_VISION_MODELS

    @staticmethod
    def _encode_image_to_base64(image_path: str) -> Optional[tuple]:
        """把本地图片编码成 base64"""
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
        return image_path.replace("\\", "/").split("/")[-1]

    def _build_multimodal_message(
        self,
        prompt: str,
        image_paths: Optional[Union[str, List[str]]] = None
    ) -> List[dict]:
        contents = [{"type": "text", "text": prompt}]

        if not image_paths:
            return contents

        if isinstance(image_paths, str):
            image_paths = [image_paths]

        for i, image_path in enumerate(image_paths, 1):
            result = self._encode_image_to_base64(image_path)
            if not result:
                logger.warning(f"图片 '{image_path}' 编码失败，已跳过。")
                continue

            b64, mime_type = result
            image_url = f"data:{mime_type};base64,{b64}"
            filename = self._get_filename(image_path)
            label = f"[图片{i}: {filename}]" if len(image_paths) > 1 else f"[图片: {filename}]"

            contents.append({"type": "text", "text": label})
            contents.append({
                "type": "image_url",
                "image_url": {"url": image_url},
            })

        return contents

    def _build_flexible_content(self, content_parts: List[Union[str, dict]]) -> List[dict]:
        contents = []
        image_count = 0

        for part in content_parts:
            if isinstance(part, str):
                contents.append({"type": "text", "text": part})
                continue

            if isinstance(part, dict) and "image" in part:
                result = self._encode_image_to_base64(part["image"])
                if not result:
                    logger.warning(f"图片 '{part['image']}' 编码失败，已跳过。")
                    continue

                image_count += 1
                b64, mime_type = result
                image_url = f"data:{mime_type};base64,{b64}"
                if "label" in part:
                    label = f"[{part['label']}]"
                else:
                    filename = self._get_filename(part["image"])
                    label = f"[图片{image_count}: {filename}]"

                contents.append({"type": "text", "text": label})
                contents.append({
                    "type": "image_url",
                    "image_url": {"url": image_url},
                })
            else:
                contents.append({"type": "text", "text": str(part)})

        return contents

    def _call_api(
        self,
        messages: List[dict],
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        if self.verbose:
            logger.debug(f"[DashScope] 使用模型: {self.model}")
            logger.debug(f"[DashScope] API 端点: {self.endpoint}")
            logger.debug(f"[DashScope] 支持 Vision: {self.supports_vision}")

        try:
            if stream:
                full_response = ""
                stream_response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=True,
                    timeout=self.timeout,
                )
                for chunk in stream_response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        if self.verbose:
                            logger.debug(content)
                return full_response

            completion = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=False,
                timeout=self.timeout,
            )
            return completion.choices[0].message.content or ""
        except Exception as e:
            logger.exception(f"调用 DashScope API 时出错: {e}")
            raise

    def chat(
        self,
        prompt: str,
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        messages = [{"role": "user", "content": prompt}]
        return self._call_api(
            messages,
            stream=stream,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def chat_with_history(
        self,
        messages: List[dict],
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        return self._call_api(
            messages,
            stream=stream,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def chat_with_images(
        self,
        prompt: str,
        image_paths: Optional[Union[str, List[str]]] = None,
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        if not self.supports_vision:
            if self.verbose:
                logger.warning(f"模型 {self.model} 不支持 Vision，将忽略图片")
            return self.chat(
                prompt,
                stream=stream,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        user_content = self._build_multimodal_message(prompt, image_paths)
        messages = [{"role": "user", "content": user_content}]
        return self._call_api(
            messages,
            stream=stream,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def chat_multimodal(
        self,
        content_parts: List[Union[str, dict]],
        stream: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs
    ) -> str:
        if not self.supports_vision:
            text_parts = [part for part in content_parts if isinstance(part, str)]
            combined_text = "\n".join(text_parts)
            return self.chat(
                combined_text,
                stream=stream,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        contents = self._build_flexible_content(content_parts)
        messages = [{"role": "user", "content": contents}]
        return self._call_api(
            messages,
            stream=stream,
            max_tokens=max_tokens,
            temperature=temperature,
        )