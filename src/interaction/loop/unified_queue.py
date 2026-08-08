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
from typing import Optional

from ...shared.types import QueueEntry

log = logging.getLogger("interaction.queue")

# 系统/信息流"会话"黑名单：不是真实聊天，任何通道都不得入队
# （公众号/服务通知是 feed 不是对话；"微信"是桌面登录等系统通知的标题噪声；
#  "折叠的聊天"是折叠分组不是会话）
def _dedup_stutter(label: str, known_fn=None) -> str:
    """会话名归一：OCR/分段会产生各种变体（结巴拼接 "YOUSAOBLYOUSAOBI"、
    尾巴垃圾 "JY君C"、空格漂移 "怨憎会爱别离 …"）。按序尝试：
    1. 原样命中已知会话 → 直接用
    2. 去空白后命中 → 用库里的规范名
    3. 互为包含（短名被垃圾尾巴包裹）→ 用短的已知名
    4. 相似度 ≥0.75 → 用最像的已知名
    5. 结巴截半（两半相似 ≥0.8）→ 截半后再过一遍 1-4
    全都不中才原样返回。"""
    import difflib
    if not label:
        return label
    try:
        known = known_fn() or [] if known_fn else []
    except Exception:  # noqa: BLE001
        known = []

    def _snap(name):
        if name in known:
            return name
        best, best_r = None, 0.0
        for k in known:
            ka, kb = name.replace(" ", ""), k.replace(" ", "")
            if ka == kb:
                return k
            if len(k) >= 2 and (ka in kb or kb in ka):
                r = difflib.SequenceMatcher(None, ka, kb).ratio()
                if r > best_r:
                    best, best_r = k, max(r, 0.76)
            r = difflib.SequenceMatcher(None, ka, kb).ratio()
            if r > best_r:
                best, best_r = k, r
        return best if best_r >= 0.75 else None

    hit = _snap(label)
    if hit:
        return hit
    # 结巴截半
    n = len(label)
    if n >= 4 and n % 2 == 0:
        a, b = label[:n // 2], label[n // 2:]
        if a == b:
            return _snap(a) or a
        if difflib.SequenceMatcher(None, a, b).ratio() >= 0.8:
            return _snap(a) or a
    return label


BLOCKED_SESSIONS = frozenset({
    "微信", "微信团队", "微信支付", "服务通知", "公众号", "腾讯新闻",
    "微信运动", "QQ邮箱提醒", "腾讯充值", "微信游戏", "企业微信",
    "折叠的聊天",
})


class UnifiedQueue:
    """统一时间序队列：通知与行动合二为一，按时间排序。

    线程安全：所有公共方法加锁。
    """

    def __init__(self, max_attempts: int = 2, snapshot_path: str = None,
                 known_sessions_fn=None):
        self._entries: dict[str, QueueEntry] = {}  # session → entry
        # RLock：requeue_action 的"全新入队"分支会在持锁状态下调用 push_action
        self._lock = threading.RLock()
        self._max_attempts = max_attempts
        # 状态快照（网关只读展示用）：每次变更后写 workspace/runtime/queue.json
        self._snapshot_path = snapshot_path
        self._known_sessions_fn = known_sessions_fn   # 结巴标签对齐用

    # ------------------------------------------------------------------ 快照
    def _persist(self):
        """把当前队列快照写入 snapshot_path（网关只读文件边界）。"""
        if not self._snapshot_path:
            return
        import json
        import os
        try:
            entries = self.snapshot()
            data = [
                {"order": i + 1, "kind": e.kind, "session": e.session,
                 "mention": e.mention, "attempts": e.attempts,
                 "sources": sorted(e.sources), "ts": e.ts,
                 "payload": e.payload or "",
                 "payload_brief": (e.payload or "")[:80]}
                for i, e in enumerate(entries)
            ]
            os.makedirs(os.path.dirname(self._snapshot_path), exist_ok=True)
            tmp = self._snapshot_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._snapshot_path)
        except OSError:
            log.exception("queue snapshot 写入失败")

    def restore(self):
        """启动时从快照恢复队列（崩溃/重启不丢待办行动）。"""
        import json
        import os
        if not self._snapshot_path or not os.path.exists(self._snapshot_path):
            return 0
        try:
            with open(self._snapshot_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return 0
        n = 0
        with self._lock:
            for e in data:
                session = e.get("session")
                if not session or session in self._entries:
                    continue
                self._entries[session] = QueueEntry(
                    kind=e.get("kind", "notify"), session=session,
                    ts=e.get("ts", time.time()),
                    mention=bool(e.get("mention")),
                    payload=e.get("payload", ""),
                    attempts=int(e.get("attempts", 0)),
                    sources=set(e.get("sources", [])))
                n += 1
        if n:
            log.info("queue 从快照恢复 %d 个条目", n)
        return n

    # ------------------------------------------------------------------ 入队
    def push_notify(self, session: str, mention: bool = False,
                    source: str = "sweep") -> Optional[QueueEntry]:
        """推送通知条目。

        规则：会话已在队列中 → 位置不变、内容不更新（只更新 sources）。
        """
        session = _dedup_stutter(session, self._known_sessions_fn)
        if session in BLOCKED_SESSIONS:
            log.debug("queue: %s 被黑名单拦截（系统/feed 会话）", session)
            return None
        ts = time.time()
        with self._lock:
            existing = self._entries.get(session)
            if existing is not None:
                # 已在队列中：不挪动，只登记来源和 @我 粘滞。
                # 注意：mention 升级（@我 粘滞）是对"去重不挪动"的有意例外——
                # 它只改优先级标记，不改 ts，FIFO 位置保持不变；
                # 插队效果体现在 pop_next 的 priority 排序上，而非挪动条目。
                existing.sources.add(source)
                if mention:
                    existing.mention = True
                self._persist()
                log.debug("queue: %s already queued (kind=%s), sources=%s",
                          session, existing.kind, sorted(existing.sources))
                return existing

            entry = QueueEntry(
                kind="notify", session=session, ts=ts,
                mention=mention, sources={source},
            )
            self._entries[session] = entry
            self._persist()
            log.info("queue push notify: %s mention=%s source=%s",
                     session, mention, source)
            return entry

    def push_action(self, session: str, blocks_xml: str,
                    mention: bool = False) -> Optional[QueueEntry]:
        """推送行动条目（来自决策层 submit_bundle）。

        规则：如果该会话已有通知条目 → 升级为行动条目（吞并通知）。
        如果已有行动条目 → 合并（追加 payload，但保持位置）。
        """
        session = _dedup_stutter(session, self._known_sessions_fn)
        if session in BLOCKED_SESSIONS:
            log.debug("queue: %s 被黑名单拦截（系统/feed 会话）", session)
            return None
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
                self._persist()
                return existing

            entry = QueueEntry(
                kind="action", session=session, ts=ts,
                mention=mention, payload=blocks_xml,
            )
            self._entries[session] = entry
            self._persist()
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
            self._persist()
            return entry

    def pop_session(self, session: str) -> Optional[QueueEntry]:
        """取出指定会话的条目（旅程开始时调用），同时清除同会话的通知条目。"""
        with self._lock:
            entry = self._entries.pop(session, None)
            self._persist()
            return entry

    # ------------------------------------------------------------------ 重试
    def requeue_entry(self, entry: QueueEntry):
        """行动失败后把已出队的条目重新入队（保留重试计数）。

        - 未达上限：attempts+1，按原 ts 重新入队（保持原位置语义）
        - 达到上限（2 次）：排到队尾，**attempts 不归零**
        - 达到硬上限（2×上限）：彻底丢弃——防止幻影会话/持久故障
          无限排尾刷屏（实测 JY君C 每分钟刷一次）
        """
        with self._lock:
            entry.attempts += 1
            if entry.attempts >= self._max_attempts * 2:
                log.error("queue: %s 行动重试 %d 次仍失败，丢弃",
                          entry.session, entry.attempts)
                return None
            if entry.attempts >= self._max_attempts:
                # 耗尽：排到队尾（ts=now），attempts 保留继续计
                entry.ts = time.time()
                self._entries[entry.session] = entry
                self._persist()
                log.warning("queue: %s max attempts reached, requeued at tail",
                            entry.session)
                return entry
            # 未达上限：原条目按原 ts 放回，保持原位置语义
            self._entries[entry.session] = entry
            self._persist()
            log.info("queue: %s retry %d/%d",
                     entry.session, entry.attempts, self._max_attempts)
            return entry

    def reinsert(self, entry: QueueEntry) -> None:
        """把已出队的条目原样放回队列（不碰 attempts/ts）。

        用于暂停模式：行动条目永不许丢，暂停时 pop 出的行动必须
        原样放回（保持原位置）。
        """
        with self._lock:
            self._entries[entry.session] = entry
            self._persist()
            log.info("queue: %s reinserted (kind=%s)", entry.session, entry.kind)

    def requeue_action(self, session: str, blocks_xml: str) -> Optional[QueueEntry]:
        """行动失败后重新入队（向后兼容接口）。

        旅程管理器应优先使用 requeue_entry（pop 出队后条目已删，
        按 session 查找会落空，重试计数失效）。本接口保留给外部调用方。

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
