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
EVENTS_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                           "proxy_events.jsonl")
EVENTS_MAX_BYTES = 2 * 1024 * 1024     # jsonl 超过则截断保留后半
PROMPT_JOURNAL_LIMIT = 30000           # prompt/llm_output 超过则截断标注


def _clip(text: str, limit: int = PROMPT_JOURNAL_LIMIT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，全文 {len(text)} 字符]"


def _journal(event_type: str, **data):
    """往 workspace/runtime/proxy_events.jsonl 追加一条事件（网关实况页读取）。
    观测性通道：任何失败只记日志，绝不影响决策主流程。"""
    try:
        rec = {"ts": time.time(), "type": event_type}
        rec.update(data)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
        if (os.path.isfile(EVENTS_PATH)
                and os.path.getsize(EVENTS_PATH) > EVENTS_MAX_BYTES):
            _truncate_events()
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        log.exception("journal 写入失败: %s", event_type)


def _truncate_events():
    """jsonl 超限：截断保留后半（按行对齐，丢掉被截断的半行）。"""
    with open(EVENTS_PATH, "rb") as f:
        data = f.read()
    half = data[len(data) // 2:]
    nl = half.find(b"\n")
    half = half[nl + 1:] if nl != -1 else b""
    with open(EVENTS_PATH, "wb") as f:
        f.write(half)

MAX_TOOL_CALLS = 3            # chat_history 每轮最多 3 次
MAX_REPLY_BLOCKS = 3          # 一轮最多 3 个 <reply>（防刷屏）
MAX_TASK_BLOCKS = 1           # 一轮最多 1 个 <task>

# 工具层简报 preamble：向 CLI 核心说明处境与输出格式（2026-08-08 用户要求：
# kimicode 必须知道自己是在替一个微信人格 agent 动手，最终文本会被程序解析）
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
            all_new = self._reader.get_new_since(session, last_seq)
            if all_new:
                self._watermarks[session] = max(
                    m.seq for m in all_new)
                self._save_watermarks()
            # 过滤：自己的消息/时间线/系统消息不进决策（否则自己刚发的
            # 回复会再次触发决策 → 空转/自答循环，2026-08-08 实测）
            new_msgs = [m for m in all_new
                        if not getattr(m, "is_mine", False)
                        and getattr(m, "content_type", "text")
                        not in ("time_divider", "system")]

            # @我 逐条必回（Policy）
            unreplied = self._policy.unreplied_mentions(session, new_msgs)
            if not new_msgs and not mention_hint and not unreplied:
                log.info("[%s] 无新消息，跳过", session)
                return

            # 媒体转换：新消息里的未标注多媒体先转文字（限并发）
            n_targets = sum(1 for m in new_msgs
                            if self._media.needs_convert(m))
            n_converted = self._media.convert_all(session, new_msgs)
            if n_targets:
                _journal("media_convert", session=session,
                         ok=n_converted, total=n_targets)

            history = self._reader.get_context(
                session, n=self._rt("history_size", 200))
            trigger = "有人@我" if (mention_hint or unreplied) else "新消息"
            t0 = time.monotonic()
            _journal("decision_start", session=session, trigger=trigger,
                     new_messages=len(new_msgs))

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
            _journal("decision_end", session=session,
                     replied=bool(reply_sent),
                     elapsed_ms=int((time.monotonic() - t0) * 1000))

    def _llm_loop(self, session, is_group, trigger, history, new_msgs) -> bool:
        """LLM 调用循环：生成 → 解析 → 路由；tool 块回灌续生成。
        返回是否发出了回复。"""
        tool_feedback = ""
        tool_calls = 0
        replied = False
        for _round in range(MAX_TOOL_CALLS + 2):
            messages = self._builder.build(
                session, is_group, trigger, history, new_msgs,
                tool_feedback=tool_feedback,
                running_tasks=self._ledger.running_for(session))
            _journal("prompt", session=session, round=_round,
                     system=_clip("\n\n".join(
                         m.get("content", "") for m in messages
                         if m.get("role") == "system")),
                     user=_clip("\n\n".join(
                         m.get("content", "") for m in messages
                         if m.get("role") == "user")))
            try:
                out = self._provider.chat(messages)
            except Exception as e:  # noqa: BLE001
                # LLM 调用失败（网络/限额/超时）：本轮放弃，不影响其他事件
                log.warning("[%s] LLM 调用失败: %s: %s",
                            session, type(e).__name__, e)
                return replied
            _journal("llm_output", session=session, round=_round,
                     output=_clip(out))
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
            # @我 消息强制引用回复：reply 的 ref 指向 @我 消息但块内没有
            # <quote> 时自动注入（LLM 偶尔忘记，2026-08-08 用户要求）
            ref_map = {f"m{i + 1}": m for i, m in enumerate(new_msgs)}
            deliveries = []
            for b in reply_blocks[:MAX_REPLY_BLOCKS]:
                self._inject_quote_for_at(b, ref_map)
                xml = self._block_to_xml(b, session)
                ok = self._submit_bundle(session, xml).ok
                deliveries.append({"session": session, "ok": bool(ok)})
                if ok:
                    replied = True
            for b in task_blocks[:MAX_TASK_BLOCKS]:
                self._start_task(session, b)
            _journal("route", session=session,
                     blocks=[b.tag for b in blocks], deliveries=deliveries)
            return replied or bool(task_blocks)
        return replied

    # ---------------------------------------------------------------- 块处理
    def _inject_quote_for_at(self, block, ref_map: dict):
        """reply 的 ref 指向 @我 消息且块内无 <quote> 时，注入引用标记。
        match 取目标消息内容前 12 字符（XML 转义）。"""
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
        """归一化块上的 session 属性：LLM 会从 prompt 里抄上
        "（群聊）/（私聊）" 后缀（实测），剥掉；与当前会话匹配则用规范名。"""
        import re as _re
        from ...shared.name_match import _name_match
        s = _re.sub(r"[（(][^）)]*[）)]$", "", (attr_session or "").strip())
        if not s:
            return current
        return current if _name_match(s, current) else s

    def _block_to_xml(self, block, current_session: str = "") -> str:
        """Block → 原始 XML 文本（用 raw_inner 原样转发：inner 已反转义，
        再转义会把 <text> 结构标签变成字面量发到微信里）。"""
        attrs = block.attrs.rstrip()
        # session 属性归一化（剥 LLM 抄来的类型后缀）
        if current_session:
            norm = self._norm_session_attr(
                parse_attrs(attrs).get("session", ""), current_session)
            if "session=" in attrs:
                import re as _re
                attrs = _re.sub(r'session="[^"]*"', f'session="{norm}"',
                                attrs)
            else:
                attrs = f'{attrs} session="{norm}"'
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
        _journal("task_start", task_id=task["task_id"], session=session,
                 desc=task["desc"])
        brief = TaskBrief(goal=brief_text,
                          context=self._brief_context(session),
                          deliver=attrs.get("deliver", "reply"))

        def _work():
            full_brief = (f"{TASK_BRIEF_PREAMBLE}\n\n"
                          f"【本次任务】\n{brief.goal}\n\n"
                          f"【相关背景】\n{brief.context}")
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
        _journal("task_done", task_id=task_id, session=session,
                 desc=task.get("desc", ""), ok=task["status"] == "done")
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
                    self._submit_bundle(session, self._block_to_xml(b, session))

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
