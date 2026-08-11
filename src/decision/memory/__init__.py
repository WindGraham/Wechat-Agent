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
from .injector import MemoryInjector
from .vector_store import VectorStore
from .extractor import MemoryExtractor

__all__ = ["MemoryTool", "MemoryStore", "MemoryInjector",
           "VectorStore", "MemoryExtractor"]


class MemoryTool:
    """memory 工具实现：把 <tool> 属性翻译成 MemoryStore 操作，返回文本结果。"""

    def __init__(self, store: MemoryStore = None):
        self._store = store or MemoryStore()

    def run(self, attrs: dict, current_session: str = "",
           is_group: bool = None) -> str:
        """执行 memory 操作，返回回灌给 LLM 的文本。

        attrs: <tool> 块解析出的属性 dict（name/op/key/value/...）。
        current_session: 当前决策会话（scope 缺省时按此推断）。
        """
        op = attrs.get("op", "")
        try:
            if op == "add":
                return self._add(attrs, current_session, is_group)
            if op == "read":
                return self._read(attrs)
            if op == "search":
                return self._search(attrs, current_session)
            if op == "update":
                return self._update(attrs)
            if op == "delete":
                return self._delete(attrs)
            if op == "alias":
                return self._alias(attrs)
            return f"未知 memory 操作: {op}（可选 add/read/search/update/delete/alias）"
        except Exception as e:  # noqa: BLE001
            return f"memory 操作失败: {type(e).__name__}: {e}"

    # ---------------------------------------------------------------- 操作
    def _scope(self, attrs: dict, current_session: str) -> str:
        """scope 解析：显式指定优先；缺省按当前会话（安全，不跨会话，
        对齐 memory.md「scope 缺省按当前会话记，别擅自跨会话」）。"""
        scope = attrs.get("scope", "")
        if scope in ("global", "user", "session"):
            return scope
        if current_session:
            return "session"       # 缺省：当前会话（安全，不跨会话）
        return "global"

    def _add(self, attrs: dict, current_session: str,
            is_group: bool = None) -> str:
        # 兼容 value 与 content 两种属性名（LLM 输出习惯用 content）
        value = attrs.get("value", "") or attrs.get("content", "")
        if not value:
            return "memory add 缺 value/content 属性"
        # 守卫：scope=user 必须带 user 属性——缺了会写进 users/unnamed.json
        # （归属不明的记忆），违反 memory.md「scope=user 必带 user」硬规则
        scope = self._scope(attrs, current_session)
        if scope == "user" and not (attrs.get("user") or "").strip():
            return "memory add: scope=user 必须带 user 属性（记忆归属谁）"
        # 守卫：scope=session 且无任何会话可推断 → 拒绝（防写进 unnamed.json）
        if scope == "session" and not (attrs.get("session") or "").strip() \
                and not (current_session or "").strip():
            return "memory add: scope=session 必须带 session 属性"
        # source 区分私聊/群聊：私聊记"私聊"，群聊记会话名——
        # 注入块据此标注 [来自私聊] vs [来自特高课]，LLM 能判断该不该说
        explicit = attrs.get("source", "")
        if explicit:
            src = explicit
        elif is_group is False:
            src = "私聊"
        else:
            src = current_session or "unknown"
        entry = self._store.add(
            content=value,
            key=attrs.get("key", ""),
            scope=scope,
            user=attrs.get("user", ""),
            session=attrs.get("session", "") or current_session,
            source=src,
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
        value = attrs.get("value") or attrs.get("content")
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

    def _alias(self, attrs: dict) -> str:
        """给用户加别名（昵称/别称）。user=主用户, alias=别名。"""
        user = attrs.get("user", "")
        alias = attrs.get("alias", "") or attrs.get("value", "")
        if not user or not alias:
            return "memory alias 需要 user(主用户) 和 alias(别名)"
        ok = self._store.add_alias(user, alias)
        return f"已给 {user} 加别名「{alias}」" if ok else "别名添加失败"
