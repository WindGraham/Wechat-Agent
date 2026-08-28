# -*- coding: utf-8 -*-
"""proxy/decider.py — Decider：单会话决策循环（生成 → 解析 → 路由）。

从 Proxy 抽出的"决策执行"职责：
  - decide_session：一个会话的一次决策（信号量 + 会话锁 + 水位）
  - llm_loop：LLM 生成 → 解析 → 路由，tool 块回灌续生成
  - warm_memory：冷启动记忆预热
  - start_task / handle_task_done：任务委派与回执再决策
  - handle_search_done：搜索结果回灌再决策

依赖全部构造注入（reader/submit_bundle/builder/policy/media/ledger/
providers/memory_svc/tools/cli + 并发原语 + 水位 + 事件推送 + journal）。
"""

import json
import logging
import os
import threading
import time

from ...shared.types import TaskBrief
from ...shared.xml_blocks import extract_blocks, parse_attrs
from ..policy import Policy

log = logging.getLogger("decision.proxy.decider")

MAX_TOOL_CALLS = 3            # 工具调用每轮上限(协议保留)
MAX_REPLY_BLOCKS = 3          # 一轮最多 3 个 <reply>（防刷屏）
MAX_TASK_BLOCKS = 1           # 一轮最多 1 个 <task>

# 工具层简报 preamble：向 CLI 核心说明处境与输出格式
TASK_BRIEF_PREAMBLE = """\
你是微信人格 agent「陈曦」的后台执行核心。她在微信里陪用户聊天，
自己没有操作电脑的能力；凡是需要动手的事（找图、下载、做文件、
查资料、跑程序、处理数据），她都委派给你独立完成后回传结果。

你运行在她的电脑上，可以用终端做几乎任何事：联网搜索/下载、
运行脚本、处理图片与文件、读写代码、调用本机已有工具。

工作约定：
- 当前目录就是你的专属工作目录，交付物一律保存到 ./files/ 子目录
- 图片/文件务必下载落盘，不要只给网络链接（她打不开链接）
- 拿不准的结果宁可说不确定，不许编造

最终回复格式（硬性要求，程序会解析你的最后一条消息）：
1. 一句话总结：成了什么 / 没成什么
2. 每个交付物单独一行：DELIVERABLE: <本机绝对路径>
3. 需要她转达给用户的话（一两句口语，她会说微信里）
做不到就直说做不到并说明卡在哪，不许假装成功。"""


