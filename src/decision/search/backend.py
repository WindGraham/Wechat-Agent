# -*- coding: utf-8 -*-
"""decision/search/backend.py — websearch 可插拔搜索后端。

设计（docs/DESIGN_DECISION_TOOL_ARCHITECTURE.md §四）：
  - SearchBackend 接口：任何搜索实现只需实现 search()，proxy 分流不变
  - DuckDuckGoBackend：默认实现（ddgs 库，免费无 key，多引擎）
  - 将来可加：argus / SearXNG / kimi k2.6 $web_search 等

websearch 是"工具层"能力（决策层固定 k3，工具层模型/后端自由），
故不依赖 k3 的 $web_search（k3 上该功能有 bug，见 Kimi 论坛 2026-07）。
"""

import logging
import time

log = logging.getLogger("decision.search")

# 后端注册表：name -> class（可插拔）
_BACKENDS = {}


class SearchBackend:
    """搜索后端接口。子类实现 search()。"""

    name = "base"

    def search(self, query: str, n: int = 5) -> list:
        """搜索 query，返回 [{"title", "snippet", "url"}, ...]。
        失败抛异常（调用方负责降级/错误回灌）。"""
        raise NotImplementedError


class DuckDuckGoBackend(SearchBackend):
    """DuckDuckGo 搜索（ddgs 库，免费无 key，多引擎）。"""

    name = "ddgs"

    def search(self, query: str, n: int = 5) -> list:
        from ddgs import DDGS
        out = []
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=n)
        for r in results or []:
            url = r.get("href") or r.get("url") or ""
            title = r.get("title") or ""
            body = r.get("body") or r.get("snippet") or ""
            if url:
                out.append({"title": title, "snippet": body, "url": url})
        return out


# ---------------------------------------------------------------- 注册表
def register_backend(cls):
    _BACKENDS[cls.name] = cls
    return cls


register_backend(DuckDuckGoBackend)


def get_backend(name: str = "ddgs", **kw) -> SearchBackend:
    cls = _BACKENDS.get(name)
    if cls is None:
        raise KeyError(f"未知搜索后端: {name}（可用: {sorted(_BACKENDS)}）")
    return cls(**kw)


class SearchService:
    """websearch 工具的服务层：本地段 + 网络段。

    - local 段：查 MemoryStore + 消息库（毫秒，同步）
    - web 段：调后端搜索（秒级，由 proxy 决定是否异步）
    """

    def __init__(self, backend: SearchBackend = None, memory_store=None,
                 reader=None):
        self._backend = backend or get_backend("ddgs")
        self._memory = memory_store
        self._reader = reader

    # ---------------------------------------------------------------- 本地段
    def search_local(self, query: str, session: str = "",
                     limit: int = 5) -> str:
        """查本地记忆（memory + 消息日志），返回带来源标注的文本。"""
        parts = []
        # 1. memory
        if self._memory is not None:
            try:
                hits = self._memory.search(query, scope="all",
                                           session=session, limit=limit)
                if hits:
                    lines = []
                    for f in hits:
                        src = f.get("source", "")
                        lines.append(
                            f"[记忆{f.get('_file','')}|{src}] "
                            f"{f.get('content','')}")
                    parts.append("【本地记忆】\n" + "\n".join(lines))
            except Exception:  # noqa: BLE001
                log.exception("memory 搜索失败")
        # 2. 消息日志（reader 接口）
        if self._reader is not None:
            try:
                rows = self._reader.get_context(session, n=500) \
                    if session else []
                hits = [m for m in rows if query in (getattr(m, "content", "")
                                                     or "")]
                if hits:
                    lines = [f"{getattr(m,'sender','?')}: "
                             f"{(getattr(m,'content','') or '')[:120]}"
                             for m in hits[-limit:]]
                    parts.append("【本地聊天记录】\n" + "\n".join(lines))
            except Exception:  # noqa: BLE001
                log.exception("消息日志搜索失败")
        return "\n\n".join(parts)

    # ---------------------------------------------------------------- 网络段
    def search_web(self, query: str, n: int = 5) -> list:
        """网络搜索，返回 [{"title","snippet","url"}, ...]。"""
        t0 = time.time()
        results = self._backend.search(query, n=n)
        log.info("websearch '%s' -> %d 条 (%.1fs)", query, len(results),
                 time.time() - t0)
        return results

    @staticmethod
    def format_results(results: list) -> str:
        """搜索结果 → 回灌文本。"""
        if not results:
            return "【网络结果】未找到相关结果"
        lines = ["【网络结果】"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            snip = r.get("snippet", "")
            url = r.get("url", "")
            lines.append(f"{i}. {title}")
            if snip:
                lines.append(f"   {snip[:200]}")
            if url:
                lines.append(f"   {url}")
        return "\n".join(lines)
