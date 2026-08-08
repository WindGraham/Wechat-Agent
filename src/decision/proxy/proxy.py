# -*- coding: utf-8 -*-
"""proxy/proxy.py — 决策层运行时：LLM 唯一的对话对象，三层唯一的交汇点。

职责（docs/DECISION_LAYER.md §一）：
  出向路由（reply→交互层 / task→工具层）、进程执行与监听（CLI subprocess）、
  入向汇聚（LogUpdated/任务回执）、并发管理（信号量+会话锁）、任务台账、
  媒体转换队列。

对接口编程：reader / submit_bundle 由入口依赖注入，Proxy 不知道任何
交互层/工具层的具体实现。
"""

import json
import logging
import os
import threading
import time

from ...shared.types import LogUpdated, TaskBrief
from ...shared.xml_blocks import extract_blocks, parse_attrs
from ..prompt import ContextBuilder
from ..policy import Policy
from .cli_backend import get_backend
from .media import MediaConverter
from .tasks import TaskLedger

log = logging.getLogger("decision.proxy")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
WATERMARKS_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                               "watermarks.json")

MAX_TOOL_CALLS = 3            # chat_history 每轮最多 3 次
MAX_REPLY_BLOCKS = 3          # 一轮最多 3 个 <reply>（防刷屏）
MAX_TASK_BLOCKS = 1           # 一轮最多 1 个 <task>

# 事件类型
EV_LOG_UPDATED = "log_updated"
EV_TASK_DONE = "task_done"


