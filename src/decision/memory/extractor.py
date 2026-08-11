# -*- coding: utf-8 -*-
"""decision/memory/extractor.py — 后置记忆提取 + 定期整合。

两层机制：
  1. post-hoc 提取：每次 agent 回复后，把本轮对话 + 已有记忆喂给便宜模型，
     让它判断 merge（update 已有 id）还是 add（新增）。不占用回复轮次注意力。
  2. 定期 consolidation：每 N 小时，把同一用户/会话的散碎记忆合并成更高级、
     信息密度更高的条目（减少条目数、提高单条质量）。

与 warm_memory 的关系：
  - warm_memory 是一次性冷启动（从 chatlog.db 批量提取）
  - extractor 是持续运行中增量提取（每轮对话后触发）
  - consolidation 是定期整理（碎片合并）

关键设计：提取 LLM 必须看到已有记忆的 id，以便决定 merge vs. add。
"""

import logging
import os
import time

log = logging.getLogger("decision.memory.extractor")

# 提取清单：告诉 LLM 具体该关注什么类型的记忆
EXTRACTION_CHECKLIST = (
    '【偏好与忌讳】用户明确说的喜欢/不喜欢、习惯（如\u201c我不吃辣\u201d\u201c别发表情包\u201d）\n'
    '【身份与背景】职业、学校、所在地、正在做的事（如考研、找工作、学车）\n'
    '【人际关系】谁是谁的谁（如\u201c他是我哥\u201d\u201c我俩是室友\u201d），谁和谁关系好/不好\n'
    '【承诺与约定】答应过的事、约定的时间（如\u201c下周给你答复\u201d\u201c周末去爬山\u201d）\n'
    '【群内文化】本群特有的梗、约定、氛围（如\u201c本群爱复读\u201d\u201c大家叫 X 为 Y\u201d）\n'
    '【技能与能力】谁会什么、在做什么项目、擅长什么\n'
    '【近期关注】某人最近在追的事、在讨论的话题、在纠结的问题\n'
    '【外号与别称】同一个人在不同场合被叫不同的名字 → 用 alias 登记'
)

# 合并策略：告诉 LLM 如何处理已有记忆
MERGE_RULES = (
    "## 合并策略（仔细看，这决定了记忆质量）\n\n"
    "每发现一条值得记的信息，先看「已有记忆」里是否已有相关条目：\n\n"
    "1. **新信息确认/补充/延伸了已有记忆** → 用 `op=\"update\"` 更新那条记忆，"
    "把新旧信息合并成一条更完整的（用已有条目的 id）\n"
    "2. **新信息与已有记忆矛盾** → 用 `op=\"update\"` 更正，按新信息为准\n"
    "3. **全新信息，与已有记忆无关** → 用 `op=\"add\"` 新增\n"
    "4. **新信息没有增加任何实质内容** → 跳过，不输出\n"
    "5. **宁可多记一条，别漏记**：记错了可以 update/delete，漏了就没了\n"
    "6. 每条 memory 内容要**具体、可复用、一句话能说清**。"
    "不记流水账（吃了什么、哈哈哈），但关键信息一定要记"
)

# 后置提取 system prompt 模板
EXTRACTION_SYSTEM = (
    "# 记忆提取任务\n\n"
    "你是微信人格 agent 的后台记忆整理模块。系统把一段**刚发生的对话**和"
    "**已有的相关记忆**交给你，你要从中提取值得长期记住的信息。\n\n"
    "## 硬性要求\n"
    "1. **只输出 <tool name=\"memory\" .../> 块**，不输出任何其他内容\n"
    "2. 工具块格式：\n"
    "   - 新增：`<tool name=\"memory\" op=\"add\" scope=\"user\" user=\"人名\" content=\"信息\"/>`\n"
    "   - 更新：`<tool name=\"memory\" op=\"update\" id=\"已有条目的id\" content=\"合并后的完整信息\"/>`\n"
    "   - 别名：`<tool name=\"memory\" op=\"alias\" user=\"主称呼\" alias=\"别称\"/>`\n"
    "3. **内容一律放 content 属性**，不要用 value\n"
    "4. scope=user 时 **user 必填**；scope=session 时 **session 必填**\n"
    "5. 一次可输出多个 memory 块\n\n"
    + MERGE_RULES + "\n\n"
    "## 该关注什么\n\n"
    + EXTRACTION_CHECKLIST
)

