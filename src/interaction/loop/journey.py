# -*- coding: utf-8 -*-
"""journey.py — 旅程管理器：处理一个会话条目的完整流程。

旅程规则（见 docs/INTERACTION_LAYER.md §5）：
  进入会话 → 同步日志 → 执行全部待办行动
  → 执行期间新来行动一并吸收 → 最后一次日志同步
  → 向 Proxy 发 LogUpdated → 退出

铁律：只要进入一个会话，就必须完成一次日志更新回传才能退出。
LogUpdated 只在行动清空后发送。

同步失败处理（铁律兜底）：重试 2 次（随机间隔 1~2s），仍失败允许退出，
但该会话标记 dirty，下一轮循环优先补同步（不允许把 agent 卡死在单个会话里）。

行动吸收不设单会话驻留上限（文档明确"暂不设计"）。ABSORB_FUSE_ROUNDS
只是防死循环保险丝（对端不停发行动的极端场景），不是驻留限制：
行动未清空前不许离场，只有连续吸收达到保险丝轮数才强制退出，
剩余行动留在队列里下轮继续。
"""

import logging
import random
import time
from typing import Optional, Callable

from .unified_queue import UnifiedQueue, QueueEntry

log = logging.getLogger("interaction.journey")

SYNC_MAX_RETRIES = 2             # 日志同步失败后的重试次数
ABSORB_FUSE_ROUNDS = 20          # 行动吸收防死循环保险丝（非驻留上限，见模块 docstring）


