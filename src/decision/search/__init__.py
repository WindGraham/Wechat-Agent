# -*- coding: utf-8 -*-
"""decision/search — websearch 工具（可插拔后端）。

对外暴露 WebSearchTool，供 proxy 的 _exec_tool 分发调用。

工具协议（docs/DESIGN_DECISION_TOOL_ARCHITECTURE.md §四）：
    <tool name="websearch" query="..." scope="all|web|local"/>
  - local 段：查本地记忆/聊天记录（毫秒，同步）
  - web 段：网络搜索（秒级，异步由 proxy 处理）
"""

from .backend import (DuckDuckGoBackend, SearchBackend, SearchService,
                      get_backend, register_backend)

__all__ = ["WebSearchTool", "SearchService", "SearchBackend",
           "DuckDuckGoBackend", "get_backend", "register_backend"]


class WebSearchTool:
    """websearch 工具实现：把 <tool> 属性翻译成 SearchService 操作。

    proxy 注入 search_service（含 memory_store/reader 用于本地段）。
    """

    def __init__(self, search_service: SearchService = None):
        self._svc = search_service or SearchService()

    def run_local(self, attrs: dict, session: str = "") -> str:
        """本地段（同步，立即回灌）。返回本地记录文本。"""
        query = attrs.get("query", "")
        if not query:
            return "websearch 缺 query 属性"
        return self._svc.search_local(query, session=session)

    def run_web(self, attrs: dict) -> list:
        """网络段（由 proxy 决定异步）。返回搜索结果列表。"""
        query = attrs.get("query", "")
        if not query:
            return []
        n = min(int(attrs.get("n", 5)), 10)
        return self._svc.search_web(query, n=n)
