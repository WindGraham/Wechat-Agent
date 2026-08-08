# -*- coding: utf-8 -*-
"""prompt/builder.py — ContextBuilder：把事件+历史+人设拼成 LLM messages。

输入格式契约见 docs/DECISION_LAYER.md §二。新消息编号 m1..mN 供输出 ref。
"""

import logging
import re
import time

from .library import PromptLibrary
from .persona import PersonaRenderer

log = logging.getLogger("decision.prompt.builder")

_WEEKDAYS = "一二三四五六日"


class ContextBuilder:
    """组装决策 prompt。所有输入都是纯数据（契约类型），不依赖交互层实现。"""

    def __init__(self, library: PromptLibrary = None,
                 personas: PersonaRenderer = None, owner: str = "",
                 clock=time.time):
        self._lib = library or PromptLibrary()
        self._personas = personas or PersonaRenderer()
        self._owner = owner
        self._clock = clock

    # ---------------------------------------------------------------- 渲染辅助
    @staticmethod
    def render_history(rows) -> str:
        """消息历史 → 文本行（[我]=自己，[@我]=@我的，divider 成时间行）。"""
        lines = []
        for m in rows:
            content = (getattr(m, "content", "") or "").replace("\n", " ")[:300]
            ctype = getattr(m, "content_type", "text")
            if ctype == "time_divider":
                lines.append(f"—— {content} ——")
                continue
            sender = getattr(m, "sender", "?")
            if getattr(m, "is_mine", False):
                sender = "我"
                content = re.sub(r"^哈哈+", "", content)
            at = "[@我] " if getattr(m, "at_me", False) else ""
            lines.append(f"{at}{sender}: {content}")
        return "\n".join(lines)

    @staticmethod
    def render_new_messages(new_messages) -> str:
        """新消息编号 m1..mN。"""
        lines = []
        for i, m in enumerate(new_messages, 1):
            sender = getattr(m, "sender", "?")
            content = (getattr(m, "content", "") or "").replace("\n", " ")[:300]
            lines.append(f"m{i} {sender}: {content}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 主构建
    def build(self, session: str, is_group: bool, trigger: str,
              history, new_messages, tool_feedback: str = "") -> list:
        """返回 [{"role": "system", ...}, {"role": "user", ...}]。"""
        persona_text = self._personas.render(session)
        system_parts = self._lib.system_blocks(persona=persona_text)
        system = "\n\n".join(p for p in system_parts if p)

        lt = time.localtime(self._clock())
        weekday = _WEEKDAYS[lt.tm_wday]
        kind = "群聊" if is_group else "私聊"

        user_parts = [
            self._lib.user_block(
                "session_info", session=session, kind=kind,
                time=time.strftime("%Y-%m-%d %H:%M", lt),
                weekday=f"周{weekday}", trigger=trigger),
        ]
        if history:
            user_parts.append(self._lib.user_block(
                "history", n=len(history),
                history=self.render_history(history)))
        if new_messages:
            user_parts.append(self._lib.user_block(
                "new_messages",
                new_messages=self.render_new_messages(new_messages)))
        if tool_feedback:
            user_parts.append(f"【工具返回】\n{tool_feedback.strip()}")

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(
                p for p in user_parts if p)},
        ]

    def build_task_receipt(self, session: str, is_group: bool,
                           receipt: dict, history) -> list:
        """任务完成回调的 prompt：触发原因+新消息替换为【任务回执】。"""
        persona_text = self._personas.render(session)
        system_parts = self._lib.system_blocks(persona=persona_text)
        system = "\n\n".join(p for p in system_parts if p)

        lt = time.localtime(self._clock())
        kind = "群聊" if is_group else "私聊"
        user_parts = [
            self._lib.user_block(
                "session_info", session=session, kind=kind,
                time=time.strftime("%Y-%m-%d %H:%M", lt),
                weekday=f"周{_WEEKDAYS[lt.tm_wday]}",
                trigger="任务完成回调"),
            self._lib.user_block(
                "task_receipt",
                task_id=receipt.get("task_id", "?"),
                session=session,
                ref=receipt.get("ref", "?"),
                ref_brief=receipt.get("ref_brief", ""),
                desc=receipt.get("desc", ""),
                result=receipt.get("result", "")),
        ]
        if history:
            user_parts.append(self._lib.user_block(
                "history", n=len(history),
                history=self.render_history(history)))
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(
                p for p in user_parts if p)},
        ]