# 定期整合 system prompt 模板
CONSOLIDATION_SYSTEM = (
    "# 记忆整合任务\n\n"
    "你是一个记忆整理模块。系统会把**同一个对象的多条散碎记忆**交给你，"
    "你要把它们**合并成更少、信息密度更高的条目**。\n\n"
    "## 规则\n"
    "1. 把语义相近的多条合并成一条（如'风图喜欢短消息'+'风图不喜欢长文'→"
    "'风图的沟通偏好：喜欢短消息，不爱长文'）\n"
    "2. 保留所有实质信息，不丢失细节\n"
    "3. 删除明显过时/矛盾的旧信息（以最新的为准）\n"
    "4. 如果只有一条或互不相关，原样保留\n"
    "5. **只输出 <tool name=\"memory\" .../> 块**：update 改内容，delete 删废弃条目\n"
    "6. 格式：`<tool name=\"memory\" op=\"update\" id=\"条目id\" content=\"合并后内容\"/>`\n"
    "   `<tool name=\"memory\" op=\"delete\" id=\"废弃条目id\"/>`\n"
)


def build_extraction_prompt(session: str, conversation_text: str,
                            existing: dict) -> str:
    """构建后置提取的 user prompt。

    existing: MemoryStore.get_context_for_extraction() 的返回值。
    """
    parts = [f"【当前会话】{session}\n"]

    # 已有记忆上下文（供 merge 判断）
    has_existing = False
    if existing.get("global_memories"):
        has_existing = True
        parts.append("## 已有全局记忆")
        for f in existing["global_memories"]:
            parts.append(f"- [id: {f['id']}] {f.get('content', '')}")

    if existing.get("session_memories"):
        has_existing = True
        parts.append(f"\n## 本会话已有记忆（{session}）")
        for f in existing["session_memories"]:
            parts.append(f"- [id: {f['id']}] {f.get('content', '')}")

    for uname, facts in (existing.get("user_memories") or {}).items():
        if facts:
            has_existing = True
            parts.append(f"\n## {uname} 的已有记忆")
            for f in facts:
                parts.append(f"- [id: {f['id']}] {f.get('content', '')}")

    if not has_existing:
        parts.append("\n（暂无已有记忆，全部按新增处理）")

    parts.append(f"\n## 本轮对话（{session}）\n{conversation_text}")
    return "\n".join(parts)


def build_consolidation_prompt(owner_name: str, owner_type: str,
                               facts: list) -> str:
    """构建定期整合的 user prompt。

    owner_type: "user" | "session" | "global"
    facts: 该对象的所有记忆条目列表。
    """
    label = {"user": f"用户 {owner_name}",
             "session": f"会话 {owner_name}",
             "global": "全局"}[owner_type]
    lines = [f"【整合对象】{label}（共 {len(facts)} 条记忆）\n"]
    for f in facts:
        lines.append(f"- [id: {f['id']}] "
                      f"[confidence: {f.get('confidence', 0):.1f}] "
                      f"{f.get('content', '')}")
    return "\n".join(lines)


