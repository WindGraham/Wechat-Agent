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

from ...shared.types import LogUpdated
from ..prompt import ContextBuilder
from ..policy import Policy
from .cli_backend import get_backend
from .decider import Decider
from .media import MediaConverter
from .memory_service import MemoryService
from .providers import ProviderRegistry
from .special_executor import SpecialExecutor
from .tasks import TaskLedger
from .tool_executor import ToolExecutor

log = logging.getLogger("decision.proxy")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
WATERMARKS_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                               "watermarks.json")
EVENTS_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                           "proxy_events.jsonl")
EVENTS_MAX_BYTES = 2 * 1024 * 1024     # jsonl 超限则归档轮转（旧文件改名留存，全量不丢）
PROMPT_JOURNAL_LIMIT = 30000           # 单条 prompt/llm_output 超长则截断标注


def _clip(text: str, limit: int = PROMPT_JOURNAL_LIMIT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，全文 {len(text)} 字符]"


def _journal(event_type: str, **data):
    """往 workspace/runtime/proxy_events.jsonl 追加一条事件（网关实况页读取）。
    观测性通道：任何失败只记日志，绝不影响决策主流程。
    超限处理 = 归档轮转：旧文件改名 proxy_events.jsonl.<ts> 完整留存，
    新文件从头写——**所有数据都不丢**（2026-08-12 修复：原实现截断丢前半，
    与「留存所有数据」要求不符）。"""
    try:
        rec = {"ts": time.time(), "type": event_type}
        rec.update(data)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
        if (os.path.isfile(EVENTS_PATH)
                and os.path.getsize(EVENTS_PATH) > EVENTS_MAX_BYTES):
            _rotate_events()
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        log.exception("journal 写入失败: %s", event_type)


def _rotate_events():
    """jsonl 超限归档：把当前文件原子改名留存，新文件从头写。"""
    archive = f"{EVENTS_PATH}.{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(EVENTS_PATH, archive)
    except OSError:
        # 归档失败不丢本次写入：保持原文件继续追加（日志留痕）
        log.exception("journal 归档失败（保留原文件继续追加）: %s", archive)

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
                 emoji_index=None):
        self._providers = ProviderRegistry(provider, self._rt, clock=clock)
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
        self._memory_svc = MemoryService(
            extract_provider_fn=lambda: self._providers.extract_provider(),
            push_fn=self._push_event, clock=clock)
        self._tools = ToolExecutor(
            memory_svc=self._memory_svc, reader=reader,
            submit_bundle=submit_bundle, push_fn=self._push_event,
            clock=clock, journal_fn=_journal, clip_fn=_clip,
            emoji_index=emoji_index)
        self._special = SpecialExecutor(
            builder=self._builder,
            extract_provider_fn=lambda: self._providers.extract_provider(),
            memory_svc=self._memory_svc, tool_executor=self._tools,
            start_task_fn=self._start_task, clock=clock,
            journal_fn=_journal, clip_fn=_clip)
        self._clock = clock

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
        self._stop = threading.Event()

        # 决策循环（依赖 sem + session_lock + 水位）
        self._decider = Decider(
            reader=reader, submit_bundle=submit_bundle,
            builder=self._builder, policy=self._policy, media=self._media,
            ledger=self._ledger, providers=self._providers,
            memory_svc=self._memory_svc, tools=self._tools, cli=self._cli,
            search_feedback={}, clock=clock, rt=self._rt,
            sem=self._sem, session_lock_fn=self._session_lock,
            push_event=self._push_event, journal_fn=_journal, clip_fn=_clip,
            watermarks_path=watermarks_path)

    def _rt(self, key, default=None):
        get = getattr(self._runtime, "get", None)
        return get(key, default) if get else default

    # ---------------------------------------------------------------- provider（委托 ProviderRegistry）
    def set_provider(self, provider):
        """热替换决策 LLM provider（网关模型切换）。"""
        self._providers.set_provider(provider)

    def set_extract_provider(self, provider):
        """设置记忆提取专用 provider（不设置则回退主 provider）。"""
        self._providers.set_extract_provider(provider)
        self._memory_svc.reset_extractor()

    def provider_info(self) -> dict:
        """当前决策 provider 实况（网关展示用）。"""
        return self._providers.provider_info()

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

    # ---------------------------------------------------------------- 决策（委托 Decider）
    def _warm_memory(self, session: str, history_batch: list):
        """冷启动记忆预热（薄委托）。"""
        self._decider.warm_memory(session, history_batch)

    def _decide_session(self, session: str, mention_hint: bool = False,
                        force: bool = False, extra_msgs: list = None):
        """一个会话的一次决策（薄委托）。"""
        self._decider.decide_session(session, mention_hint=mention_hint,
                                     force=force, extra_msgs=extra_msgs)

    def _handle_search_done(self, session: str, query: str,
                            results=None, error=None):
        """搜索结果回灌（薄委托）。"""
        self._decider.handle_search_done(session, query, results, error)

    def _start_task(self, session: str, block):
        """<task> 委派（薄委托，special executor 经此调用）。"""
        self._decider.start_task(session, block)

    def _handle_task_done(self, task_id: str):
        """任务回执再决策（薄委托）。"""
        self._decider.handle_task_done(task_id)

    # ---------------------------------------------------------------- 工具（委托 ToolExecutor）
    @property
    def _emoji_idx(self):
        """表情索引（薄委托：测试注入自定义索引实例）。"""
        return self._tools._emoji_idx

    @_emoji_idx.setter
    def _emoji_idx(self, value):
        self._tools._emoji_idx = value

    def _exec_emoji_search(self, attrs: dict) -> str:
        """emoji 工具（薄委托，测试直接调用）。"""
        return self._tools.exec_emoji_search(attrs)

    # ---------------------------------------------------------------- 媒体写回
    def _write_back(self, session, sender, old_content, new_content):
        self._reader.update_content(session, sender, old_content,
                                    new_content)
