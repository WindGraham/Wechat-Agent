# -*- coding: utf-8 -*-
"""unified_queue.py — 统一时间序队列（交互层核心数据结构）。

规则（见 docs/INTERACTION_LAYER.md §5）：
1. 去重不挪动：会话已在队列中 → 位置不变、内容不更新
2. 行动吞并通知：执行会话行动时，该会话的通知条目直接删除
3. @我/主人插队：含 @我 的条目直接插到队首
4. 行动失败有尝试上限（2 次），耗尽后排到队尾
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("interaction.queue")


@dataclass
class QueueEntry:
    """统一时间序队列中的一条记录。"""
    kind: str = ""             # "notify" | "action"
    session: str = ""
    ts: float = 0.0            # 入队时间
    mention: bool = False      # @我/主人 → 插队队首
    payload: str = ""          # action 时为 XML bundle 原文；notify 时为空
    attempts: int = 0          # 已尝试次数
    sources: set = field(default_factory=set)  # {"sweep", "notify", "heartbeat"}

    @property
    def is_priority(self) -> bool:
        """是否为优先条目（@我/主人）。"""
        return self.mention


class UnifiedQueue:
    """统一时间序队列：通知与行动合二为一，按时间排序。

    线程安全：所有公共方法加锁。
    """

    def __init__(self, max_attempts: int = 2):
        self._entries: dict[str, QueueEntry] = {}  # session → entry
        self._lock = threading.Lock()
        self._max_attempts = max_attempts

    # ------------------------------------------------------------------ 入队
    def push_notify(self, session: str, mention: bool = False,
                    source: str = "sweep") -> Optional[QueueEntry]:
        """推送通知条目。

        规则：会话已在队列中 → 位置不变、内容不更新（只更新 sources）。
        """
        ts = time.time()
        with self._lock:
            existing = self._entries.get(session)
            if existing is not None:
                # 已在队列中：不挪动，只登记来源和 @我 粘滞
                existing.sources.add(source)
                if mention:
                    existing.mention = True
                log.debug("queue: %s already queued (kind=%s), sources=%s",
                          session, existing.kind, sorted(existing.sources))
                return existing

            entry = QueueEntry(
                kind="notify", session=session, ts=ts,
                mention=mention, sources={source},
            )
            self._entries[session] = entry
            log.info("queue push notify: %s mention=%s source=%s",
                     session, mention, source)
            return entry

    def push_action(self, session: str, blocks_xml: str,
                    mention: bool = False) -> Optional[QueueEntry]:
        """推送行动条目（来自决策层 submit_bundle）。

        规则：如果该会话已有通知条目 → 升级为行动条目（吞并通知）。
        如果已有行动条目 → 合并（追加 payload，但保持位置）。
        """
        ts = time.time()
        with self._lock:
            existing = self._entries.get(session)
            if existing is not None:
                # 已在队列中：吞并通知 → 升级为行动
                if existing.kind == "notify":
                    log.info("queue: action swallows notify for %s", session)
                existing.kind = "action"
                existing.payload = (existing.payload or "") + blocks_xml
                existing.attempts = 0  # 重置尝试次数
                if mention:
                    existing.mention = True
                return existing

            entry = QueueEntry(
                kind="action", session=session, ts=ts,
                mention=mention, payload=blocks_xml,
            )
            self._entries[session] = entry
            log.info("queue push action: %s mention=%s", session, mention)
            return entry

    # ------------------------------------------------------------------ 出队
    def pop_next(self) -> Optional[QueueEntry]:
        """取出下一条待处理条目。

        优先级：@我/主人 > 按入队时间 FIFO。
        取出即从队列中移除。
        """
        with self._lock:
            if not self._entries:
                return None
            # 排序：priority first, then FIFO
            entries = sorted(
                self._entries.values(),
                key=lambda e: (0 if e.is_priority else 1, e.ts),
            )
            entry = entries[0]
            del self._entries[entry.session]
            return entry

    def pop_session(self, session: str) -> Optional[QueueEntry]:
        """取出指定会话的条目（旅程开始时调用），同时清除同会话的通知条目。"""
        with self._lock:
            return self._entries.pop(session, None)

    # ------------------------------------------------------------------ 重试
    def requeue_action(self, session: str, blocks_xml: str) -> Optional[QueueEntry]:
        """行动失败后重新入队。

        - 未达上限：保持原位置重试
        - 达到上限：排到队尾（作为新条目）
        """
        with self._lock:
            existing = self._entries.get(session)
            if existing and existing.kind == "action":
                existing.attempts += 1
                if existing.attempts >= self._max_attempts:
                    # 耗尽：排到队尾
                    del self._entries[session]
                    new_entry = QueueEntry(
                        kind="action", session=session,
                        ts=time.time(),
                        mention=existing.mention,
                        payload=blocks_xml,
                        attempts=0,
                    )
                    self._entries[session] = new_entry
                    log.warning("queue: %s max attempts reached, requeued at tail",
                                session)
                    return new_entry
                else:
                    # 未达上限：保持原 payload
                    existing.payload = blocks_xml
                    log.info("queue: %s retry %d/%d",
                             session, existing.attempts, self._max_attempts)
                    return existing
            else:
                # 全新入队
                return self.push_action(session, blocks_xml)

    # ------------------------------------------------------------------ 查询
    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, session: str) -> bool:
        with self._lock:
            return session in self._entries

    def snapshot(self) -> list:
        """返回队列快照（按出队顺序排列）。"""
        with self._lock:
            entries = sorted(
                self._entries.values(),
                key=lambda e: (0 if e.is_priority else 1, e.ts),
            )
            return [QueueEntry(
                kind=e.kind, session=e.session, ts=e.ts,
                mention=e.mention, payload=e.payload,
                attempts=e.attempts, sources=set(e.sources),
            ) for e in entries]

    def describe(self) -> str:
        """进 prompt 的一行描述。"""
        entries = self.snapshot()
        if not entries:
            return ""
        return " | ".join(
            f"{e.session}({e.kind}"
            + ("@我" if e.mention else "")
            + ")"
            for e in entries
        )
