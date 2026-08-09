# -*- coding: utf-8 -*-
"""provider — LLM 供应商抽象（决策层唯一的模型出口）。

模型可热替换（k3 / DeepSeek / 本地端点），模型怪癖集中收口。
密钥读取优先级：环境变量 > workspace/.env。严禁打印/写日志带出 key。
"""

from .base import LLMProvider, ProviderError
from .factory import create_provider

__all__ = ["LLMProvider", "ProviderError", "create_provider"]