class Decider:
    """决策循环。所有协作对象经构造注入，不依赖 Proxy 实例。"""

    def __init__(self, reader, submit_bundle, builder, policy, media, ledger,
                 providers, memory_svc, tools, cli, search_feedback, clock,
                 rt, sem, session_lock_fn, push_event, journal_fn, clip_fn,
                 watermarks_path):
        self._reader = reader
        self._submit_bundle = submit_bundle
        self._builder = builder
        self._policy = policy
        self._media = media
        self._ledger = ledger
        self._providers = providers
        self._memory_svc = memory_svc
        self._tools = tools
        self._cli = cli
        self._search_feedback = search_feedback
        self._clock = clock
        self._rt = rt
        self._sem = sem
        self._session_lock = session_lock_fn
        self._push_event = push_event
        self._journal = journal_fn
        self._clip = clip_fn
        self._watermarks_path = watermarks_path
        self._watermarks = self._load_watermarks()

    # ================================================================== 水位
    def _load_watermarks(self) -> dict:
        try:
            with open(self._watermarks_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_watermarks(self):
        os.makedirs(os.path.dirname(self._watermarks_path), exist_ok=True)
        tmp = self._watermarks_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._watermarks, f)
        os.replace(tmp, self._watermarks_path)

    # ================================================================== 记忆预热
    def warm_memory(self, session: str, history_batch: list):
        """冷启动记忆预热：从一批历史提取值得记的记忆（不回复）。"""
        if not history_batch:
            log.warning("[%s] memory warm: 空批次，跳过", session)
            return
        with self._sem, self._session_lock(session):
            t0 = time.monotonic()
            self._journal("memory_warm_start", session=session,
                          batch=len(history_batch))
            try:
                messages = self._builder.build_warmup(session, history_batch)
                provider = self._providers.provider_for(session)
                if hasattr(provider, "chat_full"):
                    out, thinking = provider.chat_full(messages)
                else:
                    out, thinking = provider.chat(messages), ""
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] memory warm LLM 调用失败: %s: %s",
                            session, type(e).__name__, e)
                return
            self._journal("llm_output", session=session, round="warm",
                          output=self._clip(out),
                          thinking=self._clip(thinking) if thinking else "")
            executed = 0
            for b in extract_blocks(out):
                if b.valid and b.tag == "tool" \
                        and parse_attrs(b.attrs).get("name") == "memory":
                    result = self._tools.exec(b, session)
                    if not result.startswith("未知"):
                        executed += 1
                    log.info("[%s] memory warm: %s", session, result)
            self._journal("memory_warm_end", session=session,
                          batch=len(history_batch), executed=executed,
                          elapsed_ms=int((time.monotonic() - t0) * 1000))
            log.info("[%s] memory warm 完成: %d 条历史, 执行 %d 个 memory 操作",
                     session, len(history_batch), executed)

    # ================================================================== 决策入口
    def decide_session(self, session: str, mention_hint: bool = False,
                       force: bool = False, extra_msgs: list = None):
        """一个会话的一次决策（信号量 + 会话锁）。"""
        with self._sem, self._session_lock(session):
            is_group = self._reader.last_is_group(session)
            if is_group is None:
                is_group = True

            last_seq = self._watermarks.get(session, 0)
            all_new = self._reader.get_new_since(session, last_seq)
            if all_new:
                self._watermarks[session] = max(m.seq for m in all_new)
                self._save_watermarks()
            new_msgs = [m for m in all_new
                        if not getattr(m, "is_mine", False)
                        and getattr(m, "content_type", "text")
                        not in ("time_divider", "system")]
            # 超龄消息不作回复触发（2026-08-27 218 事故：重启深采把 8/11
            # 的 231 条积压当新消息，回复了两周前的 @我）。判定用发送时刻
            # ts_hint（时间分割线解析），缺失回退采集时刻；超龄消息仍进
            # 历史/记忆，只是不触发回复。阈值热控 reply_max_age_h（默认 6h）。
            max_age_s = float(self._rt("reply_max_age_h", 6)) * 3600
            now = self._clock()
            fresh = []
            n_stale = 0
            for m in new_msgs:
                mt = getattr(m, "ts_hint", 0) or getattr(m, "ts", 0)
                if mt and now - mt > max_age_s:
                    n_stale += 1
                else:
                    fresh.append(m)
            if n_stale:
                log.info("[%s] %d 条超龄消息（>%.0fh）不作回复触发",
                         session, n_stale, max_age_s / 3600)
            new_msgs = fresh
            if extra_msgs:
                new_msgs = new_msgs + list(extra_msgs)

            unreplied = self._policy.unreplied_mentions(session, new_msgs)
            if not new_msgs and not mention_hint and not unreplied \
                    and not force:
                log.info("[%s] 无新消息，跳过", session)
                return

            if self._rt("prompt_attach_images", True):
                # 图片直发多模态（_llm_loop 里 media_enrich 附带 image_url），
                # 不再做 vision→文字描述写回（2026-08-27 用户定稿）
                pass
            else:
                n_targets = sum(1 for m in new_msgs
                                if self._media.needs_convert(m))
                n_converted = self._media.convert_all(session, new_msgs)
                if n_targets:
                    self._journal("media_convert", session=session,
                                  ok=n_converted, total=n_targets)

            history = self._reader.get_context(
                session, n=self._rt("history_size", 200))
            trigger = "有人@我" if (mention_hint or unreplied) else "新消息"
            t0 = time.monotonic()
            self._journal("decision_start", session=session, trigger=trigger,
                          new_messages=len(new_msgs))

            reply_sent = self._llm_loop(session, is_group, trigger, history,
                                        new_msgs)

            if not reply_sent and self._policy.must_reply(
                    session, is_group, new_msgs):
                log.warning("[%s] 必回但未回复，重试一次", session)
                reply_sent = self._llm_loop(
                    session, is_group, trigger + "（必须回复）",
                    history, new_msgs)
                if not reply_sent:
                    self._submit_bundle(
                        session, Policy.fallback_bundle(session))
            if reply_sent:
                for m in unreplied:
                    self._policy.mark_replied(session, m)
            self._journal("decision_end", session=session,
                          replied=bool(reply_sent),
                          elapsed_ms=int((time.monotonic() - t0) * 1000))

            if reply_sent and new_msgs:
                self._memory_svc.schedule_extraction(
                    session, history, new_msgs)

    # ================================================================== LLM 循环
    @staticmethod
    def _supports_image_chat(provider) -> bool:
        """provider 的 chat 是否支持 OpenAI content 数组（image_url 直发图片）。

        GeminiProvider 走 generateContent（content 数组不支持）→ False；
        Kimi/DeepSeek 等 OpenAI 兼容 provider（provider.base 模块）→ True。
        """
        return type(provider).__module__.endswith("provider.base")

    def _llm_loop(self, session, is_group, trigger, history, new_msgs) -> bool:
        """LLM 调用循环：生成 → 解析 → 路由；tool 块回灌续生成。"""
        tool_feedback = ""
        replied = False
        fb = self._search_feedback.pop(session, "")
        if fb:
            tool_feedback = fb
        provider = self._providers.provider_for(session)
        opts = self._providers.session_options(session)
        memory_block = (self._memory_svc.block(session, is_group, history,
                                               new_msgs)
                        if opts["include_memory"] else "")
        hist_prompt = history if opts["include_history"] else []
        known_sessions = self.known_sessions()

        # prompt 多媒体增强（2026-08-27 用户定稿）：链接正文追加到 prompt
        # 尾部（带磁盘缓存）；图片以 image_url 直发多模态模型。
        from .media_enrich import enrich, attach_images
        tail_text, image_paths = enrich(hist_prompt, new_msgs, self._rt)
        attach_ok = bool(self._rt("prompt_attach_images", True)) \
            and bool(image_paths) and self._supports_image_chat(provider)

        def _call(msgs):
            if hasattr(provider, "chat_full"):
                return provider.chat_full(msgs)
            return provider.chat(msgs), ""

        for _round in range(MAX_TOOL_CALLS + 2):
            messages = self._builder.build(
                session, is_group, trigger, hist_prompt, new_msgs,
                tool_feedback=tool_feedback,
                running_tasks=self._ledger.running_for(session),
                memory_block=memory_block,
                known_sessions=known_sessions,
                goal=opts["goal"])
            if tail_text:
                messages[-1]["content"] = \
                    (messages[-1].get("content") or "") + "\n\n" + tail_text
            attached = 0
            if attach_ok:
                messages, attached = attach_images(messages, image_paths)
            self._journal("prompt", session=session, round=_round,
                          system=self._clip("\n\n".join(
                              m.get("content", "") for m in messages
                              if m.get("role") == "system")),
                          user=self._clip("\n\n".join(
                              m.get("content", "") for m in messages
                              if m.get("role") == "user"
                              and isinstance(m.get("content"), str))))
            try:
                out, thinking = _call(messages)
            except Exception as e:  # noqa: BLE001
                if attached:
                    # 非多模态模型/图片超限：去图重建降级重试一次，
                    # 本次决策后续 round 不再带图
                    log.warning("[%s] 带图调用失败(%s: %s)，去图降级重试",
                                session, type(e).__name__, e)
                    attach_ok = False
                    messages = self._builder.build(
                        session, is_group, trigger, hist_prompt, new_msgs,
                        tool_feedback=tool_feedback,
                        running_tasks=self._ledger.running_for(session),
                        memory_block=memory_block,
                        known_sessions=known_sessions,
                        goal=opts["goal"])
                    if tail_text:
                        messages[-1]["content"] = \
                            (messages[-1].get("content") or "") \
                            + "\n\n" + tail_text
                    try:
                        out, thinking = _call(messages)
                    except Exception as e2:  # noqa: BLE001
                        log.warning("[%s] LLM 调用失败: %s: %s",
                                    session, type(e2).__name__, e2)
                        return replied
                else:
                    log.warning("[%s] LLM 调用失败: %s: %s",
                                session, type(e).__name__, e)
                    return replied
            self._providers.note_cache(provider)
            self._journal("llm_output", session=session, round=_round,
                          output=self._clip(out),
                          thinking=self._clip(thinking) if thinking else "",
                          cache=provider.cache_stats(),
                          model=getattr(provider, "model", "?"))
            blocks = [b for b in extract_blocks(out) if b.valid]
            if not blocks:
                log.warning("[%s] 输出无合法块，重试一次", session)
                tool_feedback += ("\n[系统提示] 上次输出不是合法的 XML 动作块，"
                                  "请只输出协议规定的块。")
                continue

            reply_blocks, task_blocks = [], []
            had_tool = False
            for b in blocks:
                if b.tag == "reply":
                    reply_blocks.append(b)
                elif b.tag == "task":
                    task_blocks.append(b)
                elif b.tag == "tool":
                    tool_feedback += "\n[工具返回] " + self._tools.exec(b, session)
                    had_tool = True
                elif b.tag == "silent":
                    pass

            ref_map = {f"m{i + 1}": m for i, m in enumerate(new_msgs)}
            deliveries = []
            for b in reply_blocks[:MAX_REPLY_BLOCKS]:
                self._inject_quote_for_at(b, ref_map)
                xml = self.block_to_xml(b, session)
                target = parse_attrs(b.attrs).get("session") or session
                ok = self._submit_bundle(target, xml).ok
                deliveries.append({"session": target, "ok": bool(ok)})
                if ok:
                    replied = True
            for b in task_blocks[:MAX_TASK_BLOCKS]:
                self.start_task(session, b)
            self._journal("route", session=session,
                          blocks=[b.tag for b in blocks], deliveries=deliveries)

            if had_tool:
                continue
            return replied or bool(task_blocks)
        return replied

    # ================================================================== 块处理
    def _inject_quote_for_at(self, block, ref_map: dict):
        """reply 的 ref 指向 @我 消息且块内无 <quote> 时，注入引用标记。"""
        refs = parse_attrs(block.attrs).get("ref", "")
        if not refs or "<quote" in block.raw_inner:
            return
        first_ref = refs.split("+")[0].strip()
        target = ref_map.get(first_ref)
        if target is None or not self._policy.is_at_me(target):
            return
        snippet = (getattr(target, "content", "") or "")[:12]
        if not snippet:
            return
        esc = (snippet.replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;").replace('"', "&quot;"))
        block.raw_inner = f'<quote match="{esc}"/>' + block.raw_inner
        log.info("为 @我 消息 %s 的回复自动注入引用", first_ref)

    @staticmethod
    def _norm_session_attr(attr_session: str, current: str) -> str:
        """归一化块上的 session 属性（剥 LLM 抄来的类型后缀）。"""
        import re as _re
        from ...shared.name_match import _name_match
        s = _re.sub(r"[（(][^）)]*[）)]$", "", (attr_session or "").strip())
        if not s:
            return current
        return current if _name_match(s, current) else s

    def block_to_xml(self, block, current_session: str = "") -> str:
        """Block → 原始 XML 文本（用 raw_inner 原样转发）。"""
        attrs = block.attrs.rstrip()
        if current_session:
            norm = self._norm_session_attr(
                parse_attrs(attrs).get("session", ""), current_session)
            if "session=" in attrs:
                import re as _re
                attrs = _re.sub(r'session="[^"]*"', f'session="{norm}"', attrs)
            else:
                attrs = f'{attrs} session="{norm}"'
        if block.self_closing:
            return f"<{block.tag}{attrs}/>"
        return f"<{block.tag}{attrs}>{block.raw_inner}</{block.tag}>"

    # ================================================================== 搜索回灌
    def handle_search_done(self, session: str, query: str,
                           results=None, error=None):
        """搜索结果回灌：把结果作为工具反馈，触发该会话再决策一轮。"""
        from ..search.backend import SearchService
        if error:
            feedback = f"\n[工具返回] websearch('{query}') 失败: {error}"
        else:
            feedback = (f"\n[工具返回] websearch('{query}') 结果:\n"
                        + SearchService.format_results(results or []))
        self._journal("tool_result", session=session, tool="websearch",
                      op=query[:60], ok=not bool(error), error=error or "",
                      result=self._clip(feedback))
        self._search_feedback[session] = feedback
        log.info("[%s] websearch 结果回灌: query=%s", session, query)
        self.decide_session(session, mention_hint=False, force=True)

    # ================================================================== 会话名单
    def known_sessions(self) -> list:
        """已知会话名单（[(name, is_group)]）。查询失败降级空列表。"""
        try:
            fn = getattr(self._reader, "known_sessions", None)
            return fn() if callable(fn) else []
        except Exception:  # noqa: BLE001
            log.exception("known_sessions 查询失败（降级空名单）")
            return []

    # ================================================================== 任务
    def start_task(self, session: str, block):
        """<task> 块 → 登记台账 → 后台 subprocess 执行 → 完成回执入队。"""
        attrs = parse_attrs(block.attrs)
        refs = [r for r in (attrs.get("ref") or "").split("+") if r]
        brief_text = block.inner.strip()
        dup = self._ledger.find_similar(session, attrs.get("desc", ""))
        if dup is not None:
            log.info("[%s] 相似任务 %s(%s) 已存在，跳过重复委派: %s",
                     session, dup["task_id"], dup["status"],
                     (attrs.get("desc") or "")[:30])
            self._journal("task_dup_skipped", session=session,
                          desc=attrs.get("desc", ""), dup_of=dup["task_id"])
            return
        task = self._ledger.register(
            session=session, refs=refs, ref_briefs=[],
            desc=attrs.get("desc", ""), deliver=attrs.get("deliver", "reply"))
        self._journal("task_start", task_id=task["task_id"], session=session,
                      desc=task["desc"])
        brief = TaskBrief(goal=brief_text,
                          context=self._brief_context(session),
                          deliver=attrs.get("deliver", "reply"))

        def _work():
            full_brief = (f"{TASK_BRIEF_PREAMBLE}\n\n"
                          f"【本次任务】\n{brief.goal}\n\n"
                          f"【相关背景】\n{brief.context}")
            mm = parse_attrs(block.attrs).get("mm") == "1"
            model = self._rt("tool_model_mm", "kimi-code/kimi-for-coding") \
                if mm else self._rt("tool_model_text", "deepseek/v4-pro")
            result = self._cli.run(full_brief, task["workdir"], model=model,
                                   timeout_s=self._rt("task_timeout_s", 1800))
            self._ledger.finish(task["task_id"], result.ok,
                                result.cli_session_id)
            with open(os.path.join(task["workdir"], "result.txt"),
                      "w", encoding="utf-8") as f:
                f.write(result.summary)
            self._push_event({"type": "task_done",
                              "task_id": task["task_id"],
                              "session": session, "ts": self._clock()})

        threading.Thread(target=_work, name=f"task-{task['task_id']}",
                         daemon=True).start()
        log.info("[%s] 任务已启动: %s", session, task["task_id"])

    def _brief_context(self, session: str, n: int = 20) -> str:
        try:
            rows = self._reader.get_context(session, n=n)
            return "\n".join(f"{r.sender}: {(r.content or '')[:100]}"
                             for r in rows)
        except Exception:  # noqa: BLE001
            return ""

    def handle_task_done(self, task_id: str):
        """任务完成：拼回执 → 再决策一轮 → 人格化告诉用户。"""
        task = self._ledger.get(task_id)
        if not task:
            return
        session = task["session"]
        self._journal("task_done", task_id=task_id, session=session,
                      desc=task.get("desc", ""), ok=task["status"] == "done")
        try:
            with open(os.path.join(task["workdir"], "result.txt"),
                      encoding="utf-8") as f:
                summary = f.read()
        except OSError:
            summary = ""
        deliverables = [ln.split("DELIVERABLE:", 1)[1].strip()
                        for ln in summary.splitlines()
                        if "DELIVERABLE:" in ln]
        brief_text = "\n".join(ln for ln in summary.splitlines()
                               if "DELIVERABLE:" not in ln)
        receipt = {
            "task_id": task_id, "ref": "+".join(task["refs"]),
            "ref_brief": "; ".join(task["ref_briefs"]),
            "desc": task["desc"],
            "result": ("成功。" if task["status"] == "done" else "失败。")
                      + brief_text[:300],
            "deliverables": "\n".join(deliverables) or "（无）",
        }
        with self._sem, self._session_lock(session):
            is_group = self._reader.last_is_group(session)
            history = self._reader.get_context(
                session, n=self._rt("history_size", 200))
            messages = self._builder.build_task_receipt(
                session, bool(is_group), receipt, history)
            self._journal("prompt", session=session, round="receipt",
                          system=self._clip("\n\n".join(
                              m.get("content", "") for m in messages
                              if m.get("role") == "system")),
                          user=self._clip("\n\n".join(
                              m.get("content", "") for m in messages
                              if m.get("role") == "user")))
            provider = self._providers.provider_for(session)
            if hasattr(provider, "chat_full"):
                out, thinking = provider.chat_full(messages)
            else:
                out, thinking = provider.chat(messages), ""
            self._providers.note_cache(provider)
            self._journal("llm_output", session=session, round="receipt",
                          output=self._clip(out),
                          thinking=self._clip(thinking) if thinking else "",
                          cache=provider.cache_stats(),
                          model=getattr(provider, "model", "?"))
            delivered = []
            for b in extract_blocks(out):
                if b.valid and b.tag == "reply":
                    xml = self.block_to_xml(b, session)
                    target = parse_attrs(b.attrs).get("session") or session
                    ok = self._submit_bundle(target, xml).ok
                    delivered.append({"session": target, "ok": bool(ok)})
            self._journal("route", session=session, blocks=["receipt_reply"],
                          deliveries=delivered)
