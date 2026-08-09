# -*- coding: utf-8 -*-
"""prompt — 上下文工程：prompt 块文件装配 + 人格卡渲染 + 决策输入构建。

块文件在 config/prompts/（order.txt 为装配清单），按 mtime 热重读——
网关改完文件下一次决策即生效，无需重启。
"""

from .builder import ContextBuilder
from .library import PromptLibrary
from .persona import PersonaRenderer

__all__ = ["ContextBuilder", "PromptLibrary", "PersonaRenderer"]
