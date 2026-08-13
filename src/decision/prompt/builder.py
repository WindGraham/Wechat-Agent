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

# 记忆提取清单：告诉 LLM 具体该关注什么类型的信息
_EXTRACTION_CHECKLIST = (
    "  - 【偏好与忌讳】用户明确说的喜欢/不喜欢、习惯（如\"我不吃辣\"\"别发表情包\"）\n"
    "  - 【身份与背景】职业、学校、所在地、正在做的事（如考研、找工作、学车）\n"
    "  - 【人际关系】谁是谁的谁（如\"他是我哥\"\"我俩是室友\"），谁和谁关系好/不好\n"
    "  - 【承诺与约定】答应过的事、约定的时间（如\"下周给你答复\"\"周末去爬山\"）\n"
    "  - 【群内文化】本群特有的梗、约定、氛围（如\"本群爱复读\"\"大家叫 X 为 Y\"）\n"
    "  - 【技能与能力】谁会什么、在做什么项目、擅长什么\n"
    "  - 【近期关注】某人最近在追的事、在讨论的话题、在纠结的问题\n"
    "  - 【外号与别称】同一个人在不同场合被叫不同的名字 → 用 alias 登记"
)


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
    def _render_content(m) -> str:
        """消息内容 → 决策层可见文本。quote 消息把被引用段与正文段
        用明确标记拆开（入库格式：第一段=引用块文字，其余=本条正文）。"""
        content = (getattr(m, "content", "") or "").strip()
        ctype = getattr(m, "content_type", "text")
        if ctype != "quote" or not content:
            return content.replace("\n", " ")[:300]
        parts = content.split("\n")
        quoted = parts[0].strip()
        body = " ".join(p.strip() for p in parts[1:] if p.strip())
        if not body:
            return f"[引用「{quoted}」]（本条只有引用，无正文）"
        return f"[引用「{quoted}」] {body}"

    @staticmethod
    def render_history(rows) -> str:
        """消息历史 → 文本行（[我]=自己，[@我]=@我的，divider 成时间行）。"""
        lines = []
        for m in rows:
            content = ContextBuilder._render_content(m)
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
            content = ContextBuilder._render_content(m)
            lines.append(f"m{i} {sender}: {content}")
        return "\n".join(lines)

    @staticmethod
    def render_known_sessions(pairs) -> str:
        """已知会话名单 → 文本行（[(name, is_group)]）。"""
        return "\n".join(f"- {name}（{'群聊' if g else '私聊'}）"
                         for name, g in pairs)

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
              running_tasks=None, memory_block: str = "",
              known_sessions=None, goal: str = "") -> list:
        """返回 [{"role": "system", ...}, {"role": "user", ...}]。

        running_tasks: 该会话执行中的后台任务台账记录列表（实时板块，
        防重复委派 + 让 LLM 知道"已经派人去办了"）。
        memory_block: 自动注入的记忆块文本（proxy 用 MemoryInjector 拼好
        传入；空串则不注入）。对应【记忆】块（L0全局+L2会话+L1在场人）。
        known_sessions: [(name, is_group)] 已知会话名单（跨会话投递时
        提供准确会话名；空/None 则不注入）。
        goal: 本会话目标/任务提示（网关按会话配置，构建 prompt 时注入
        【本群目标】块，让模型按目标工作；空串则不注入）。"""
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
        # 本群目标：紧随会话信息（模型先看到任务再读背景）
        if goal:
            user_parts.append(self._lib.user_block(
                "goal", goal=goal))
        # 记忆块在历史之前注入（LLM 先看到记忆背景，再看历史与新消息）
        if memory_block:
            user_parts.append(memory_block)
        # 已知会话名单：跨会话投递（<reply session="X">）时提供准确名称
        if known_sessions:
            user_parts.append(self._lib.user_block(
                "known_sessions",
                lines=self.render_known_sessions(known_sessions)))
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
            "scope 缺省按当前会话记\n"
            "   - user/session 属性值是**对话里出现的准确称呼**，不要自己造\n"
            "4. 提取准则（scope 与粒度）：\n"
            "   - **某个人**的偏好/经历/身份 → op=add scope=user（content 里描述这个人）\n"
            "   - 群聊的约定/氛围/固定梗 → op=add scope=session\n"
            "   - 跨会话通用的事实/重要背景 → op=add scope=global\n"
            "   - 同一个人多个称呼 → op=alias（user=主称呼, alias=别的称呼）\n"
            "5. **宁可多记，不遗漏**：记错了可以 update/delete，漏了就没了。"
            "每次提取至少输出 3-5 条记忆（除非对话真的毫无信息量）。\n"
            "6. 一次可输出多个 memory 块，按顺序执行\n\n"
            "## 具体该关注什么（逐类检查，每类都扫一遍）\n\n"
            + _EXTRACTION_CHECKLIST
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

    # ---------------------------------------------------------------- special
    def build_special(self, prompt_name: str, context: dict) -> list:
        """组装特殊 prompt 的 messages。"""
        spec = self._lib.load_special(prompt_name)
        if spec is None:
            return []

        system = spec["system"]
        for k, v in (context or {}).items():
            system = system.replace("{" + k + "}", str(v))

        user_lines = []
        for k, v in (context or {}).items():
            if k in ("time", "prompt_name"):
                continue
            if isinstance(v, list) and v:
                user_lines.append(f"## {k}")
                for item in v[:50]:
                    if isinstance(item, dict):
                        user_lines.append(
                            f"- [{item.get('source', '?')}] "
                            f"{item.get('content', str(item))}")
                    else:
                        user_lines.append(f"- {item}")
            elif isinstance(v, str) and v.strip():
                user_lines.append(f"## {k}\n{v}")
        user = "\n".join(user_lines) if user_lines else "(无额外上下文)"

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