class JourneyManager:
    """旅程管理器：编排进入会话到退出的完整流程。

    依赖注入：
    - queue: 统一时间序队列
    - session_reader: 会话读取器（SessionReader）
    - bundle_sender: 动作包发送器（BundleSender）
    - port_navigator: 端口导航器（Navigator）
    - on_log_updated: LogUpdated 回调（通知决策层 Proxy）
    """

    def __init__(self, queue: UnifiedQueue, session_reader,
                 bundle_sender, port_navigator,
                 on_log_updated: Optional[Callable] = None,
                 config=None, friend_ops=None):
        self._queue = queue
        self._reader = session_reader
        self._sender = bundle_sender
        self._nav = port_navigator
        self._on_log_updated = on_log_updated
        # 好友申请处理（2026-08-09）：config 热读 friend_auto_accept 等；
        # friend_ops 可注入 {"probe": fn, "accept": fn}（测试用假实现）
        self._config = config
        self._friend_ops = friend_ops
        self._on_friend_result = None

        self._dirty: set = set()   # 同步失败的会话：下轮循环优先补同步

        # 可注入（测试用）
        self._sleep = time.sleep
        self._rand = random.uniform

    # ------------------------------------------------------------------ 公开访问
    def set_on_log_updated(self, cb):
        """LogUpdated 回调热替换（决策层装配时接线用）。"""
        self._on_log_updated = cb

    def set_on_friend_result(self, cb):
        """好友申请处理结果回调（决策层装配时接线用）。"""
        self._on_friend_result = cb
    @property
    def navigator(self):
        """端口导航器（供 run_loop 乱逛等场景使用）。"""
        return self._nav

    def take_dirty_sessions(self) -> list:
        """取出并清空 dirty 会话集合（run_loop 每轮开头调用，优先补同步）。"""
        dirty = sorted(self._dirty)
        self._dirty.clear()
        return dirty

    # ------------------------------------------------------------------ 旅程入口
    def process_entry(self, entry: QueueEntry) -> bool:
        """处理一个队列条目：完整的进入→同步→行动→退出旅程。

        返回 True 表示有发送动作。
        """
        # 好友申请条目：不是聊天会话，走专门流程（不进会话/不同步日志）
        if entry.kind == "friend":
            return self._process_friend(entry)

        session = entry.session
        log.info("=== journey start: %s (kind=%s mention=%s) ===",
                 session, entry.kind, entry.mention)

        # 1. 进入会话（队列中同会话的通知条目被行动吞并，此处不再处理）
        r = self._nav.enter_session(session)
        if not r.success:
            log.error("[%s] enter_session failed: %s", session, r.error)
            # 进不去先重排（队列硬上限 2×max_attempts 后丢弃，
            # 幻影会话不会无限循环，瞬时失败也有机会）
            self._handle_action_failure(entry, retryable=True)
            return False

        is_group = self._detect_is_group(session)

        # 好友申请伪装会话（用户 2026-08-09 实测）：新申请在首页以
        # "头像+昵称+打招呼"出现，红点在昵称条目上；点开后该位置变回
        # "新的朋友"但申请仍待通过。这类条目进入后落在新的朋友/验证页，
        # 不是聊天会话——转好友申请流程，不做日志同步。
        if self._on_friend_page():
            log.info("[%s] 进入后落在好友申请页，转 friend 流程", session)
            return self._process_friend(entry)

        # 2. 同步日志（进会话必做，铁律）；失败重试，仍失败 → 标 dirty 退出
        updated = self._sync_with_retry(session, is_group)
        if updated is None:
            log.warning("[%s] sync failed after retries, exit and mark dirty",
                        session)
            self._dirty.add(session)
            # 行动永远不许丢：带进会话的行动条目原样退回队列重试
            self._handle_action_failure(entry, retryable=True)
            self._safe_back_home(session)
            return False
        self._dirty.discard(session)

        sent = False

        # 3. 执行行动（如果有）
        action_failed = False
        if entry.kind == "action" and entry.payload:
            sent = self._execute_actions(session, entry)
            action_failed = not sent

        # 4. Follow-up：处理期间新来的行动一并吸收，直到行动清空。
        #    不设驻留上限；ABSORB_FUSE_ROUNDS 仅为防死循环保险丝。
        #    行动失败已重排队列的，本旅程不再吸收（防止刚重排的失败行动
        #    在同一旅程里被立刻弹出热重试，空转到硬上限被丢弃）。
        for round_i in range(ABSORB_FUSE_ROUNDS):
            if action_failed:
                break
            followup = self._queue.pop_session(session)
            if followup is None or followup.kind != "action":
                break
            log.info("[%s] follow-up round %d", session, round_i + 1)

            # 再次同步（有新消息可能在处理期间到达）；失败不阻塞行动执行
            if self._reader.sync_session(session, is_group) is None:
                log.warning("[%s] follow-up sync failed (round %d)",
                            session, round_i + 1)
            if followup.payload:
                ok = self._execute_actions(session, followup)
                sent = ok or sent
                if not ok:
                    break
        else:
            # 保险丝熔断：行动未清空，但已连续吸收 ABSORB_FUSE_ROUNDS 轮。
            # 剩余行动仍在队列中，下轮循环继续处理。
            log.warning("[%s] absorb fuse blown (%d rounds), force exit",
                        session, ABSORB_FUSE_ROUNDS)

        # 5. 最后一次日志同步 + 发 LogUpdated（铁律：必须回传才能退出）
        final_updated = self._sync_with_retry(session, is_group)
        if final_updated is not None:
            self._dirty.discard(session)
            if self._on_log_updated:
                self._on_log_updated(final_updated)
        else:
            # 同步彻底失败：允许退出但标 dirty，下轮优先补同步
            log.warning("[%s] final sync failed, mark dirty", session)
            self._dirty.add(session)

        # 6. 回首页
        self._safe_back_home(session)

        log.info("=== journey end: %s sent=%s ===", session, sent)
        return sent

    # ------------------------------------------------------------------ 内部
    def _on_friend_page(self) -> bool:
        """当前是否停留在好友申请相关页（新的朋友列表/通过朋友验证）。"""
        try:
            state = self._nav.tools._snap()
        except Exception:  # noqa: BLE001
            return False
        title = (state.get("page", {}) or {}).get("title") or ""
        return "新的朋友" in title or "通过朋友验证" in title

    def _friend_cfg(self, key, default):
        get = getattr(self._config, "get", None)
        return get(key, default) if get else default

    def _load_friend_ops(self):
        """好友申请操作实现（延迟 import，测试可注入 friend_ops）。"""
        if self._friend_ops is not None:
            return self._friend_ops
        from ..ports.android.action import friend_requests
        return {"probe": friend_requests.probe_pending,
                "accept": friend_requests.accept_all}

    def _process_friend(self, entry: QueueEntry) -> bool:
        """好友申请处理流程：巡检计数 →（自动通过开启时）逐条通过 → 结果回传。

        与聊天旅程的区别：不进任何会话、不同步消息日志；屏幕占用发生在
        主循环分发线程内，与盯屏/旅程天然互斥（盯屏见非首页自动跳过）。
        结果通过 on_friend_result 回传决策层（Proxy 转成回执决策轮）。
        """
        log.info("=== friend journey start (sources=%s) ===",
                 sorted(entry.sources))
        ops = self._load_friend_ops()
        auto_accept = bool(self._friend_cfg("friend_auto_accept", True))
        max_accept = int(self._friend_cfg("friend_max_accept", 30))
        tools = self._nav.tools

        try:
            if auto_accept:
                result = ops["accept"](tools, max_accept=max_accept,
                                       sleep_fn=self._sleep)
                result["probed"] = None      # accept 内部自己数，不单独巡检
            else:
                count, names = ops["probe"](tools, sleep_fn=self._sleep)
                result = {"ok": count is not None, "accepted": [],
                          "remaining": count or 0, "probed": count,
                          "pending_names": names,
                          "error": None if count is not None else "巡检导航失败"}
        except Exception as e:  # noqa: BLE001
            log.exception("friend journey failed")
            result = {"ok": False, "accepted": [], "remaining": -1,
                      "probed": None, "error": f"{type(e).__name__}: {e}"}

        # 失败重排（retryable：导航/设备瞬时故障占多数）
        if not result.get("ok"):
            self._queue.requeue_entry(entry)

        # 有实质内容才回传决策层（巡检为 0 不打扰）
        interesting = (result.get("accepted") or
                       (result.get("probed") or 0) > 0 or
                       result.get("error"))
        if interesting and self._on_friend_result:
            try:
                self._on_friend_result(result)
            except Exception:  # noqa: BLE001
                log.exception("on_friend_result 回调失败")

        self._safe_back_home("新的朋友")
        log.info("=== friend journey end: %s ===", result)
        return bool(result.get("accepted"))

    def _sync_with_retry(self, session: str, is_group: bool):
        """同步日志，失败重试 SYNC_MAX_RETRIES 次（随机间隔 1~2s）。

        返回 LogUpdated；彻底失败返回 None。
        """
        for attempt in range(SYNC_MAX_RETRIES + 1):
            updated = self._reader.sync_session(session, is_group)
            if updated is not None:
                return updated
            if attempt < SYNC_MAX_RETRIES:
                delay = self._rand(1.0, 2.0)
                log.warning("[%s] sync failed, retry %d/%d in %.1fs",
                            session, attempt + 1, SYNC_MAX_RETRIES, delay)
                self._sleep(delay)
        return None

    def _execute_actions(self, session: str, entry: QueueEntry) -> bool:
        """执行会话的全部待办行动。返回是否发送成功。"""
        if not entry.payload:
            return False

        result = self._sender.submit_bundle(session, entry.payload)
        if result.ok:
            return True

        # 失败处理：以条目为单位重入队（保留重试计数，B2 修复）
        log.warning("[%s] action failed: %s (retryable=%s)",
                    session, result.error, result.retryable)
        if result.retryable:
            self._queue.requeue_entry(entry)
        return False

    def _handle_action_failure(self, entry: QueueEntry,
                               retryable: bool = True):
        """行动执行失败：retryable 才重排；不可重试（如会话不存在）丢弃，
        防止幻影会话名无限排尾循环（实测 JY君C 每分钟刷一次屏）。"""
        if entry.kind == "action" and entry.payload:
            if retryable:
                self._queue.requeue_entry(entry)
            else:
                log.warning("[%s] 行动不可重试，丢弃: %s",
                            entry.session, (entry.payload or "")[:60])

    def _safe_back_home(self, session: str):
        try:
            self._nav.back_to_home()
        except Exception:
            log.exception("[%s] back_to_home failed", session)

    def _detect_is_group(self, session: str, chat_title: str = "") -> bool:
        """检测会话是否为群聊（进入会话后调用，此时停留在聊天页）。

        优先用端口实测：群聊标题必带 "(人数)" 后缀，私聊没有
        （2026-08-09 新加好友 Road 被默认成群聊导致私聊沉默；
        state 的 page.title 被规整过会丢人数后缀，必须原始 OCR）。
        实测失败回退 DB 里的历史实测值，再不行保守默认 True。
        """
        probe = getattr(self._nav, "chat_is_group", None)
        if callable(probe):
            try:
                got = probe()
                if got is not None:
                    return bool(got)
            except Exception:
                log.exception("[%s] chat_is_group failed", session)
        getter = getattr(self._reader, "last_is_group", None)
        if callable(getter):
            try:
                is_group = getter(session)
                if is_group is not None:
                    return bool(is_group)
            except Exception:
                log.exception("[%s] last_is_group failed", session)
        return True  # 默认按群聊处理（更安全）