class Proxy:
    """决策层运行时。

    reader: 交互层读取接口（get_context/get_new_since/update_content/
            last_is_group）
    submit_bundle: fn(session, xml) -> ActionResult（交互层动作出口）
    provider: LLMProvider；runtime: RuntimeConfig；policy/builder 可注入
    """

    def __init__(self, provider, reader, submit_bundle, runtime,
                 builder: ContextBuilder = None, policy: Policy = None,
                 cli_backend=None, clock=time.time,
                 watermarks_path=WATERMARKS_PATH, tasks_root=None):
        self._provider = provider
        self._reader = reader
        self._submit_bundle = submit_bundle
        self._runtime = runtime
        self._builder = builder or ContextBuilder(
            owner=getattr(runtime, "get", lambda k, d=None: d)("owner", ""))
        self._policy = policy or Policy(
            owner=self._rt("owner", ""), owner_nick=self._rt("owner_nick", ""))
        self._cli = cli_backend or get_backend(
            default_model=self._rt("tool_model", "kimi-code/k3"))
        self._ledger = TaskLedger(tasks_root) if tasks_root else TaskLedger()
        self._media = MediaConverter(
            provider, writer=self._write_back,
            max_workers=self._rt("media_convert_concurrency", 2))
        self._clock = clock
        self._watermarks_path = watermarks_path

        # 并发结构
        self._events = []                    # 事件队列（优先级排序）
        self._ev_lock = threading.Lock()
        self._sem = threading.Semaphore(
            self._rt("max_concurrent_decisions", 1))
        self._session_locks = {}             # session -> Lock
        self._watermarks = self._load_watermarks()
        self._stop = threading.Event()

    def _rt(self, key, default=None):
        get = getattr(self._runtime, "get", None)
        return get(key, default) if get else default

    # ================================================================== 入向
    def notify_log_updated(self, ev: LogUpdated):
        """交互层日志更新通知入口。"""
        self._push_event({
            "type": EV_LOG_UPDATED, "session": ev.session,
            "version": ev.version, "mention": ev.mention_hint,
            "ts": self._clock(),
        })

    def notify_owner_command(self, text: str):
        """主人指令（最高优先级）。"""
        self._push_event({"type": EV_LOG_UPDATED,
                          "session": self._rt("owner", ""),
                          "version": 0, "mention": True, "owner": True,
                          "ts": self._clock()})

    def _push_event(self, ev: dict):
        with self._ev_lock:
            # 同会话同类事件合并（保留最新）
            self._events = [e for e in self._events
                            if not (e["type"] == ev["type"]
                                    and e["session"] == ev["session"])]
            self._events.append(ev)
            self._events.sort(key=lambda e: (
                0 if e.get("owner") else 1 if e.get("mention")
                else 2 if e["type"] == EV_TASK_DONE else 3,
                e["ts"]))

    # ================================================================== 主循环
    def run_forever(self, poll_s: float = 0.5):
        """事件循环（阻塞；入口以线程方式运行）。"""
        while not self._stop.is_set():
            ev = self._pop_event()
            if ev is None:
                self._stop.wait(poll_s)
                continue
            try:
                self._handle(ev)
            except Exception:  # noqa: BLE001
                log.exception("事件处理失败: %s", ev)

    def run_once(self):
        """处理一个事件（测试/单轮用）。"""
        ev = self._pop_event()
        if ev is not None:
            self._handle(ev)
            return True
        return False

    def stop(self):
        self._stop.set()

    def _pop_event(self):
        with self._ev_lock:
            return self._events.pop(0) if self._events else None

    def _handle(self, ev: dict):
        if self._rt("paused", False):
            log.info("paused，事件跳过: %s", ev.get("session"))
            return
        if ev["type"] == EV_LOG_UPDATED:
            self._decide_session(ev["session"], mention_hint=ev.get("mention"))
        elif ev["type"] == EV_TASK_DONE:
            self._handle_task_done(ev["task_id"])

    # ================================================================== 决策
    def _session_lock(self, session: str) -> threading.Lock:
        with self._ev_lock:
            return self._session_locks.setdefault(
                session, threading.Lock())

    def _decide_session(self, session: str, mention_hint: bool = False):
        """一个会话的一次决策（信号量 + 会话锁）。"""
        with self._sem, self._session_lock(session):
            is_group = self._reader.last_is_group(session)
            if is_group is None:
                is_group = True               # 未知保守按群聊

            last_seq = self._watermarks.get(session, 0)
            new_msgs = self._reader.get_new_since(session, last_seq)
            if new_msgs:
                self._watermarks[session] = max(
                    m.seq for m in new_msgs)
                self._save_watermarks()

            # @我 逐条必回（Policy）
            unreplied = self._policy.unreplied_mentions(session, new_msgs)
            if not new_msgs and not mention_hint and not unreplied:
                log.info("[%s] 无新消息，跳过", session)
                return

            # 媒体转换：新消息里的未标注多媒体先转文字（限并发）
            self._media.convert_all(session, new_msgs)

            history = self._reader.get_context(
                session, n=self._rt("history_size", 200))
            trigger = "有人@我" if (mention_hint or unreplied) else "新消息"

            reply_sent = self._llm_loop(
                session, is_group, trigger, history, new_msgs)

            # 必回场景：私聊/@我/主人 —— 没回就重试一次，再沉默用兜底
            if not reply_sent and self._policy.must_reply(
                    session, is_group, new_msgs):
                log.warning("[%s] 必回但未回复，重试一次", session)
                reply_sent = self._llm_loop(
                    session, is_group, trigger + "（必须回复）",
                    history, new_msgs)
                if not reply_sent:
                    self._submit_bundle(
                        session, Policy.fallback_bundle(session))
            # 登记已回复的 @
            if reply_sent:
                for m in unreplied:
                    self._policy.mark_replied(session, m)

    def _llm_loop(self, session, is_group, trigger, history, new_msgs) -> bool:
        """LLM 调用循环：生成 → 解析 → 路由；tool 块回灌续生成。
        返回是否发出了回复。"""
        tool_feedback = ""
        tool_calls = 0
        replied = False
        for _round in range(MAX_TOOL_CALLS + 2):
            messages = self._builder.build(
                session, is_group, trigger, history, new_msgs,
                tool_feedback=tool_feedback)
            try:
                out = self._provider.chat(messages)
            except Exception as e:  # noqa: BLE001
                # LLM 调用失败（网络/限额/超时）：本轮放弃，不影响其他事件
                log.warning("[%s] LLM 调用失败: %s: %s",
                            session, type(e).__name__, e)
                return replied
            blocks = [b for b in extract_blocks(out) if b.valid]
            if not blocks:
                log.warning("[%s] 输出无合法块，重试一次", session)
                tool_feedback += ("\n[系统提示] 上次输出不是合法的 XML 动作块，"
                                  "请只输出协议规定的块。")
                continue

            reply_blocks, task_blocks = [], []
            for b in blocks:
                if b.tag == "reply":
                    reply_blocks.append(b)
                elif b.tag == "task":
                    task_blocks.append(b)
                elif b.tag == "tool" and tool_calls < MAX_TOOL_CALLS:
                    tool_calls += 1
                    tool_feedback += "\n[工具返回] " + self._exec_tool(b)
                elif b.tag == "silent":
                    pass

            if tool_feedback and not reply_blocks and not task_blocks:
                continue                       # 只有工具调用：回灌续生成

            # 终止输出：路由执行
            for b in reply_blocks[:MAX_REPLY_BLOCKS]:
                xml = self._block_to_xml(b)
                if self._submit_bundle(session, xml).ok:
                    replied = True
            for b in task_blocks[:MAX_TASK_BLOCKS]:
                self._start_task(session, b)
            return replied or bool(task_blocks)
        return replied

    # ---------------------------------------------------------------- 块处理
    @staticmethod
    def _block_to_xml(block) -> str:
        """Block → 原始 XML 文本（用 raw_inner 原样转发：inner 已反转义，
        再转义会把 <text> 结构标签变成字面量发到微信里）。"""
        attrs = block.attrs.rstrip()
        if block.self_closing:
            return f"<{block.tag}{attrs}/>"
        return f"<{block.tag}{attrs}>{block.raw_inner}</{block.tag}>"

    def _exec_tool(self, block) -> str:
        """执行 <tool> 块（当前只有 chat_history）。"""
        attrs = parse_attrs(block.attrs)
        if attrs.get("name") != "chat_history":
            return f"未知工具: {attrs.get('name', '?')}"
        session = attrs.get("session", "")
        keyword = attrs.get("keyword", "")
        n = min(int(attrs.get("n", 20)), 50)
        rows = self._reader.get_context(session, n=500)
        hits = [r for r in rows
                if keyword and keyword in (r.content or "")]
        if not hits:
            return f"未找到包含 '{keyword}' 的消息"
        return (f"找到 {len(hits)} 条（最近 {n} 条）:\n" + "\n".join(
            f"{r.sender}: {(r.content or '')[:150]}" for r in hits[-n:]))

    # ---------------------------------------------------------------- 任务
    def _start_task(self, session: str, block):
        """<task> 块 → 登记台账 → 后台 subprocess 执行 → 完成回执入队。"""
        attrs = parse_attrs(block.attrs)
        refs = [r for r in (attrs.get("ref") or "").split("+") if r]
        brief_text = block.inner.strip()
        task = self._ledger.register(
            session=session, refs=refs,
            ref_briefs=[], desc=attrs.get("desc", ""),
            deliver=attrs.get("deliver", "reply"))
        brief = TaskBrief(goal=brief_text,
                          context=self._brief_context(session),
                          deliver=attrs.get("deliver", "reply"))

        def _work():
            full_brief = (f"{brief.goal}\n\n相关背景：\n{brief.context}\n\n"
                          "最后一律用一句话总结：成了什么、交付物在哪"
                          "（本机绝对路径）、需要告诉用户什么。")
            result = self._cli.run(full_brief, task["workdir"])
            self._ledger.finish(task["task_id"], result.ok,
                                result.cli_session_id)
            with open(os.path.join(task["workdir"], "result.txt"),
                      "w", encoding="utf-8") as f:
                f.write(result.summary)
            self._push_event({"type": EV_TASK_DONE,
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

    def _handle_task_done(self, task_id: str):
        """任务完成：拼回执 → 再决策一轮 → 人格化告诉用户。"""
        task = self._ledger.get(task_id)
        if not task:
            return
        session = task["session"]
        try:
            with open(os.path.join(task["workdir"], "result.txt"),
                      encoding="utf-8") as f:
                summary = f.read()
        except OSError:
            summary = ""
        receipt = {
            "task_id": task_id, "ref": "+".join(task["refs"]),
            "ref_brief": "; ".join(task["ref_briefs"]),
            "desc": task["desc"],
            "result": ("成功。" if task["status"] == "done" else "失败。")
                      + summary[:300],
        }
        with self._sem, self._session_lock(session):
            is_group = self._reader.last_is_group(session)
            history = self._reader.get_context(
                session, n=self._rt("history_size", 200))
            messages = self._builder.build_task_receipt(
                session, bool(is_group), receipt, history)
            out = self._provider.chat(messages)
            for b in extract_blocks(out):
                if b.valid and b.tag == "reply":
                    self._submit_bundle(session, self._block_to_xml(b))

    # ---------------------------------------------------------------- 媒体写回
    def _write_back(self, session, sender, old_content, new_content):
        self._reader.update_content(session, sender, old_content,
                                    new_content)

    # ---------------------------------------------------------------- 水位
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
