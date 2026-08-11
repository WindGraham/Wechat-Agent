# -*- coding: utf-8 -*-
"""decision/memory/injector.py — 记忆自动拼接（决策前注入）。

决策前把记忆拼进 prompt，分四个区：

  【你是谁】     L0 全局记忆（全量）— agent 核心身份、主人、能力边界
  【当前所在群】  L2 当前会话 + L1 在场人的完整记忆（全量）
  【你的世界】    所有群/人的轻量概览 — 让 agent 知道"外面还有谁"
                 只列群名+一句话摘要、不在场人的名字，不展开完整事实

注入哲学：
  - 相关上下文（当前群+在场人）→ 全量注入，不丢失细节
  - 世界认知（其他群/人）→ 轻量概览，知道存在即可
  - 需要深挖时 → agent 主动调 memory 工具搜索

渲染成【记忆】块，带来源标注。
"""

import logging
from collections import OrderedDict

log = logging.getLogger("decision.memory.injector")

# 注入上限（防御性）
MAX_PER_USER = 15           # 每用户最多注入多少条（按 updated_at 倒序取最新）
MAX_PER_SCOPE = 500         # 全局/会话记忆上限（防御性）
SUMMARY_LEN = 40            # 世界概览中每条摘要截断长度


def _senders_of(history, new_messages) -> set:
    """从历史窗口 + 新消息提取会话中出现的人（非"我"的 sender）。"""
    seen = OrderedDict()
    for rows in (history or [], new_messages or []):
        for m in rows:
            sender = getattr(m, "sender", "")
            if not sender or sender in ("我", "self", "system"):
                continue
            seen.setdefault(sender, True)
    return set(seen.keys())


class MemoryInjector:
    """把 MemoryStore 的记忆拼成 prompt 块文本。"""

    def __init__(self, store):
        self._store = store

    def build_memory_block(self, session: str, is_group: bool,
                           history, new_messages) -> str:
        """拼【记忆】块文本：你是谁 + 当前所在群 + 你的世界。

        session: 当前会话名（标注"本群"）
        history/new_messages: 提取"在场的人"
        """
        parts = []

        all_facts = self._store.list_scope("all") \
            if hasattr(self._store, "list_scope") else []

        # ---- 按 scope 分组
        globals_ = [f for f in all_facts if f.get("scope") == "global"]
        session_facts = [f for f in all_facts if f.get("scope") == "session"]
        user_facts = [f for f in all_facts if f.get("scope") == "user"]

        # 按用户分组
        by_user = {}
        for f in user_facts:
            uname = f.get("_file", "unknown")
            by_user.setdefault(uname, []).append(f)

        # 当前窗口内在场的人
        present = _senders_of(history, new_messages)

        # ============================================================
        # 1. 【你是谁】— 全局记忆，全量（agent 核心身份）
        # ============================================================
        if globals_:
            lines = [f"- {f.get('content', '')}" for f in globals_[:MAX_PER_SCOPE]]
            parts.append("【你是谁】\n" + "\n".join(lines))

        # ============================================================
        # 2. 【当前所在群】— 本群记忆 + 在场人的完整记忆
        # ============================================================
        current_lines = []

        # 2a. 本群会话记忆
        cur_session = [f for f in session_facts
                       if f.get("_file") == session]
        for f in cur_session:
            current_lines.append(f"- [本群] {f.get('content', '')}")

        # 2b. 在场人的完整记忆（支持别名反查）
        for display in sorted(present):
            resolved = self._store.resolve_user(display) \
                if hasattr(self._store, "resolve_user") else (None, None)
            canonical, _path = resolved
            user_key = canonical or display
            facts = by_user.get(user_key, [])
            if not facts:
                continue
            # 按 updated_at 倒序取最新 MAX_PER_USER 条
            facts = sorted(facts, key=lambda f: -f.get("updated_at", 0))
            facts = facts[:MAX_PER_USER]
            label = display if display == user_key else f"{display}(即{user_key})"
            for f in facts[:MAX_PER_USER]:
                src = f.get("source", "")
                current_lines.append(
                    f"- [{label}" + (f" 来自{src}" if src else "") + "] "
                    + f.get('content', ''))
        if current_lines:
            parts.append("【当前所在群】\n" + "\n".join(current_lines))

        # ============================================================
        # 3. 【你的世界】— 所有群/人的轻量概览
        #    其他群：群名 + 最新一条记忆摘要
        #    不在场的人：只列名字
        # ============================================================
        world_lines = []

        # 3a. 其他群的概览
        by_session = {}
        for f in session_facts:
            sname = f.get("_file", "?")
            by_session.setdefault(sname, []).append(f)

        if len(by_session) > 1 or (len(by_session) == 1
                                    and session not in by_session):
            world_lines.append("你参与的群：")
            for sname in sorted(by_session, key=lambda s:
                                (s != session, s)):
                marker = "★" if sname == session else " "
                facts = by_session[sname]
                # 取最近一条作为摘要
                latest = max(facts, key=lambda f: f.get("updated_at", 0))
                summary = latest.get("content", "")
                if len(summary) > SUMMARY_LEN:
                    summary = summary[:SUMMARY_LEN] + "…"
                n = len(facts)
                extra = f" ({n}条记忆)" if n > 1 else ""
                world_lines.append(f"  {marker} {sname} — {summary}{extra}")

        # 3b. 不在场的人（只列名字）
        known_users = set(by_user.keys())
        others = known_users - present
        # 也处理别名：如果 display 通过别名反查找到了用户，display 也算"在场"
        # 这里简单处理：不在场的人中排除已注入的
        if others:
            names = sorted(others)
            world_lines.append(f"你认识但不在场的人：{', '.join(names)}")

        if world_lines:
            parts.append("【你的世界】\n" + "\n".join(world_lines))

        if not parts:
            return ""
        return "\n\n".join(parts)
