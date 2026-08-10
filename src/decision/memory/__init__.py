# -*- coding: utf-8 -*-
"""decision/memory — 长期记忆工具。

对外暴露 MemoryTool 类，供 proxy 的 _exec_tool 分发调用。
只实现工具逻辑，不创建真实 memory 文件（文件在首次写入时惰性生成）。

工具协议（docs/DESIGN_DECISION_TOOL_ARCHITECTURE.md §三）：
    <tool name="memory" op="add" key="..." value="..." scope="global|user|session"/>
    <tool name="memory" op="read" key="..."/>
    <tool name="memory" op="search" keyword="..."/>
    <tool name="memory" op="update" id="..." value="..."/>
    <tool name="memory" op="delete" id="..."/>
"""

from .store import MemoryStore

__all__ = ["MemoryTool", "MemoryStore"]


class MemoryTool:
    """memory 工具实现：把 <tool> 属性翻译成 MemoryStore 操作，返回文本结果。"""

    def __init__(self, store: MemoryStore = None):
        self._store = store or MemoryStore()

    def run(self, attrs: dict, current_session: str = "") -> str:
        """执行 memory 操作，返回回灌给 LLM 的文本。

        attrs: <tool> 块解析出的属性 dict（name/op/key/value/...）。
        current_session: 当前决策会话（scope 缺省时按此推断）。
        """
        op = attrs.get("op", "")
        try:
            if op == "add":
                return self._add(attrs, current_session)
            if op == "read":
                return self._read(attrs)
            if op == "search":
                return self._search(attrs, current_session)
            if op == "update":
                return self._update(attrs)
            if op == "delete":
                return self._delete(attrs)
            return f"未知 memory 操作: {op}（可选 add/read/search/update/delete）"
        except Exception as e:  # noqa: BLE001
            return f"memory 操作失败: {type(e).__name__}: {e}"

    # ---------------------------------------------------------------- 操作
    def _scope(self, attrs: dict, current_session: str) -> str:
        """scope 解析：显式指定优先；缺省时私聊按 user、群聊按 session。"""
        scope = attrs.get("scope", "")
        if scope in ("global", "user", "session"):
            return scope
        if current_session:
            return "session"       # 缺省：当前会话（安全，不跨会话）
        return "global"

    def _add(self, attrs: dict, current_session: str) -> str:
        value = attrs.get("value", "")
        if not value:
            return "memory add 缺 value 属性"
        entry = self._store.add(
            content=value,
            key=attrs.get("key", ""),
            scope=self._scope(attrs, current_session),
            user=attrs.get("user", ""),
            session=attrs.get("session", "") or current_session,
            source=attrs.get("source", "") or current_session,
            ref_msg=attrs.get("ref", ""),
        )
        return f"已记录（id={entry['id']}, key={entry['key']}, scope={entry['scope']}）"

    def _read(self, attrs: dict) -> str:
        key = attrs.get("key", "")
        if not key:
            return "memory read 缺 key 属性"
        rows = self._store.read(key)
        if not rows:
            return f"未找到 key='{key}' 的记忆"
        return "\n".join(
            f"- [{f.get('source','')}] {f.get('content','')}"
            f"（id={f.get('id')}）" for f in rows)

    def _search(self, attrs: dict, current_session: str) -> str:
        keyword = attrs.get("keyword", "")
        if not keyword:
            return "memory search 缺 keyword 属性"
        rows = self._store.search(
            keyword,
            scope=attrs.get("scope", "all"),
            user=attrs.get("user", ""),
            session=attrs.get("session", "") or current_session,
            limit=int(attrs.get("n", 10)),
        )
        if not rows:
            return f"未找到包含 '{keyword}' 的记忆"
        return "\n".join(
            f"- [{f.get('_file','')} | {f.get('source','')}] "
            f"{f.get('content','')}（id={f.get('id')}）" for f in rows)

    def _update(self, attrs: dict) -> str:
        fid = attrs.get("id", "")
        if not fid:
            return "memory update 缺 id 属性"
        value = attrs.get("value")
        conf = attrs.get("confidence")
        ok = self._store.update(fid, content=value,
                                confidence=float(conf) if conf else None)
        return "已更新" if ok else f"未找到 id={fid}"

    def _delete(self, attrs: dict) -> str:
        fid = attrs.get("id", "")
        if not fid:
            return "memory delete 缺 id 属性"
        ok = self._store.delete(fid)
        return "已删除" if ok else f"未找到 id={fid}"
