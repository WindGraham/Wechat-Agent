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

MAX_TOOL_CALLS = 3            # 工具调用每轮上限(协议保留)
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

# 事件类型（定义见 events.py）
from .events import (EV_LOG_UPDATED, EV_TASK_DONE, EV_MEMORY_WARM,
                     EV_MEMORY_EXTRACT, EV_SPECIAL_RUN,
                     EV_SEARCH_DONE, EV_ASIDE,
                     same_event, sort_key)  # noqa: E402,F401


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
                 watermarks_path=WATERMARKS_PATH, tasks_root=None,
                 wechat_tools=None):
        self._provider = provider
        self._reader = reader
        self._submit_bundle = submit_bundle
        self._runtime = runtime
        self._wechat_tools = wechat_tools  # 朋友圈发帖等需要直接操作微信
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
        self._memory = None            # 懒加载：MemoryTool（避免 import 开销）
        self._memory_injector = None   # 懒加载：MemoryInjector（自动注入用）
        self._memory_extractor = None  # 懒加载：MemoryExtractor（后置提取）
        self._extract_provider = None  # 提取用便宜模型（独立于主决策 provider）
        self._websearch = None         # 懒加载：WebSearchTool
        self._search_feedback = {}     # session -> 待回灌的搜索结果文本
        self._clock = clock
        self._watermarks_path = watermarks_path

        # Special Scheduler：特殊 prompt 投硬币器
        from .special_scheduler import SpecialScheduler
        self._special_scheduler = SpecialScheduler(
            push_fn=self._push_event,
            configs=self._rt("special_prompts", {}),
            clock=self._clock)

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

    # ---------------------------------------------------------------- provider 热切换
    def set_provider(self, provider):
        """热替换决策 LLM provider（网关模型切换，2026-08-11 用户要求）。

        只换决策对话用的 provider；媒体转换器（MediaConverter）仍持有
        启动时的 provider 引用——切换成无图像能力的模型（如 deepseek v4）
        时，图片识别反而因此不受影响。
        """
        old = getattr(self._provider, "model", "?")
        self._provider = provider
        log.info("决策 provider 热切换: %s -> %s",
                 old, getattr(provider, "model", "?"))

    def set_extract_provider(self, provider):
        """设置记忆提取专用 provider（通常用便宜模型，如 deepseek）。

        不设置则回退到主决策 provider。
        """
        self._extract_provider = provider
        # 重置 extractor（下次懒加载用新 provider）
        self._memory_extractor = None
        log.info("提取 provider 已设置: %s", getattr(provider, "model", "?"))

    def _get_extract_provider(self):
        """取提取用 provider：专用 > 主决策 provider。"""
        return self._extract_provider or self._provider

    def provider_info(self) -> dict:
        """当前决策 provider 实况（网关展示用）。"""
        p = self._provider
        return {"model": getattr(p, "model", "?"),
                "url": getattr(p, "_url", ""),
                "token_floor": getattr(p, "_token_floor", 0),
                "token_ceiling": getattr(p, "_token_ceiling", 0)}

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

    def inject_task_done(self, task_id: str):
        """外部注入任务完成事件（进程外补跑的任务完成后由网关打入，
        走正常回执流程让她自己交付——2026-08-09 用户要求）。"""
        task = self._ledger.get(task_id)
        if not task:
            log.warning("inject_task_done: 找不到任务 %s", task_id)
            return False
        self._push_event({"type": EV_TASK_DONE, "task_id": task_id,
                          "session": task["session"], "ts": self._clock()})
        return True

    def inject_aside(self, session: str, text: str,
                     sender: str = None) -> bool:
        """旁注：网关直接对 proxy 输入一条消息（2026-08-10 用户要求）。

        等价于在目标会话里以指定发送者身份"说了"一句话，触发一次
        带该消息的决策。缺省发送者用 owner（主人），优先级最高。
        """
        if not text or not text.strip():
            log.warning("inject_aside: 空文本，忽略")
            return False
        sender = sender or self._rt("owner", "")
        self._push_event({"type": EV_ASIDE, "session": session,
                          "text": text.strip(), "sender": sender,
                          "ts": self._clock()})
        return True

    def warm_memory(self, session: str, history_batch: list):
        """冷启动记忆预热：喂一批聊天历史，让 agent 生成 memory（不回复）。

        可多次调用（分批）：每批独立事件、独立处理，互不干扰
        （同会话经会话锁串行，memory 去重保证不重复记）。
        history_batch: 消息列表（Message 或含 sender/content 的对象）。
        返回 True 表示已入队。"""
        self._push_event({
            "type": EV_MEMORY_WARM, "session": session,
            "history_batch": list(history_batch), "ts": self._clock(),
        })
        return True

    def _push_event(self, ev: dict):
        with self._ev_lock:
            # 同会话同类事件合并（保留最新）
            self._events = [e for e in self._events
                            if not same_event(e, ev)]
            self._events.append(ev)
            self._events.sort(key=sort_key)

    # ================================================================== 主循环
    def run_forever(self, poll_s: float = 0.5):
        """事件循环（阻塞；入口以线程方式运行）。"""
        last_tick = 0.0
        while not self._stop.is_set():
            ev = self._pop_event()
            if ev is None:
                self._stop.wait(poll_s)
                # 每分钟 tick 一次 scheduler
                now = self._clock()
                if now - last_tick >= 60.0:
                    self._special_scheduler.tick(now)
                    last_tick = now
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
        """事件分发：查处理器注册表（热插拔，新增事件 = 新处理器文件）。"""
        if self._rt("paused", False):
            log.info("paused，事件跳过: %s", ev.get("session"))
            return
        from .handlers import get_handler
        handler_cls = get_handler(ev["type"])
        if handler_cls is None:
            log.warning("未知事件类型: %s", ev.get("type"))
            return
        handler_cls().handle(self, ev)

    # ================================================================== 决策
    def _session_lock(self, session: str) -> threading.Lock:
        with self._ev_lock:
            return self._session_locks.setdefault(
                session, threading.Lock())

    # ================================================================== 记忆预热
    def _warm_memory(self, session: str, history_batch: list):
        """冷启动记忆预热：从一批历史提取值得记的记忆（不回复）。

        与正常决策的区别：
          - 专用 prompt 强制"只输出 memory 操作，不回复"
          - 解析后只执行 memory 工具（add/alias），忽略 reply/task
          - 会话锁串行（同会话批之间互不干扰）+ memory 去重（不重复记）
        """
        if not history_batch:
            log.warning("[%s] memory warm: 空批次，跳过", session)
            return
        with self._sem, self._session_lock(session):
            t0 = time.monotonic()
            _journal("memory_warm_start", session=session,
                     batch=len(history_batch))
            try:
                messages = self._builder.build_warmup(session, history_batch)
                if hasattr(self._provider, "chat_full"):
                    out, thinking = self._provider.chat_full(messages)
                else:
                    out, thinking = self._provider.chat(messages), ""
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] memory warm LLM 调用失败: %s: %s",
                            session, type(e).__name__, e)
                return
            _journal("llm_output", session=session, round="warm",
                     output=_clip(out),
                     thinking=_clip(thinking) if thinking else "")
            # 只执行 memory 工具块；reply/task 忽略（不回复、不委派）。
            # 非 memory 工具（如 websearch）也跳过：预热是后台整理，绝不能
            # 触发网络搜索 + 结果回灌再决策（那会真的回消息，2026-08-10 审查）
            executed = 0
            for b in extract_blocks(out):
                if b.valid and b.tag == "tool" \
                        and parse_attrs(b.attrs).get("name") == "memory":
                    result = self._exec_tool(b, session)
                    if not result.startswith("未知"):
                        executed += 1
                    log.info("[%s] memory warm: %s", session, result)
            _journal("memory_warm_end", session=session,
                     batch=len(history_batch), executed=executed,
                     elapsed_ms=int((time.monotonic() - t0) * 1000))
            log.info("[%s] memory warm 完成: %d 条历史, 执行 %d 个 memory 操作",
                     session, len(history_batch), executed)

    def _decide_session(self, session: str, mention_hint: bool = False,
                       force: bool = False, extra_msgs: list = None):
        """一个会话的一次决策（信号量 + 会话锁）。

        extra_msgs: 外部注入的消息（旁注），附加到本次决策的新消息列表
        尾部，让 LLM 在同一轮里看到。"""
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
            # 旁注附加：作为额外的"新消息"参与本轮决策
            if extra_msgs:
                new_msgs = new_msgs + list(extra_msgs)

            # @我 逐条必回（Policy）
            unreplied = self._policy.unreplied_mentions(session, new_msgs)
            if not new_msgs and not mention_hint and not unreplied \
                    and not force:
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

            # 后置记忆提取：agent 回复后异步提取本轮对话中的记忆
            # （不占用决策轮次，用便宜模型，merge 已有记忆）
            if reply_sent and new_msgs:
                self._schedule_memory_extraction(
                    session, history, new_msgs)

    def _llm_loop(self, session, is_group, trigger, history, new_msgs) -> bool:
        """LLM 调用循环：生成 → 解析 → 路由；tool 块回灌续生成。
        返回是否发出了回复。"""
        tool_feedback = ""
        tool_calls = 0
        replied = False
        # 搜索结果回灌：若有待回灌的搜索结果，作为工具反馈拼进首轮
        fb = self._search_feedback.pop(session, "")
        if fb:
            tool_feedback = fb
        # 记忆块：决策前自动拼接（L0 全局 + L2 当前会话 + L1 在场人）
        # 每轮循环都带（工具反馈轮也保持记忆上下文）
        memory_block = self._memory_block(session, is_group, history, new_msgs)
        # 已知会话名单：跨会话投递的准确目标名（2026-08-10 发错群事故）
        known_sessions = self._known_sessions()
        for _round in range(MAX_TOOL_CALLS + 2):
            messages = self._builder.build(
                session, is_group, trigger, history, new_msgs,
                tool_feedback=tool_feedback,
                running_tasks=self._ledger.running_for(session),
                memory_block=memory_block,
                known_sessions=known_sessions)
            _journal("prompt", session=session, round=_round,
                     system=_clip("\n\n".join(
                         m.get("content", "") for m in messages
                         if m.get("role") == "system")),
                     user=_clip("\n\n".join(
                         m.get("content", "") for m in messages
                         if m.get("role") == "user")))
            try:
                if hasattr(self._provider, "chat_full"):
                    out, thinking = self._provider.chat_full(messages)
                else:
                    out, thinking = self._provider.chat(messages), ""
            except Exception as e:  # noqa: BLE001
                # LLM 调用失败（网络/限额/超时）：本轮放弃，不影响其他事件
                log.warning("[%s] LLM 调用失败: %s: %s",
                            session, type(e).__name__, e)
                return replied
            _journal("llm_output", session=session, round=_round,
                     output=_clip(out),
                     thinking=_clip(thinking) if thinking else "")
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
                elif b.tag == "tool":
                    tool_feedback += "\n[工具返回] " + self._exec_tool(b, session)
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
                # 跨会话投递：块内 session 属性优先（"从A群获取→发到B群"），
                # 缺省回落到当前决策会话
                target = parse_attrs(b.attrs).get("session") or session
                ok = self._submit_bundle(target, xml).ok
                deliveries.append({"session": target, "ok": bool(ok)})
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

    # ---------------------------------------------------------------- 工具
    def _exec_tool(self, block, current_session: str = "") -> str:
        """执行 <tool> 块（决策层内联工具分发）。

        当前工具：
          - memory: 长期记忆读写（add/read/search/update/delete）
          - websearch: 查资料/搜索（本地段同步 + 网络段异步）
        current_session: 当前决策会话（memory scope 缺省时按此推断）。
        工具调用与结果均记 journal（网关"工具"三竖列展示，2026-08-11）。
        """
        attrs = parse_attrs(block.attrs)
        name = attrs.get("name", "")
        op = attrs.get("op", attrs.get("query", ""))
        t0 = time.time()
        result = ""
        if name == "memory":
            # is_group 用于 source 区分私聊/群聊（隐私边界：私聊内容绝不外泄）
            is_group = None
            try:
                if current_session:
                    is_group = self._reader.last_is_group(current_session)
            except Exception:  # noqa: BLE001
                is_group = None
            result = self._memory_tool().run(
                attrs, current_session=current_session, is_group=is_group)
        elif name == "websearch":
            result = self._exec_websearch(attrs, current_session)
        else:
            result = f"未知工具: {name}"
        _journal("tool_call", session=current_session,
                 tool=name, op=op[:60], attrs={k: str(v)[:120]
                                               for k, v in attrs.items()},
                 result=_clip(str(result)), elapsed_ms=int(
                     (time.time() - t0) * 1000))
        return result

    # ---------------------------------------------------------------- websearch
    def _websearch_tool(self):
        """懒加载 WebSearchTool（注入 memory_store/reader 供本地段）。"""
        if self._websearch is None:
            from ..memory import MemoryStore
            from ..search import SearchService, WebSearchTool
            self._websearch = WebSearchTool(
                search_service=SearchService(memory_store=MemoryStore(),
                                             reader=self._reader))
        return self._websearch

    def _exec_websearch(self, attrs: dict, session: str = "") -> str:
        """websearch 分流：local 段同步回灌；web 段起子线程异步。

        返回本地段文本 + 网络段状态提示（回灌给 LLM 的第一轮反馈）。
        """
        query = attrs.get("query", "")
        if not query:
            return "websearch 缺 query 属性"
        scope = attrs.get("scope", "all")
        tool = self._websearch_tool()

        # local 段（同步，毫秒）
        local_text = ""
        if scope in ("local", "all"):
            local_text = tool.run_local(attrs, session=session)

        # web 段（异步，子线程，不阻塞事件循环）
        if scope in ("web", "all"):
            def _work():
                try:
                    results = tool.run_web(attrs)
                    self._push_event({
                        "type": EV_SEARCH_DONE, "session": session,
                        "query": query, "results": results,
                        "ts": self._clock()})
                except Exception as e:  # noqa: BLE001
                    log.warning("websearch '%s' 失败: %s", query, e)
                    self._push_event({
                        "type": EV_SEARCH_DONE, "session": session,
                        "query": query, "error": f"{type(e).__name__}: {e}",
                        "ts": self._clock()})
            threading.Thread(target=_work, daemon=True,
                             name=f"websearch-{query[:10]}").start()
            tail = "\n[网络搜索进行中，结果回来后会通知你]"
        else:
            tail = ""

        return (local_text + tail).strip() or "（无本地记录）"

    def _handle_search_done(self, session: str, query: str,
                            results=None, error=None):
        """搜索结果回灌：把结果作为工具反馈，触发该会话再决策一轮。"""
        from ..search.backend import SearchService
        if error:
            feedback = f"\n[工具返回] websearch('{query}') 失败: {error}"
        else:
            feedback = (f"\n[工具返回] websearch('{query}') 结果:\n"
                        + SearchService.format_results(results or []))
        _journal("tool_result", session=session, tool="websearch",
                 op=query[:60], ok=not bool(error), error=error or "",
                 result=_clip(feedback))
        self._search_feedback[session] = feedback
        log.info("[%s] websearch 结果回灌: query=%s", session, query)
        # 触发该会话再决策一轮（带上搜索结果）——force: 无新消息也进
        # （否则"无新消息跳过"会让搜索结果永远不被消费，2026-08-10 实测）
        self._decide_session(session, mention_hint=False, force=True)

    def _memory_store(self):
        """懒加载 MemoryStore（共享实例，带向量存储）。

        injector / tool / extractor 共用同一个 store，避免向量存储不一致。
        """
        if not hasattr(self, "_memory_store_inst") or \
                self._memory_store_inst is None:
            from ..memory import MemoryStore, VectorStore
            from ..memory.store import DEFAULT_MEMORY_ROOT, DEFAULT_VECTOR_ROOT
            vs = VectorStore(DEFAULT_VECTOR_ROOT)
            self._memory_store_inst = MemoryStore(
                root=DEFAULT_MEMORY_ROOT, vector_store=vs)
        return self._memory_store_inst

    def _memory_tool(self):
        """懒加载 MemoryTool（避免 import 开销，仅首次调用时构造）。"""
        if self._memory is None:
            from ..memory import MemoryTool
            self._memory = MemoryTool(store=self._memory_store())
        return self._memory

    def _memory_block(self, session, is_group, history, new_msgs) -> str:
        """决策前自动拼接【记忆】块：L0 全局 + L2 当前会话 + L1 在场人。
        任何异常降级为空串（记忆是增强，不是决策的硬依赖）。"""
        try:
            if self._memory_injector is None:
                from ..memory.injector import MemoryInjector
                self._memory_injector = MemoryInjector(self._memory_store())
            return self._memory_injector.build_memory_block(
                session, is_group, history, new_msgs)
        except Exception:  # noqa: BLE001
            log.exception("memory 注入失败（降级空块）")
        return ""

    # ---------------------------------------------------------------- 后置记忆提取
    def _schedule_memory_extraction(self, session: str, history,
                                    new_msgs: list):
        """调度后置记忆提取事件（不阻塞当前决策）。

        把本轮对话文本 + 参与人列表打包成事件，由独立 handler 异步处理。
        """
        # 构建对话文本片段
        lines = []
        # 取最近 10 条历史 + 所有新消息
        context = (list(history[-10:]) if history else []) + list(new_msgs)
        for m in context:
            sender = getattr(m, "sender", "?")
            content = (getattr(m, "content", "") or "").replace("\n", " ")[:300]
            if getattr(m, "is_mine", False):
                sender = "我"
            lines.append(f"{sender}: {content}")

        # 提取参与人（非"我"的 sender）
        user_names = list(set(
            getattr(m, "sender", "") for m in context
            if getattr(m, "sender", "") and getattr(m, "sender", "") != "我"
            and not getattr(m, "is_mine", False)
        ))

        self._push_event({
            "type": EV_MEMORY_EXTRACT,
            "session": session,
            "conversation_text": "\n".join(lines),
            "user_names": user_names,
            "ts": self._clock(),
        })

    def _extract_memory(self, session: str, conversation_text: str,
                        user_names: list):
        """执行后置记忆提取（由 handler 调用）。

        从对话中提取记忆，与已有记忆合并（merge vs. add）。
        同时触发定期 consolidation 检查。
        """
        extractor = self._get_memory_extractor()
        if extractor is None:
            return
        try:
            n = extractor.extract_from_conversation(
                session, conversation_text, user_names)
            if n:
                log.info("[%s] 后置记忆提取: %d 条", session, n)
        except Exception:
            log.exception("[%s] 后置记忆提取失败", session)

        # 检查是否需要定期整合
        try:
            extractor.maybe_consolidate(self._clock())
        except Exception:
            log.exception("记忆整合检查失败")

    def _get_memory_extractor(self):
        """懒加载 MemoryExtractor（用提取专用/主 provider）。"""
        if self._memory_extractor is None:
            from ..memory import MemoryExtractor
            self._memory_extractor = MemoryExtractor(
                store=self._memory_store(),
                provider=self._get_extract_provider())
        return self._memory_extractor

    # ---------------------------------------------------------------- 特殊 prompt
    def _run_special(self, prompt_name: str, session: str):
        """执行一个特殊 prompt（由 SpecialRunHandler 调用）。

        根据 output_mode 分流：
          - memory: 记忆整合
          - task: LLM 生成 <task> → 委派 CLI
          - text: LLM 生成文本 → 保存文件
        """
        spec = self._builder._lib.load_special(prompt_name)
        if spec is None:
            log.warning("[%s] special prompt 加载失败", prompt_name)
            return

        meta = spec["meta"]
        mode = meta.get("output_mode", "memory")

        # 收集上下文
        ctx = self._collect_special_context(prompt_name, meta)
        ctx["time"] = time.strftime("%Y-%m-%d %H:%M %A",
                                    time.localtime(self._clock()))

        # 构建 messages
        messages = self._builder.build_special(prompt_name, ctx)
        if not messages:
            return

        # 调 LLM
        try:
            provider = self._get_extract_provider()  # 特殊 prompt 走便宜模型
            if hasattr(provider, "chat_full"):
                out, thinking = provider.chat_full(messages)
            else:
                out, thinking = provider.chat(messages), ""
        except Exception as e:
            log.warning("[%s] special LLM 调用失败: %s", prompt_name, e)
            return

        _journal("special_run", prompt=prompt_name, mode=mode,
                 output=_clip(out))

        # 按模式执行
        if mode == "memory":
            self._exec_special_memory(out, session)
        elif mode == "task":
            self._exec_special_task(out, session, meta)
        elif mode == "text":
            self._exec_special_text(out, prompt_name, meta)

    def _collect_special_context(self, prompt_name: str,
                                 meta: dict) -> dict:
        """收集特殊 prompt 需要的上下文数据。"""
        ctx = {}
        # 近期记忆
        mems = self._memory_store().list_scope("all", limit=30)
        if mems:
            ctx["memories"] = [
                {"source": m.get("_file", "?"),
                 "content": m.get("content", "")[:120]}
                for m in mems[:20]
            ]
        return ctx

    def _exec_special_memory(self, llm_output: str, session: str):
        """memory 模式：只执行记忆工具块。"""
        from ..memory.extractor import MemoryExtractor
        extractor = self._get_memory_extractor()
        n = extractor._execute_memory_blocks(llm_output, session)
        log.info("[special] memory 模式: 执行 %d 个操作", n)

    def _exec_special_task(self, llm_output: str, session: str,
                           meta: dict):
        """task 模式：先保存生成内容，再委派 task。"""
        from ...shared.xml_blocks import extract_blocks, parse_attrs

        # 提取 <task> 块
        blocks = [b for b in extract_blocks(llm_output)
                  if b.valid and b.tag == "task"]
        if not blocks:
            log.warning("[special] task 模式: 无 <task> 块")
            return

        for b in blocks:
            attrs = parse_attrs(b.attrs)
            desc = attrs.get("desc", "special task")
            target = meta.get("target", "")

            if target == "moments":
                self._exec_moments_task(b, desc, session)
            else:
                # 通用 task：交给 CLI backend
                self._start_task(session, b)

    def _exec_moments_task(self, block, desc: str, session: str):
        """执行朋友圈发布 task。

        从 task body 中提取日记文本，先保存本地，再调 post_text_moments 真发。
        """
        body = (block.raw_inner or "").strip()
        if not body:
            log.warning("[special] moments task body 为空")
            return

        # 提取日记文本（在 "发布文字动态：" 之后的部分）
        diary_text = body
        for marker in ("发布文字动态：", "发布文字动态:\n", "发布纯文字："):
            if marker in body:
                diary_text = body.split(marker, 1)[1].strip()
                break
        # 去掉可能的引号包裹
        diary_text = diary_text.strip().strip('"').strip("'").strip()

        if not diary_text:
            log.warning("[special] moments: 未提取到日记文本")
            return

        # 保存日记到文件
        from ..memory.store import DEFAULT_MEMORY_ROOT
        diary_dir = os.path.join(DEFAULT_MEMORY_ROOT, "cat_diary")
        os.makedirs(diary_dir, exist_ok=True)
        date_str = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
        diary_path = os.path.join(diary_dir, f"{date_str}.md")
        with open(diary_path, "a", encoding="utf-8") as f:
            f.write(f"\n---\n{diary_text}\n")
        log.info("[special] 日记已保存: %s", diary_path)

        # 真发朋友圈（持手机锁整段执行，防交互层插队）
        if self._wechat_tools is None:
            log.warning("[special] wechat_tools 未注入，跳过发朋友圈")
        else:
            lock = getattr(self._wechat_tools, "_phone_lock", None)
            acquired = lock.acquire(timeout=120) if lock else True
            if not acquired:
                log.warning("[special] 手机锁超时（交互层忙碌），跳过发朋友圈")
            else:
                try:
                    from ...interaction.ports.android.action.moments_poster \
                        import post_text_moments
                    log.info("[special] 开始发朋友圈...")
                    result = post_text_moments(self._wechat_tools, diary_text)
                    ok = result.get("ok") and result.get("posted")
                    log.info("[special] 朋友圈发布: %s (posted=%s)",
                             "成功" if ok else "失败",
                             result.get("posted", False))
                except Exception as e:
                    log.exception("[special] 朋友圈发布异常: %s", e)
                finally:
                    if lock and acquired:
                        lock.release()

        # 保存为 global memory（先于发朋友圈：即使发圈失败也不丢日记）
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(self._clock()))
        try:
            self._memory_store().add(
                content=f"[{ts_str}] 猫娘日记：{diary_text}",
                key="猫娘日记",
                scope="global",
                source="cat_diary",
                confidence=1.0)
        except Exception:
            log.debug("日记 memory 写入失败", exc_info=True)

    def _exec_special_text(self, llm_output: str, prompt_name: str,
                           meta: dict):
        """text 模式：保存文本到文件。"""
        save_dir = os.path.join(
            PROJECT_ROOT, "workspace", "memory", prompt_name)
        os.makedirs(save_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S",
                           time.localtime(self._clock()))
        path = os.path.join(save_dir, f"{ts}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(llm_output.strip())
        log.info("[special] text 模式: 已保存 %s", path)

    def _known_sessions(self) -> list:
        """已知会话名单（[(name, is_group)]）：跨会话投递的准确目标名。
        reader 不支持/查询失败时降级空列表（增强项，不是硬依赖）。"""
        try:
            fn = getattr(self._reader, "known_sessions", None)
            return fn() if callable(fn) else []
        except Exception:  # noqa: BLE001
            log.exception("known_sessions 查询失败（降级空名单）")
            return []

    # ---------------------------------------------------------------- 任务
    def _start_task(self, session: str, block):
        """<task> 块 → 登记台账 → 后台 subprocess 执行 → 完成回执入队。"""
        attrs = parse_attrs(block.attrs)
        refs = [r for r in (attrs.get("ref") or "").split("+") if r]
        brief_text = block.inner.strip()
        # 委派前去重：同会话相似任务在执行中/刚完成——发出去的图不进文字
        # 历史，LLM 容易以为没办成而重复委派（2026-08-08 海报实测）
        dup = self._ledger.find_similar(session, attrs.get("desc", ""))
        if dup is not None:
            log.info("[%s] 相似任务 %s(%s) 已存在，跳过重复委派: %s",
                     session, dup["task_id"], dup["status"],
                     (attrs.get("desc") or "")[:30])
            _journal("task_dup_skipped", session=session,
                     desc=attrs.get("desc", ""), dup_of=dup["task_id"])
            return
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
            # 按任务多模态需求路由模型（2026-08-09 用户指定）：
            # mm=1 → K2.7 Coding（有 image_in/video_in），否则 V4 Pro
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
            # DELIVERABLE 行单独抽全量路径：塞在 summary[:300] 里会被截断，
            # 截断的路径她照抄 → 发文件报"本地文件不存在"（2026-08-09 实测）
            "deliverables": "\n".join(deliverables) or "（无）",
        }
        with self._sem, self._session_lock(session):
            is_group = self._reader.last_is_group(session)
            history = self._reader.get_context(
                session, n=self._rt("history_size", 200))
            messages = self._builder.build_task_receipt(
                session, bool(is_group), receipt, history)
            _journal("prompt", session=session, round="receipt",
                     system=_clip("\n\n".join(
                         m.get("content", "") for m in messages
                         if m.get("role") == "system")),
                     user=_clip("\n\n".join(
                         m.get("content", "") for m in messages
                         if m.get("role") == "user")))
            if hasattr(self._provider, "chat_full"):
                out, thinking = self._provider.chat_full(messages)
            else:
                out, thinking = self._provider.chat(messages), ""
            _journal("llm_output", session=session, round="receipt",
                     output=_clip(out),
                     thinking=_clip(thinking) if thinking else "")
            delivered = []
            for b in extract_blocks(out):
                if b.valid and b.tag == "reply":
                    # 跨会话投递：块内 session 属性优先（回执也可能带文件
                    # 发给别的会话，如"简历发到 canglang"），缺省回落当前
                    xml = self._block_to_xml(b, session)
                    target = parse_attrs(b.attrs).get("session") or session
                    ok = self._submit_bundle(target, xml).ok
                    delivered.append({"session": target, "ok": bool(ok)})
            _journal("route", session=session, blocks=["receipt_reply"],
                     deliveries=delivered)

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
