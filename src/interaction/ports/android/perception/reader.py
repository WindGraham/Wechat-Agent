# -*- coding: utf-8 -*-
"""reader.py — 聊天页读取：当前屏增量 / 稳定截图（懒加载容忍）/ 积压上翻收集。
从 agent_core_v2 提取，行为不变；截图全走 frame_bus.capture()，msg_log 判终止。"""

import logging
import os
import re
from dataclasses import dataclass, field

from ..device import layout
from ....msglog import message_log as msg_log

log = logging.getLogger("perception.reader")

PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", ".."))
DB_PATH = os.path.join(PROJECT_ROOT, "workspace", "chatlogs", "chatlog.db")


@dataclass
class SnapEntry:
    """截图解析条目 → msg_log 栈条目适配（与 frame_align.Entry 兼容）。"""
    kind: str = "msg"               # msg / divider
    sender: str = ""
    content: str = ""
    content_type: str = "text"
    is_mine: bool = False
    mentions: list = field(default_factory=list)
    ocr_conf: float = None
    complete: int = 1
    partial_top: bool = False
    partial_bottom: bool = False
    time_hint: str = None
    media_id: str = ""                # 多媒体归档 id（media_archive 标注）
    media_path: str = ""              # 多媒体裁图归档路径（写库进 messages.media_path）


