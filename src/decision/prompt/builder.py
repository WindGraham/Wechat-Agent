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

    @staticmethod
    def render_running_tasks(running_tasks) -> str:
        """执行中任务台账 → 实时板块文本行。"""
        if not running_tasks:
            return "无"
        lines = []
        now = time.time()
        for t in running_tasks:
            elapsed = int(now - t.get("started_at", now))
            mins, secs = divmod(elapsed, 60)
            dur = f"{mins}分{secs}秒" if mins else f"{secs}秒"
            lines.append(f"- {t.get('task_id', '?')}：{t.get('desc') or '（无描述）'}"
                         f"（已进行 {dur}）")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 主构建
    def build(self, session: str, is_group: bool, trigger: str,
              history, new_messages, tool_feedback: str = "",
              running_tasks=None, memory_block: str = "") -> list:
        """返回 [{"role": "system", ...}, {"role": "user", ...}]。

        running_tasks: 该会话执行中的后台任务台账记录列表（实时板块，
        防重复委派 + 让 LLM 知道"已经派人去办了"）。
        memory_block: 自动注入的记忆块文本（proxy 用 MemoryInjector 拼好
        传入；空串则不注入）。对应【记忆】块（L0全局+L2会话+L1在场人）。"""
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
        # 记忆块在历史之前注入（LLM 先看到记忆背景，再看历史与新消息）
        if memory_block:
            user_parts.append(memory_block)
        if history:
            user_parts.append(self._lib.user_block(
                "history", n=len(history),
                history=self.render_history(history)))
        if new_messages:
            user_parts.append(self._lib.user_block(
                "new_messages",
                new_messages=self.render_new_messages(new_messages)))
        user_parts.append(self._lib.user_block(
            "running_tasks",
            lines=self.render_running_tasks(running_tasks)))
        if tool_feedback:
            user_parts.append(f"【工具返回】\n{tool_feedback.strip()}")

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(
                p for p in user_parts if p)},
        ]

    def build_warmup(self, session: str, history_batch) -> list:
        """冷启动记忆预热的专用 prompt。

        与正常决策的区别：强制"只输出 memory 操作，不回复"——
        系统只需从历史提取记忆，不需要 agent 回复用户。
        history_batch: 消息列表（Message 或含 sender/content 的对象）。
        """
        # 渲染批次历史为文本行
        lines = []
        for m in history_batch:
            sender = getattr(m, "sender", "?")
            content = (getattr(m, "content", "") or "").replace("\n", " ")[:300]
            if getattr(m, "is_mine", False):
                sender = "我"
            lines.append(f"{sender}: {content}")
        batch_text = "\n".join(lines)

        system = (
            "# 记忆提取任务\n\n"
            "你是微信人格 agent 的后台记忆整理模块。系统会把一批历史聊天记录"
            "交给你，你要从中提取**值得长期记住**的信息，输出 memory 工具块。\n\n"
            "## 硬性要求\n"
            "1. **只输出 <tool name=\"memory\" .../> 块**，绝对不要输出 "
            "<reply>/<task>/<silent/>——这是后台整理，不需要回复任何人\n"
            "2. **严格格式模板**（每个块必须完整, 缺一不可）：\n"
            "   - 记一条个人记忆（op=add, scope=user）**必须带 user 属性**：\n"
            "     <tool name=\"memory\" op=\"add\" scope=\"user\" user=\"人名\" content=\"关于这个人的具体信息\"/>\n"
            "   - 记一条群聊记忆（op=add, scope=session）**必须带 session 属性**：\n"
            "     <tool name=\"memory\" op=\"add\" scope=\"session\" content=\"本群的具体信息\"/>\n"
            "   - 记一条全局记忆（op=add, scope=global）：\n"
            "     <tool name=\"memory\" op=\"add\" scope=\"global\" content=\"通用信息\"/>\n"
            "   - 登记别名（op=alias）：\n"
            "     <tool name=\"memory\" op=\"alias\" user=\"主称呼\" alias=\"别的称呼\"/>\n"
            "3. **属性规则（违反会被丢弃）**：\n"
            "   - 内容一律放 **content 属性**（不要用 value，不要放标签体里）\n"
            "   - scope=user 时 **user 必填**；scope=session 时 **session 必填**；"
            "scope 缺省视为 global\n"
            "   - user/session 属性值是**对话里出现的准确称呼**，不要自己造\n"
            "4. 提取准则（scope 与粒度）：\n"
            "   - **某个人**的偏好/经历/身份 → op=add scope=user（content 里描述这个人）\n"
            "   - 群聊的约定/氛围/固定梗 → op=add scope=session\n"
            "   - 跨会话通用的事实/重要背景 → op=add scope=global\n"
            "   - 同一个人多个称呼 → op=alias（user=主称呼, alias=别的称呼）\n"
            "5. **宁可少记，不记垃圾**：拿不准就不记；不确定是同一个人就别登记别名\n"
            "6. 一次可输出多个 memory 块，按顺序执行"
        )
        user = (
            f"【目标会话】{session}\n"
            f"【这批聊天记录（{len(history_batch)} 条）】\n{batch_text}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
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
                result=receipt.get("result", ""),
                deliverables=receipt.get("deliverables", "（无）")),
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
