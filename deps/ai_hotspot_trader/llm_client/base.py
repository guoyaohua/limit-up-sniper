"""
LLM 客户端基类定义
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any


class BaseLLMClient(ABC):
    """LLM 客户端抽象基类"""

    def __init__(self, model: Optional[str] = None):
        """
        初始化客户端

        参数:
            model: 使用的模型名称，如果为 None 则使用默认模型
        """
        self.model = model or self.default_model

    @property
    @abstractmethod
    def default_model(self) -> str:
        """返回默认模型名称"""
        pass

    @property
    @abstractmethod
    def available_models(self) -> List[str]:
        """返回可用模型列表"""
        pass

    @abstractmethod
    def chat(
        self,
        prompt: str,
        stream: bool = True,
        max_tokens: int = 8192,
        temperature: float = 0.7,
        **kwargs
    ) -> str:
        """
        发送聊天请求

        参数:
            prompt: 用户提示文本
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 模型的响应文本
        """
        pass

    def list_models(self) -> List[str]:
        """返回可用模型列表"""
        return self.available_models


class VisionCapableMixin:
    """支持视觉/多模态输入的 Mixin 类"""

    @abstractmethod
    def chat_with_images(
        self,
        prompt: str,
        image_paths: Optional[List[str]] = None,
        stream: bool = True,
        max_tokens: int = 8192,
        temperature: float = 0.7,
        **kwargs
    ) -> str:
        """
        发送带图片的聊天请求

        参数:
            prompt: 用户提示文本
            image_paths: 图片路径列表
            stream: 是否使用流式输出
            max_tokens: 最大生成 token 数
            temperature: 温度参数

        返回:
            str: 模型的响应文本
        """
        pass