class Reader:
    """聊天页消息读取。Reader(tools, frame_bus[, db_path][, conn])：
    tools=WeChatTools，frame_bus=FrameBus（唯一截图入口），conn 可注入。"""

    def __init__(self, tools, frame_bus, db_path=DB_PATH, conn=None):
        self.tools = tools
        self.frame_bus = frame_bus
        self.dev = tools.dev
        self._conn = conn if conn is not None else msg_log.connect(db_path)
        # 花名册双因子匹配器缓存：group_name -> RosterMatcher|None（懒加载，
        # 同一群多次同步/翻页复用，避免每帧重载 500 人头像模板）
        self._roster_matchers = {}

    # ---------------------------------------------------------- 当前屏
    # 过渡帧重试上限：搜索/列表进聊天页有 1~2s 转场动画，capture 可能截到
    # 搜索页/列表页帧（2026-08-10 交流一下？sync new=0 永久失明事故，
    # 见 docs/BUGREPORT_TIMING_RACE_20260810.md §3.1）
    READ_PAGE_RETRIES = 2

    def read_current(self, session=None):
        """读取当前屏（不滑动）。返回 (entries 早→晚, state)。

        聊天页优先走 chat_slicer（头像顶切段：昵称/multimedia/partial 标记），
        失败降级旧的 elements 转换。session 为会话名（群聊用于花名册双因子识别）。"""
        state = self.frame_bus.capture()
        # 过渡帧兜底：本方法只在旅程已进入会话后调用，页面理应是聊天页；
        # 截到非聊天页帧 = 转场动画未结束，等它稳定后重截，最多
        # READ_PAGE_RETRIES 次（仍非聊天页则按原样返回，不阻塞流程）。
        for retry in range(self.READ_PAGE_RETRIES):
            if state.get("page", {}).get("type") == "wechat_chat":
                break
            log.warning("read_current: 截到非聊天页帧(%s)，等待稳定重截 (%d/%d)",
                        state.get("page", {}).get("type"),
                        retry + 1, self.READ_PAGE_RETRIES)
            self.dev.wait_random(600, 1000)
            state = self.frame_bus.capture()
        entries = self._slice_entries(state, session=session)
        if entries is None:
            entries = self._state_to_entries(state)
        return entries, state

    # ---------------------------------------------------------- processor 协议
    def screenshot(self):
        """MentionProcessor 协议：返回 (img BGR ndarray, ocr_items)。"""
        import cv2
        import numpy as np
        img = self.frame_bus.latest_raw()
        if img is None:
            img = self.dev.capture_bytes()
        if isinstance(img, (bytes, bytearray)):
            img = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
        from .ocr_engine import run_ocr
        return img, run_ocr(img)

    def scroll_up(self):
        """MentionProcessor 协议：聊天页向上翻（看更早消息），随机化。"""
        self.dev.swipe_zone(layout.CHAT_SCROLL_ZONE, direction="down",
                            length_ratio=(0.3, 0.45), duration_ms=(300, 600))
        self.dev.wait_random(600, 1000)

    # ---------------------------------------------------------- chat_slicer 集成
    def _slice_entries(self, state, session=None):
        """聊天页用 chat_slicer 重解析当前屏 → SnapEntry 列表（含昵称/归档）。

        群聊且 session 有对应花名册时注入 RosterMatcher，做头像+昵称双因子
        识别（matched_user_name / uncertain_entity）。返回 None 表示不适用
        （非聊天页/无图/失败），调用方降级。"""
        try:
            if state.get("page", {}).get("type") != "wechat_chat":
                # 不许静默：过渡帧/卡页时返回 None 会让 sync 读到空且
                # 无任何痕迹（2026-08-10 交流一下？事故根因之一）
                log.warning("_slice_entries: 非聊天页(%s)，跳过切段解析",
                            state.get("page", {}).get("type"))
                return None
            img = self.frame_bus.latest_raw()
            if img is None:
                return None
            import numpy as np
            import cv2
            if isinstance(img, (bytes, bytearray)):
                img = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
            if img is None or not hasattr(img, "shape"):
                return None
            page = state.get("page", {})
            title = page.get("title") or ""
            is_group = bool(page.get("is_group") or page.get("member_count"))
            from .ocr_engine import run_ocr
            from .chat_slicer import slice_chat
            ocr_items = run_ocr(img)
            rm = (self._roster_matcher_for(session)
                  if (is_group and session) else None)
            sliced = slice_chat(img, ocr_items, is_group=is_group, title=title,
                                roster_matcher=rm)
            msgs = sliced.get("messages", [])
            # 多媒体归档（WP7）：裁图存会话专目录 + media_id 标注。
            # quote 消息正文段可能是图片/视频（引用+媒体），一并归档
            if any(m.get("content_type") == "multimedia"
                   or (m.get("content_type") == "quote"
                       and m.get("media_present"))
                   for m in msgs):
                try:
                    from .media_archive import archive_multimedia
                    msgs = archive_multimedia(img, title, is_group, msgs)
                except Exception:  # noqa: BLE001
                    log.exception("media archive failed (non-fatal)")
            return [self._msg_to_entry(m, title, is_group) for m in msgs]
        except Exception:  # noqa: BLE001
            log.exception("chat_slicer integration failed, fallback")
            return None

    def _roster_matcher_for(self, session):
        """session 名 → 花名册 RosterMatcher（懒加载 + 缓存）。

        群聊双因子识别（头像+昵称）用。OCR 会话名可能与花名册目录名有
        括号人数/混淆字符差异，用 name_match 容错解析到已爬取的花名册目录；
        找不到返回 None（slice_chat 降级为纯昵称 OCR，行为同现状）。
        """
        if not session:
            return None
        if session in self._roster_matchers:
            return self._roster_matchers[session]
        matcher = None
        try:
            from .roster_matcher import RosterMatcher, ROSTERS_DIR
            from .....shared.name_match import _name_match
            key = None
            if os.path.isdir(os.path.join(ROSTERS_DIR, session)):
                key = session
            elif os.path.isdir(ROSTERS_DIR):
                for d in sorted(os.listdir(ROSTERS_DIR)):
                    if d.startswith(".") or not os.path.isdir(
                            os.path.join(ROSTERS_DIR, d)):
                        continue
                    if _name_match(session, d):
                        key = d
                        break
            if key:
                matcher = RosterMatcher(key)
        except Exception:  # noqa: BLE001
            log.exception("roster matcher resolve failed: %s", session)
        self._roster_matchers[session] = matcher
        return matcher

    @staticmethod
    def _msg_to_entry(m, title, is_group):
        """slicer 消息 -> SnapEntry。双因子 (头像+昵称) 校验增强。"""
        ctype = m.get("content_type", "text")
        if ctype in ("time_divider", "system"):
            return SnapEntry(kind="divider", sender="system",
                             content=m.get("content") or "",
                             content_type="time_divider" if ctype == "time_divider" else "system")
        is_mine = m.get("side") == "self"
        nick = m.get("nickname")

        # 尝试双因子判定匹配 Sender
        if is_mine:
            sender = "self"
        elif m.get("matched_user_name"):
            sender = m["matched_user_name"]
        elif nick:
            sender = nick
        else:
            sender = title if not is_group else "unknown(left)"

        content = m.get("content") or ""
        mentions = re.findall(r"@([^\s@，,：:]+)", content) if "@" in content else []
        partial = bool(m.get("partial_top") or m.get("partial_bottom"))
        e = SnapEntry(
            kind="msg", sender=sender, content=content,
            content_type=ctype, is_mine=is_mine,
            mentions=mentions, ocr_conf=m.get("ocr_conf"),
            complete=0 if partial else 1,
            partial_top=bool(m.get("partial_top")),
            partial_bottom=bool(m.get("partial_bottom")))
        if m.get("media_id"):
            e.media_id = m["media_id"]
            e.media_path = m.get("media_path")
        return e

    # ---------------------------------------------------------- 稳定截图
    def snap_settled(self, tries=5, min_entries=1):
        """懒加载/过渡帧容忍：连续两帧一致 或 含 min_entries 个聊天元素才返回。"""
        prev = None
        state = self.frame_bus.capture()
        for _ in range(tries):
            elements = state.get("elements", [])
            cur = [(e.get("type"), e.get("sender"), e.get("content"))
                   for e in elements]
            chat_entries = [e for e in elements
                            if e.get("type") in ("message_bubble", "time_divider")]
            if cur == prev and cur:
                return state
            if len(chat_entries) >= min_entries:
                return state
            prev = cur
            self.dev.wait_random(400, 800)
            state = self.frame_bus.capture()
        return state

    # ---------------------------------------------------------- 积压上翻
    def read_backlog(self, name, max_up_screens=40):
        """未读积压读取：从当前屏向上翻（看更早）收集未读区间，再落底。
        停止条件：命中已记录日志 / 下拉小程序面板 / 连续 2 屏无变化 / 屏数上限。
        （上限 40：活跃群几十条未读 ≈ 20+ 屏，12 屏够不着日志尾，
        2026-08-08 YOUSAOBI 69 条未读实测）
        返回 (merged 早→晚, last_state)。"""
        sid = msg_log.get_or_create_session(self._conn, name, False)
        known = list(msg_log.session_tail(self._conn, sid, n=30))

        state = self.snap_settled(min_entries=1)
        backlog = [self._entries_of(state, session=name)]
        last_keys = [(e.sender, e.content) for e in backlog[0]]

        stuck = 0
        for i in range(max_up_screens):
            self.dev.swipe_zone(layout.CHAT_SCROLL_ZONE, direction="down",
                                length_ratio=(0.3, 0.45), duration_ms=(300, 600))
            self.dev.wait_random(600, 1000)
            state = self.snap_settled(min_entries=1)
            ptype = state.get("page", {}).get("type", "")
            if ptype in ("wechat_miniapp", "wechat_mini_program") or \
                    self._is_miniapp_panel(state):
                log.warning("[%s] 触发下拉小程序面板，中止积压收集", name)
                break
            cur = self._entries_of(state, session=name)
            keys = [(e.sender, e.content) for e in cur]
            if not cur or keys == last_keys:        # 懒加载未完成/滚不动
                stuck += 1
                if stuck >= 2:
                    log.info("[%s] backlog: 连续 %d 屏无变化，停", name, stuck)
                    break
                self.dev.swipe_zone(layout.CHAT_SCROLL_ZONE, direction="down",
                                    length_ratio=(0.45, 0.65),
                                    duration_ms=(350, 700))
                self.dev.wait_random(1500, 2500)
                continue
            stuck = 0
            last_keys = keys
            backlog.insert(0, cur)
            if self._entries_known(cur, known):
                log.info("[%s] backlog screen %d: 命中已记录消息，停", name, i + 1)
                break

        # 返回底部（误触离开聊天页则重新进入会话）
        bottom_screens = []
        last_bottom_keys = None
        for i in range(6):
            r = self.tools.scroll_down()
            if r.page != "wechat_chat":
                log.warning("[%s] scroll_down 离开聊天页(%s)，重新进入会话",
                            name, r.page)
                r = self.tools.enter_session(name)
                if not r.success or r.page != "wechat_chat":
                    raise RuntimeError(f"backlog 后无法回到会话 {name}: {r.error}")
                state = self.frame_bus.capture()
                bottom_screens.append(self._entries_of(state, session=name))
                break
            state = self.snap_settled(min_entries=1)
            cur = self._entries_of(state, session=name)
            keys = [(e.sender, e.content) for e in cur]
            if not cur or keys == last_bottom_keys:
                log.info("[%s] 已到底部（连续两屏相同），停", name)
                break
            last_bottom_keys = keys
            bottom_screens.append(cur)

        all_screens = backlog + bottom_screens
        merged = []
        for es in all_screens:
            for e in es:
                if not self._dup_in(e, merged):
                    merged.append(e)
        log.info("[%s] backlog read: %d 屏向上 + %d 屏向下，共 %d 条",
                 name, len(backlog), len(bottom_screens), len(merged))
        return merged, state

    # ---------------------------------------------------------- 转换/匹配
    def _entries_of(self, state, session=None):
        """chat_slicer 优先，失败降级 elements 转换。session 用于花名册识别。"""
        entries = self._slice_entries(state, session=session)
        if entries is not None:
            return entries
        return self._state_to_entries(state)

    @staticmethod
    def _state_to_entries(state):
        out = []
        for e in state.get("elements", []):
            t = e.get("type")
            if t == "time_divider":
                out.append(SnapEntry(kind="divider", sender="system",
                                     content=e.get("content") or "",
                                     content_type="time_divider"))
            elif t == "message_bubble":
                is_mine = bool(e.get("is_mine"))
                out.append(SnapEntry(
                    kind="msg",
                    sender="self" if is_mine else (e.get("sender") or "unknown"),
                    content=e.get("content") or "",
                    content_type=e.get("content_type") or "text",
                    is_mine=is_mine,
                    mentions=e.get("mentions") or [],
                    ocr_conf=e.get("ocr_conf"),
                    time_hint=e.get("time_hint")))
            elif t == "system_message":
                out.append(SnapEntry(kind="divider", sender="system",
                                     content=e.get("content") or "",
                                     content_type="system"))
        return out

    @staticmethod
    def _entries_known(entries, known):
        """known 为 session_tail 行 dict（含 content_type/media_path）。
        多媒体行内容已被标注改写，文本比不过——两侧皆多媒体且发送人一致
        即视为已知（同 msglog._media_eq 的锚定语义）。"""
        for e in entries:
            if e.kind != "msg":
                continue
            for r in known:
                if isinstance(r, dict):
                    if r["content_type"] in msg_log.MEDIA_TYPES \
                            or r["media_path"]:
                        if (getattr(e, "content_type", "text")
                                in msg_log.MEDIA_TYPES
                                and msg_log.normalize(e.sender)
                                == msg_log.normalize(r["sender"])):
                            return True
                        continue
                    s, c = r["sender"], r["content"]
                else:
                    s, c = r
                if msg_log.fuzzy_eq(e.sender, e.content, s, c):
                    return True
        return False

    @staticmethod
    def _dup_in(entry, entries):
        for x in entries:
            if entry.kind == "divider" or x.kind == "divider":
                if entry.kind == x.kind == "divider" and \
                        msg_log.normalize(entry.content) == msg_log.normalize(x.content):
                    return True
                continue
            if msg_log.fuzzy_eq(entry.sender, entry.content, x.sender, x.content):
                return True
        return False

    @staticmethod
    def _is_miniapp_panel(state):
        for e in state.get("elements", []):
            c = e.get("content") or ""
            if "搜索小程序" in c or "最近使用的小程序" in c:
                return True
        return False
