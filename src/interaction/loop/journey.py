# -*- coding: utf-8 -*-
"""journey.py — 旅程管理器：处理一个会话条目的完整流程。

旅程规则（见 docs/INTERACTION_LAYER.md §5）：
  进入会话 → 同步日志 → 执行全部待办行动
  → 执行期间新来行动一并吸收 → 最后一次日志同步
  → 向 Proxy 发 LogUpdated → 退出

铁律：只要进入一个会话，就必须完成一次日志更新回传才能退出。
LogUpdated 只在行动清空后发送。
"""

import logging
import time
from typing import Optional, Callable

from .unified_queue import UnifiedQueue

log = logging.getLogger("interaction.journey")

MAX_SESSION_DWELL = 120.0      # 单会话最长驻留秒数
MAX_FOLLOWUP_ROUNDS = 2        # follow-up 最多追加轮数


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
                 on_log_updated: Optional[Callable] = None):
        self._queue = queue
        self._reader = session_reader
        self._sender = bundle_sender
        self._nav = port_navigator
        self._on_log_updated = on_log_updated

    # ------------------------------------------------------------------ 旅程入口
    def process_entry(self, entry) -> bool:
        """处理一个队列条目：完整的进入→同步→行动→退出旅程。

        返回 True 表示有发送动作。
        """
        session = entry.session
        deadline = time.time() + MAX_SESSION_DWELL
        log.info("=== journey start: %s (kind=%s mention=%s) ===",
                 session, entry.kind, entry.mention)

        # 1. 进入会话（队列中同会话的通知条目被行动吞并，此处不再处理）
        r = self._nav.enter_session(session)
        if not r.success:
            log.error("[%s] enter_session failed: %s", session, r.error)
            self._handle_action_failure(entry)
            return False

        is_group = self._detect_is_group(session)

        # 2. 同步日志（进会话必做）
        updated = self._reader.sync_session(session, is_group)
        if updated is None:
            log.warning("[%s] sync failed, exiting", session)
            self._nav.back_to_home()
            return False

        sent = False

        # 3. 执行行动（如果有）
        if entry.kind == "action" and entry.payload:
            sent = self._execute_actions(session, entry)

        # 4. Follow-up：处理期间新来的行动（最多 2 轮）
        for round_i in range(MAX_FOLLOWUP_ROUNDS):
            if time.time() > deadline:
                log.warning("[%s] dwell > %ds, force exit",
                            session, MAX_SESSION_DWELL)
                break
            followup = self._queue.pop_session(session)
            if followup is None or followup.kind != "action":
                break
            log.info("[%s] follow-up round %d", session, round_i + 1)

            # 再次同步（有新消息可能在处理期间到达）
            updated2 = self._reader.sync_session(session, is_group)
            if followup.payload:
                sent = self._execute_actions(session, followup) or sent

        # 5. 最后一次日志同步 + 发 LogUpdated
        final_updated = self._reader.sync_session(session, is_group)
        if final_updated and self._on_log_updated:
            self._on_log_updated(final_updated)

        # 6. 回首页
        try:
            self._nav.back_to_home()
        except Exception:
            log.exception("[%s] back_to_home failed", session)

        log.info("=== journey end: %s sent=%s ===", session, sent)
        return sent

    # ------------------------------------------------------------------ 内部
    def _execute_actions(self, session: str, entry) -> bool:
        """执行会话的全部待办行动。返回是否发送成功。"""
        if not entry.payload:
            return False

        result = self._sender.submit_bundle(session, entry.payload)
        if result.ok:
            return True

        # 失败处理
        log.warning("[%s] action failed: %s (retryable=%s)",
                    session, result.error, result.retryable)
        if result.retryable:
            self._queue.requeue_action(session, entry.payload)
        return False

    def _handle_action_failure(self, entry):
        """行动执行失败（进会话失败等）。"""
        if entry.kind == "action" and entry.payload:
            self._queue.requeue_action(entry.session, entry.payload)

    def _detect_is_group(self, session: str) -> bool:
        """检测会话是否为群聊（从端口状态读取）。"""
        try:
            # 从端口 reader 的最新 state 中获取
            state = getattr(self._reader._pr, 'frame_bus', None)
            if state:
                latest = state.latest()
                if latest:
                    page = latest.get("page", {})
                    return bool(page.get("is_group") or page.get("member_count"))
        except Exception:
            pass
        return True  # 默认按群聊处理（更安全）
