"""
LLM 客户端配置文件
"""
import os

# ============================================================
# GitHub Copilot 配置
# ============================================================
COPILOT_CLIENT_ID = "01ab8ac9400c4e429b23"
COPILOT_DEVICE_CODE_URL = "https://github.com/login/device/code"
COPILOT_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_COMPLETION_URL = "https://api.githubcopilot.com/chat/completions"

# Copilot 支持的模型列表
COPILOT_AVAILABLE_MODELS = [
    "gemini-3-pro-preview",
    "claude-opus-4.5",
    "claude-sonnet-4.5",
    "gpt-5.2"
]

# Copilot 默认模型
COPILOT_DEFAULT_MODEL = "gemini-3-pro-preview"

# ============================================================
# Azure AI Foundry 配置
# ============================================================
AZURE_ENDPOINT = '<redacted>'
# 从环境变量读取 API Key，避免明文存储
AZURE_API_KEY = os.environ.get("AZURE_API_KEY", "")
AZURE_DEFAULT_MODEL = "DeepSeek-V3.2-Speciale"

# Azure 支持的模型列表（包括 DeepSeek 和 Grok）
AZURE_AVAILABLE_MODELS = [
    "DeepSeek-V3.2-Speciale",
    # "grok-4-fast-non-reasoning",
    # "grok-4-fast-reasoning",
    "grok-4-1-fast-reasoning",
    "FW-GLM-5",
    "Kimi-K2.5"

]

# Azure 纯文本模型列表（不支持 Vision）
AZURE_TEXT_MODELS = [
    # "DeepSeek-V3.2-Speciale",  # review 20260402-20260409: 整周0次BUY/SELL，零贡献，移除
    "FW-GLM-5",
]

# Azure 支持 Vision 的模型列表
AZURE_VISION_MODELS = [
    "grok-4-1-fast-reasoning",
    "Kimi-K2.5",
    # "grok-4-fast-non-reasoning",
    # "grok-4-fast-reasoning",
]

# ============================================================
# 阿里云百炼（DashScope OpenAI 兼容）配置
# ============================================================
DASHSCOPE_ENDPOINT = "https://coding.dashscope.aliyuncs.com/v1"
# 从环境变量读取 API Key，避免明文存储
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_DEFAULT_MODEL = "MiniMax-M2.5"

# DashScope 支持的模型列表
DASHSCOPE_AVAILABLE_MODELS = [
    "qwen3.5-plus",
    "kimi-k2.5",
    "glm-5",
    "MiniMax-M2.5",
    "qwen3-max-2026-01-23",
    # "qwen3-coder-next",
    # "qwen3-coder-plus",
    # "glm-4.7",
]

# DashScope 支持 Vision 的模型列表
DASHSCOPE_VISION_MODELS = [
    "qwen3.5-plus",
    "kimi-k2.5",
]

# DashScope 纯文本模型列表（不支持 Vision）
DASHSCOPE_TEXT_MODELS = [
    "glm-5",
    "MiniMax-M2.5",
    "qwen3-max-2026-01-23",
#     "qwen3-coder-next",
#     "qwen3-coder-plus",
#     "glm-4.7",
]

# ============================================================
# 通用配置
# ============================================================
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.1
DEFAULT_TOP_P = 0.95
DEFAULT_TIMEOUT = 300  # 默认超时时间，单位为秒
