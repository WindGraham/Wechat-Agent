# -*- coding: utf-8 -*-
"""session_reader.py — 会话读取器：进入会话 → 增量读取 → 日志同步 → 回传。

本模块是交互层 reader 的核心实现。对决策层只暴露两个读取接口：
- get_context(session, n=200) → list[Message]
- get_new_since(session, last_seq) → list[Message]

内部流程：
1. 使用端口感知层（Android OCR/截图）读取当前屏幕消息
2. 通过消息日志做增量合并（gap 自愈：积压上翻 / 强制追加）
3. 多媒体打标（非文字泡只写占位 + 裁图路径）
4. 每次同步后 increment_sync_version，发 LogUpdated 通知决策层
"""

import logging
import os
import time
from typing import Optional

from ...shared.types import Message, LogUpdated

log = logging.getLogger("interaction.reader")


class SessionReader:
    """会话读取器：封装端口感知 + 消息日志，提供统一读取接口。

    port_reader: 端口感知层的 Reader 实例（如 ports/android/perception/reader.py 的 Reader）
    msglog_conn: 消息日志 SQLite 连接
    media_dir: 多媒体裁图归档根目录
    owner_nick: 主人@我时的昵称（CONTRACTS §五 runtime.json owner_nick），
        @我 判定用；空串则不按昵称匹配（仅认 @所有人）
    """

    def __init__(self, port_reader, msglog_conn, media_dir: str = "",
                 owner_nick: str = ""):
        self._pr = port_reader          # 端口感知 Reader
        self._conn = msglog_conn        # 消息日志连接
        self._media_dir = media_dir or ""
        self._owner_nick = owner_nick or ""
        self._gap_fail: dict = {}       # session -> 连续 gap 次数
        self._on_log_updated = None     # 回调：决策层的 LogUpdated 处理器

    # ------------------------------------------------------------------ 决策层接口
    def last_is_group(self, session: str):
        """该会话最近一次实测的群/私属性；未知返回 None（调用方自行兜底）。"""
        try:
            row = self._conn.execute(
                "SELECT is_group FROM sessions WHERE name=?",
                (session,)).fetchone()
            return bool(row["is_group"]) if row else None
        except Exception:  # noqa: BLE001
            return None

    def get_context(self, session: str, n: int = 200) -> list:
        """按量拉取历史：返回尾部 n 条 Message，seq 升序（最新在尾）。"""
        from ..msglog import (get_or_create_session, get_session_kind,
                              get_context as _get_ctx)
        sid = get_or_create_session(self._conn, session, False)
        is_group = get_session_kind(self._conn, session)
        rows = _get_ctx(self._conn, sid, n=n)
        return [self._row_to_message(session, r, is_group) for r in rows]

    def get_new_since(self, session: str, last_seq: int) -> list:
        """水位差分：返回 seq > last_seq 的新消息，seq 升序。"""
        from ..msglog import (get_or_create_session, get_session_kind,
                              get_new_since as _get_new)
        sid = get_or_create_session(self._conn, session, False)
        is_group = get_session_kind(self._conn, session)
        rows = _get_new(self._conn, sid, last_seq)
        return [self._row_to_message(session, r, is_group) for r in rows]

    # ------------------------------------------------------------------ 同步流程（进会话→读屏→写库→回传）
    def sync_session(self, session: str, is_group: bool) -> Optional[LogUpdated]:
        """进入一个会话，完成一次完整的日志同步。

        流程：读当前屏 → 增量写库（gap 自愈）→ 版本号+1 → 返回 LogUpdated。
        返回 None 表示同步失败。
        """
        from ..msglog import (get_or_create_session, set_session_kind,
                              append_incremental,
                              increment_sync_version, get_sync_version)

        sid = get_or_create_session(self._conn, session, is_group)
        # 旅程实测到真实 is_group，显式写回（get_or_create_session 不覆写）
        set_session_kind(self._conn, session, is_group)

        # 1. 读取当前屏
        try:
            entries, state = self._pr.read_current()
        except Exception:
            log.exception("[%s] read_current failed", session)
            return None

        # 2. 增量写库（gap 自愈）
        new_entries = self._append_gap_aware(sid, session, entries)

        # 3. 多媒体打标（非文字泡 → 占位符 + 裁图路径）
        if new_entries:
            self._tag_media(session, sid, new_entries)

        # 4. 版本号+1
        new_version = increment_sync_version(self._conn, sid)

        # 5. 检查是否有 @我
        mention_hint = any(
            self._is_at_me(e) for e in new_entries
        ) if new_entries else False

        updated = LogUpdated(
            session=session,
            version=new_version,
            mention_hint=mention_hint,
        )
        log.info("[%s] sync complete: version=%d new=%d mention=%s",
                 session, new_version, len(new_entries), mention_hint)
        return updated

    # ------------------------------------------------------------------ 内部
    GAP_FORCE_CAP = 3  # 连续 gap 达此次数后强制追加

    def _append_gap_aware(self, sid: int, session: str, entries: list) -> list:
        """增量写库；gap → 积压上翻收集；连续失败达上限 → 强制追加。"""
        from ..msglog import append_incremental, merge_stack

        r = append_incremental(self._conn, sid, entries,
                               captured_ts=time.time())
        if not r.get("gap"):
            self._gap_fail[session] = 0
            return r.get("new", [])

        # gap：屏幕消息与日志不连续
        self._gap_fail[session] = self._gap_fail.get(session, 0) + 1
        if self._gap_fail[session] >= self.GAP_FORCE_CAP:
            log.warning("[%s] gap 连续 %d 次，强制追加",
                        session, self._gap_fail[session])
            fr = append_incremental(self._conn, sid, entries,
                                    captured_ts=time.time(), gap_ok=True)
            self._gap_fail[session] = 0
            return fr.get("new", [])

        # 积压上翻
        log.info("[%s] gap detected, reading backlog (round %d/%d)",
                 session, self._gap_fail[session], self.GAP_FORCE_CAP)
        try:
            backlog, _ = self._pr.read_backlog(session)
            mr = merge_stack(self._conn, sid, backlog,
                             captured_ts=time.time())
            # 合并后再试增量
            r2 = append_incremental(self._conn, sid, entries,
                                    captured_ts=time.time())
            self._gap_fail[session] = 0 if r2.get("new") else self._gap_fail[session]
            return r2.get("new", [])
        except Exception:
            log.exception("[%s] backlog read failed", session)
            return []

    def _tag_media(self, session: str, sid: int, entries: list):
        """多媒体打标：非文字泡只写占位符 + 裁图路径（打标即走，不等识别）。

        图片/表情/语音等 → content 设为 "[图片]"/"[表情]"/"[语音]" 等占位符，
        media_path 记录裁图归档路径。文字化由 Proxy 侧的媒体转换队列完成。
        """
        # 多媒体条目已在 chat_slicer/media_archive 阶段完成裁图和路径标注，
        # 这里只做确认和日志记录。内容占位符由感知层在切段时写入。
        media_types = {"multimedia", "image", "sticker", "voice", "video",
                       "unknown_nontext"}
        tagged = 0
        for e in entries:
            ctype = getattr(e, "content_type", "text") or "text"
            if ctype in media_types:
                tagged += 1
        if tagged:
            log.info("[%s] media tagged: %d non-text items", session, tagged)

    def _is_at_me(self, e) -> bool:
        """@我 判定（CONTRACTS §六）：content 含 "@所有人"，或 mentions 中有
        昵称与 owner_nick 归一化后相等/互为包含（容忍 OCR 粘连）。
        注意这只是调度提示（mention_hint）， Policy 以日志 mentions 为准。"""
        from ..msglog import normalize
        content = getattr(e, "content", "") or ""
        if "@所有人" in content:
            return True
        owner = normalize(self._owner_nick)
        if not owner:
            return False
        for m in getattr(e, "mentions", None) or []:
            nm = normalize(m)
            if nm and (nm == owner or nm in owner or owner in nm):
                return True
        return False

    @staticmethod
    def _row_to_message(session: str, row: dict, is_group: bool = False) -> Message:
        """数据库行 → Message 契约类型。"""
        return Message(
            session=session,
            is_group=is_group,
            sender=row.get("sender", ""),
            is_mine=bool(row.get("is_mine", False)),
            content=row.get("content", ""),
            content_type=row.get("content_type", "text"),
            mentions=(row.get("mentions") or "").split(",") if row.get("mentions") else [],
            media_path=row.get("media_path") or None,
            ts=row.get("ts_captured", 0.0),
            seq=row.get("seq", 0),
            msg_uid="",
        )
