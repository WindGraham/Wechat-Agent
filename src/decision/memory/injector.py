# -*- coding: utf-8 -*-
"""decision/memory/injector.py — 记忆自动拼接（决策前注入）。

在设计上对应【相关记忆】块的自动注入（DESIGN_DECISION_TOOL_ARCHITECTURE.md §五）：
决策前把三层记忆拼进 prompt：

  L0 全局记忆（global.json）        → 跨会话通用（agent 本体/主人/通用事实）
  L2 当前会话记忆（sessions/）.     → 本会话特有（群梗/本群约定）
  L1 目标会话出现的人的用户记忆     → 按"会话里出现的人"注入（用户的方案：
                                     目标会话出现的人都倒入，上下文够大）

注入规则（当前方案，按用户决策）：
  - 会话中出现的人 = history + new_messages 里的非"我" sender
  - 对每个人：拉其 users/<昵称>.json 的记忆（全部，量小）
  - 上下文够大（1M），暂不担心超出 → 全量注入，不截断
  - 只注入"当前会话出现的人"的记忆，不注入"没出现在这个会话的人"
    （防泄露：没在这个会话说话的人，其记忆不进这个会话的 prompt）

渲染成【记忆】块，带来源标注（source），LLM 可判断该不该提。
"""

import logging
from collections import OrderedDict

log = logging.getLogger("decision.memory.injector")

# 注入上限（防御性，防极端情况；正常全量注入）
MAX_USERS = 50              # 最多注入多少个用户的记忆
MAX_PER_USER = 100          # 每用户最多注入多少条
MAX_PER_SCOPE = 500         # 全局/会话记忆上限（防御性）


def _senders_of(history, new_messages) -> list:
    """从历史+新消息提取会话中出现的人（非"我"的 sender，去重保序）。"""
    seen = OrderedDict()
    for rows in (history or [], new_messages or []):
        for m in rows:
            sender = getattr(m, "sender", "")
            if not sender or sender in ("我", "self", "system"):
                continue
            seen.setdefault(sender, True)
    return list(seen.keys())


class MemoryInjector:
    """把 MemoryStore 的记忆拼成 prompt 块文本。使用 store 的公开检索接口。"""

    def __init__(self, store):
        self._store = store

    def build_memory_block(self, session: str, is_group: bool,
                           history, new_messages) -> str:
        """拼【记忆】块文本；无任何记忆时返回空串（调用方跳过该块）。

        session: 当前会话名（L2 会话记忆用）
        history/new_messages: 当前决策的上下文（提取会话中出现的人）
        """
        parts = []

        # ---- L0 全局记忆（scope=global 全量）
        globals_ = self._store.list_scope("global") \
            if hasattr(self._store, "list_scope") else []
        if globals_:
            lines = [f"- [全局] {f.get('content', '')}" for f in globals_[:MAX_PER_SCOPE]]
            parts.append("【全局记忆】\n" + "\n".join(lines))

        # ---- L2 当前会话记忆（scope=session）
        session_facts = self._store.list_scope("session", session=session) \
            if hasattr(self._store, "list_scope") else []
        if session_facts:
            lines = [f"- [本会话] {f.get('content', '')}"
                     for f in session_facts[:MAX_PER_SCOPE]]
            parts.append("【本会话记忆】\n" + "\n".join(lines))

        # ---- L1 会话中出现的人的用户记忆
        senders = _senders_of(history, new_messages)
        user_lines = []
        for user in senders[:MAX_USERS]:
            facts = self._store.list_scope("user", user=user) \
                if hasattr(self._store, "list_scope") else []
            for f in facts[:MAX_PER_USER]:
                src = f.get("source", "")
                label = f"[{user}" + (f" 来自{src}" if src else "") + "]"
                user_lines.append(f"- {label} {f.get('content', '')}")
        if user_lines:
            parts.append("【对在场人的了解】\n" + "\n".join(user_lines))

        if not parts:
            return ""
        return "\n\n".join(parts)