class MemoryExtractor:
    """后置记忆提取器。

    使用方式：
        extractor = MemoryExtractor(store, provider)
        extractor.extract_from_conversation(session, conv_text, user_names)
    """

    def __init__(self, store, provider):
        self._store = store
        self._provider = provider
        self._last_consolidation = 0.0  # 上次整合时间戳
        self._consolidation_interval = 6 * 3600  # 每 6 小时整合一次

    # ---------------------------------------------------------------- 后置提取
    def extract_from_conversation(self, session: str,
                                  conversation_text: str,
                                  user_names: list) -> int:
        """从一段对话中提取记忆（后置，不回复用户）。

        返回执行的 memory 操作数。
        """
        if not conversation_text.strip():
            return 0

        # 取已有记忆上下文
        existing = self._store.get_context_for_extraction(
            session, user_names)

        # 构建 prompt
        user_prompt = build_extraction_prompt(
            session, conversation_text, existing)

        messages = [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        # 调 LLM
        try:
            if hasattr(self._provider, "chat_full"):
                out, _thinking = self._provider.chat_full(messages)
            else:
                out = self._provider.chat(messages)
        except Exception as e:
            log.warning("[%s] 后置提取 LLM 调用失败: %s", session, e)
            return 0

        # 解析并执行 memory 工具块
        return self._execute_memory_blocks(out, session)

    def _execute_memory_blocks(self, llm_output: str,
                               current_session: str) -> int:
        """解析 LLM 输出中的 memory 工具块并执行。返回执行数。"""
        from ...shared.xml_blocks import extract_blocks, parse_attrs
        from . import MemoryTool

        tool = MemoryTool(store=self._store)
        executed = 0
        for b in extract_blocks(llm_output):
            if not b.valid or b.tag != "tool":
                continue
            attrs = parse_attrs(b.attrs)
            if attrs.get("name") != "memory":
                continue
            result = tool.run(attrs, current_session=current_session)
            if not result.startswith("未知"):
                executed += 1
                log.debug("后置提取 memory: %s", result)
        return executed

    # ---------------------------------------------------------------- 定期整合
    def maybe_consolidate(self, now: float = None) -> int:
        """每隔 consolidation_interval 秒触发一次整合。返回整合条目数。"""
        now = now or time.time()
        if now - self._last_consolidation < self._consolidation_interval:
            return 0
        self._last_consolidation = now
        return self._consolidate_all()

    def _consolidate_all(self) -> int:
        """对所有用户和会话的散碎记忆做整合。返回处理的条目数。"""
        total = 0
        # 全局
        total += self._consolidate_owner("global", "global", "")
        # 用户
        users_dir = os.path.join(self._store._root, "users")
        if os.path.isdir(users_dir):
            for fn in sorted(os.listdir(users_dir)):
                if fn.endswith(".json"):
                    uname = fn[:-5]
                    total += self._consolidate_owner("user", uname, "")
        # 会话
        sessions_dir = os.path.join(self._store._root, "sessions")
        if os.path.isdir(sessions_dir):
            for fn in sorted(os.listdir(sessions_dir)):
                if fn.endswith(".json"):
                    total += self._consolidate_owner(
                        "session", fn[:-5], "")
        if total:
            log.info("记忆整合完成: %d 条", total)
        return total

    def _consolidate_owner(self, owner_type: str, owner_name: str,
                           session: str = "") -> int:
        """整合单个对象的记忆。返回执行的 tool 操作数。"""
        facts = []
        if owner_type == "global":
            facts = self._store._facts_of(self._store._global_path())
        elif owner_type == "user":
            facts = self._store._facts_of(
                self._store._user_path(owner_name))
        else:
            facts = self._store._facts_of(
                self._store._session_path(owner_name))

        # 只有 1 条或 0 条不需要整合
        if len(facts) <= 1:
            return 0

        user_prompt = build_consolidation_prompt(
            owner_name, owner_type, facts)

        messages = [
            {"role": "system", "content": CONSOLIDATION_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        try:
            if hasattr(self._provider, "chat_full"):
                out, _thinking = self._provider.chat_full(messages)
            else:
                out = self._provider.chat(messages)
        except Exception as e:
            log.warning("整合 %s/%s 失败: %s", owner_type, owner_name, e)
            return 0

        return self._execute_memory_blocks(out, session or owner_name)
