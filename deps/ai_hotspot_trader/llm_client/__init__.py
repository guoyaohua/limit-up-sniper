"""
LLM 客户端模块

提供统一的 LLM 调用接口，支持：
- GitHub Copilot 多模态客户端（支持文本和图片输入）
- Azure OpenAI 兼容客户端（DeepSeek、Grok 等）

使用示例:

1. 使用 Copilot Vision 客户端（支持图片）:
    ```python
    from llm_client import CopilotVisionClient

    # 初始化客户端
    client = CopilotVisionClient(
        github_token='<redacted>',
        model="gemini-3-pro-preview"  # 可选，默认使用配置中的默认模型
    )

    # 纯文本对话
    response = client.chat("你好，请介绍一下自己")

    # 带图片的对话
    response = client.chat_with_images(
        prompt="请分析这张图片",
        image_paths=["image1.png", "image2.jpg"]
    )
    ```

2. 使用 Azure OpenAI 客户端（通用）:
    ```python
    from llm_client import AzureOpenAIClient

    # 初始化客户端（使用默认配置，默认 DeepSeek 模型）
    client = AzureOpenAIClient()

    # 或者指定模型
    client = AzureOpenAIClient(
        api_key='<redacted>',
        endpoint="your_endpoint",
        model="grok-4-fast-reasoning",  # 指定 Grok 模型（支持 Vision）
        verbose=True
    )

    # 发送聊天请求
    response = client.chat("请解释一下什么是量化投资")

    # 带图片的对话（仅支持 Vision 模型如 Grok）
    response = client.chat_with_images(
        prompt="请分析这张K线图",
        image_paths=["chart.png"]
    )
    ```

3. 快捷函数:
    ```python
    from llm_client import chat_with_copilot, chat_with_azure, chat_with_azure_vision

    # 使用 Copilot
    response = chat_with_copilot("你好", github_token='<redacted>')

    # 使用 Azure（默认 DeepSeek）
    response = chat_with_azure("你好")

    # 使用 Azure Vision 模型（如 Grok，带图片）
    response = chat_with_azure_vision(
        prompt="分析图片",
        image_paths=["img.png"],
        model="grok-4-fast-reasoning"
    )
    ```
"""

from .base import BaseLLMClient, VisionCapableMixin
from .copilot_vision import CopilotVisionClient
from .copilot_vision_v2 import CopilotVisionClientV2
from .azure_openai import AzureOpenAIClient
from .dashscope_openai import DashScopeOpenAIClient
from .config import (
    COPILOT_AVAILABLE_MODELS,
    COPILOT_DEFAULT_MODEL,
    AZURE_DEFAULT_MODEL,
    AZURE_AVAILABLE_MODELS,
    AZURE_TEXT_MODELS,
    AZURE_VISION_MODELS,
    DASHSCOPE_DEFAULT_MODEL,
    DASHSCOPE_AVAILABLE_MODELS,
    DASHSCOPE_TEXT_MODELS,
    DASHSCOPE_VISION_MODELS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
)

# 版本信息
__version__ = "1.2.0"
__author__ = "AI Hotspot Trader"

# 导出的公共接口
__all__ = [
    # 客户端类
    "BaseLLMClient",
    "VisionCapableMixin",
    "CopilotVisionClient",
    "CopilotVisionClientV2",
    "AzureOpenAIClient",
    "DashScopeOpenAIClient",
    # 配置常量
    "COPILOT_AVAILABLE_MODELS",
    "COPILOT_DEFAULT_MODEL",
    "AZURE_DEFAULT_MODEL",
    "AZURE_AVAILABLE_MODELS",
    "AZURE_TEXT_MODELS",
    "AZURE_VISION_MODELS",
    "DASHSCOPE_DEFAULT_MODEL",
    "DASHSCOPE_AVAILABLE_MODELS",
    "DASHSCOPE_TEXT_MODELS",
    "DASHSCOPE_VISION_MODELS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    # 快捷函数
    "chat_with_copilot",
    "chat_with_copilot_vision",
    "chat_with_copilot_v2",
    "chat_with_copilot_vision_v2",
    "chat_with_azure",
    "chat_with_azure_vision",
    "chat_with_dashscope",
    "chat_with_dashscope_vision",
]

def chat_with_copilot(
    prompt: str,
    github_token: str,
    model: str = COPILOT_DEFAULT_MODEL,
    stream: bool = True,
    verbose: bool = False
) -> str:
    """
    使用 Copilot 进行纯文本对话的快捷函数

    参数:
        prompt: 用户提示文本
        github_token: GitHub OAuth 令牌
        model: 使用的模型名称
        stream: 是否使用流式输出
        verbose: 是否打印详细日志

    返回:
        str: 模型的响应文本
    """
    client = CopilotVisionClient(
        github_token=github_token,
        model=model,
        verbose=verbose
    )
    return client.chat(prompt, stream=stream)

def chat_with_copilot_vision(
    prompt: str,
    github_token: str,
    image_paths: list = None,
    model: str = COPILOT_DEFAULT_MODEL,
    stream: bool = True,
    verbose: bool = False
) -> str:
    """
    使用 Copilot 进行带图片对话的快捷函数

    参数:
        prompt: 用户提示文本
        github_token: GitHub OAuth 令牌
        image_paths: 图片路径列表
        model: 使用的模型名称
        stream: 是否使用流式输出
        verbose: 是否打印详细日志

    返回:
        str: 模型的响应文本
    """
    client = CopilotVisionClient(
        github_token=github_token,
        model=model,
        verbose=verbose
    )
    return client.chat_with_images(prompt, image_paths=image_paths, stream=stream)

def chat_with_copilot_v2(
    prompt: str,
    github_token: str,
    model: str = COPILOT_DEFAULT_MODEL,
    stream: bool = True,
    verbose: bool = False
) -> str:
    """
    使用 Copilot V2 (LiteLLM) 进行纯文本对话的快捷函数

    参数:
        prompt: 用户提示文本
        github_token: GitHub OAuth 令牌
        model: 使用的模型名称
        stream: 是否使用流式输出
        verbose: 是否打印详细日志

    返回:
        str: 模型的响应文本
    """
    client = CopilotVisionClientV2(
        github_token=github_token,
        model=model,
        verbose=verbose
    )
    return client.chat(prompt, stream=stream)

def chat_with_copilot_vision_v2(
    prompt: str,
    github_token: str,
    image_paths: list = None,
    model: str = COPILOT_DEFAULT_MODEL,
    stream: bool = True,
    verbose: bool = False
) -> str:
    """
    使用 Copilot V2 (LiteLLM) 进行带图片对话的快捷函数

    参数:
        prompt: 用户提示文本
        github_token: GitHub OAuth 令牌
        image_paths: 图片路径列表
        model: 使用的模型名称
        stream: 是否使用流式输出
        verbose: 是否打印详细日志

    返回:
        str: 模型的响应文本
    """
    client = CopilotVisionClientV2(
        github_token=github_token,
        model=model,
        verbose=verbose
    )
    return client.chat_with_images(prompt, image_paths=image_paths, stream=stream)

def chat_with_azure(
    prompt: str,
    api_key: str = None,
    endpoint: str = None,
    model: str = AZURE_DEFAULT_MODEL,
    stream: bool = True,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    verbose: bool = False
) -> str:
    """
    使用 Azure OpenAI 兼容模型进行纯文本对话的快捷函数

    参数:
        prompt: 用户提示文本
        api_key: Azure API 密钥（可选，默认使用配置文件中的值）
        endpoint: Azure 端点（可选，默认使用配置文件中的值）
        model: 使用的模型名称（默认 DeepSeek）
        stream: 是否使用流式输出
        max_tokens: 最大生成 token 数
        temperature: 温度参数
        verbose: 是否打印详细日志

    返回:
        str: 模型的响应文本
    """
    client = AzureOpenAIClient(
        api_key=api_key,
        endpoint=endpoint,
        model=model,
        verbose=verbose
    )
    return client.chat(
        prompt,
        stream=stream,
        max_tokens=max_tokens,
        temperature=temperature
    )

def chat_with_azure_vision(
    prompt: str,
    image_paths: list = None,
    api_key: str = None,
    endpoint: str = None,
    model: str = "grok-4-fast-reasoning",
    stream: bool = True,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    verbose: bool = False
) -> str:
    """
    使用 Azure Vision 模型进行带图片对话的快捷函数

    参数:
        prompt: 用户提示文本
        image_paths: 图片路径列表
        api_key: Azure API 密钥（可选，默认使用配置文件中的值）
        endpoint: Azure 端点（可选，默认使用配置文件中的值）
        model: 使用的模型名称（默认 grok-4-fast-reasoning）
        stream: 是否使用流式输出
        max_tokens: 最大生成 token 数
        temperature: 温度参数
        verbose: 是否打印详细日志

    返回:
        str: 模型的响应文本
    """
    client = AzureOpenAIClient(
        api_key=api_key,
        endpoint=endpoint,
        model=model,
        verbose=verbose
    )
    return client.chat_with_images(
        prompt,
        image_paths=image_paths,
        stream=stream,
        max_tokens=max_tokens,
        temperature=temperature
    )

def chat_with_dashscope(
    prompt: str,
    api_key: str = None,
    endpoint: str = None,
    model: str = DASHSCOPE_DEFAULT_MODEL,
    stream: bool = True,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    verbose: bool = False
) -> str:
    """
    使用阿里云百炼 OpenAI 兼容模型进行纯文本对话的快捷函数

    参数:
        prompt: 用户提示文本
        api_key: DashScope API 密钥（可选，默认使用配置文件中的值）
        endpoint: DashScope 端点（可选，默认使用配置文件中的值）
        model: 使用的模型名称
        stream: 是否使用流式输出
        max_tokens: 最大生成 token 数
        temperature: 温度参数
        verbose: 是否打印详细日志

    返回:
        str: 模型的响应文本
    """
    client = DashScopeOpenAIClient(
        api_key=api_key,
        endpoint=endpoint,
        model=model,
        verbose=verbose
    )
    return client.chat(
        prompt,
        stream=stream,
        max_tokens=max_tokens,
        temperature=temperature
    )

def chat_with_dashscope_vision(
    prompt: str,
    image_paths: list = None,
    api_key: str = None,
    endpoint: str = None,
    model: str = "qwen3.5-plus",
    stream: bool = True,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    verbose: bool = False
) -> str:
    """
    使用阿里云百炼 Vision 模型进行带图片对话的快捷函数

    参数:
        prompt: 用户提示文本
        image_paths: 图片路径列表
        api_key: DashScope API 密钥（可选，默认使用配置文件中的值）
        endpoint: DashScope 端点（可选，默认使用配置文件中的值）
        model: 使用的模型名称（默认 qwen3.5-plus）
        stream: 是否使用流式输出
        max_tokens: 最大生成 token 数
        temperature: 温度参数
        verbose: 是否打印详细日志

    返回:
        str: 模型的响应文本
    """
    client = DashScopeOpenAIClient(
        api_key=api_key,
        endpoint=endpoint,
        model=model,
        verbose=verbose
    )
    return client.chat_with_images(
        prompt,
        image_paths=image_paths,
        stream=stream,
        max_tokens=max_tokens,
        temperature=temperature
    )
