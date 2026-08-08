# -*- coding: utf-8 -*-
"""scanner.py — 发现通道：sweep（双击 Tab 截图扫描）+ heartbeat（心跳兜底）。

从 agent_core_v2 提取，v3 增强：监控列表和间隔均从 RuntimeConfig 读取，
通过 on_config_change 回调响应热更新。
"""

import json
import logging
import os
import time
from dataclasses import dataclass

from ..device import layout
from .....shared.name_match import _name_match

log = logging.getLogger("perception.scanner")

PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", ".."))
HOME_SCAN_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                              "home_scan.json")


@dataclass
class ScanEvent:
    """sweep/heartbeat 的产物（与 AgentEvent 字段同构）。"""
    session: str
    mention: bool = False
    source: str = "sweep"           # sweep / heartbeat
    entry: object = None            # NotificationQueue.QueueEntry


class Scanner:
    """发现通道。监控列表和间隔均从 RuntimeConfig 热读取。

    构造：Scanner(tools, frame_bus, runtime, queue)
    - runtime: RuntimeManager 实例（动态读取 config.monitored / config.sweep_interval）
    """

    def __init__(self, tools, frame_bus, runtime, queue):
        self.tools = tools
        self.frame_bus = frame_bus
        self._runtime = runtime
        self.queue = queue
        # 注册回调：runtime 变更时自动刷新
        runtime.on_change(lambda _: None)  # sessions 每次实时从 config 读

    @property
    def sessions(self):
        """始终从 runtime 读取最新监控列表。"""
        return self._runtime.config.monitored

    # ------------------------------------------------------------------ sweep（主发现通道）
    def sweep(self):
        """截图扫描：open_wechat → 双击底部"微信"Tab → 解析首页未读。

        微信特性：双击"微信"Tab 把会话列表滚到第一个有未读的会话。
        双击只对数字未读生效，红点（免打扰）会话可能留在首屏以下：
        补一轮下滚扫描（最多 2 屏）把所有红点会话合入通知队列，然后滚回顶部。
        """
        r = self.tools.open_wechat()
        if not r.success or r.page != "wechat_home":
            # 卡页自救：最多 back 3 次回首页（聊天页输入栏聚焦时第一次 back
            # 只取消聚焦页面不退，需再按；小程序面板同理）。不自救会每轮 skip
            log.warning("sweep: open_wechat -> success=%s page=%s，尝试 back 自救",
                        r.success, r.page)
            for _ in range(3):
                if r.success and r.page == "wechat_home":
                    break
                try:
                    self.tools.dev.back()
                    self.tools.dev.wait_random(600, 1000)
                    r = self.tools.open_wechat()
                except Exception:  # noqa: BLE001
                    log.exception("sweep 自救 back 失败")
                    break
            if not r.success or r.page != "wechat_home":
                log.warning("sweep: 自救失败 success=%s page=%s (skip)",
                            r.success, r.page)
                return []
        self.tools.dev.double_tap_rect(layout.TAB_WECHAT)
        self.tools.dev.wait_random(800, 1500)
        state = self.frame_bus.capture()
        events = self._queue_unread(state, source="sweep")
        events += self._scan_below_fold()
        # 同一会话跨屏重复入队：去重（队列内部本就有粘滞合并，这里只整 events）
        uniq = {}
        for e in events:
            uniq.setdefault(e.session, e)
        return list(uniq.values())

    def _scan_below_fold(self, max_screens=2):
        """红点补扫：下滚最多 max_screens 屏收集未读/红点会话，再滚回顶部
        （2.14 首页滑动纪律：允许上下滑检索红点）。"""
        events = []
        scrolled = 0
        prev_labels = None
        for _ in range(max_screens):
            self.tools.dev.swipe_zone(layout.HOME_LIST_ZONE, direction="up",
                                      length_ratio=(0.55, 0.75))
            self.tools.dev.wait_random(600, 1000)
            state = self.frame_bus.capture()
            if state.get("page", {}).get("type") != "wechat_home":
                log.warning("sweep 补扫: 离开首页(%s)，中止",
                            state.get("page", {}).get("type"))
                break
            labels = [e.get("label") for e in state.get("elements", [])
                      if e.get("type") == "session_item"]
            if not labels or labels == prev_labels:
                break                           # 滚到底 / 无变化
            prev_labels = labels
            scrolled += 1
            events += self._queue_unread(state, source="sweep")
        for _ in range(scrolled):               # 滚回顶部，不干扰下一轮双击
            self.tools.dev.swipe_zone(layout.HOME_LIST_ZONE, direction="down",
                                      length_ratio=(0.55, 0.75))
            self.tools.dev.wait_random(400, 800)
        return events

    # ------------------------------------------------------------------ heartbeat（兜底）
    def heartbeat(self):
        """心跳兜底：只拍首页未读（不双击）。"""
        r = self.tools.open_wechat()
        if not r.success or r.page != "wechat_home":
            log.warning("heartbeat: open_wechat -> success=%s page=%s (skip)",
                        r.success, r.page)
            return []
        state = self.frame_bus.capture()
        return self._queue_unread(state, source="heartbeat")

    # ------------------------------------------------------------------ 内部
    def _queue_unread(self, state, source):
        """首页 state 里有未读/mention 的会话入队。
        open_all_sessions 时不再过滤监控列表（系统会话由队列黑名单拦截）。"""
        self._write_home_scan(state)        # 实况快照（网关首页红点卡片）
        events = []
        sessions = self.sessions  # 实时读取
        open_all = getattr(self._runtime.config, "open_all_sessions", False)
        for e in state.get("elements", []):
            if e.get("type") != "session_item" or e.get("partial"):
                continue                # 残缺条目（屏幕边缘截断）不纳入发现
            unread = e.get("unread_count", 0)
            mention = bool(e.get("mention_me"))
            if unread == 0 and not mention:
                continue
            if open_all:
                name = e.get("label") or ""
                if not name:
                    continue
                entry = self.queue.push(name, preview=e.get("last_message", ""),
                                        mention=mention, source=source)
                if entry is None:
                    continue            # 系统会话黑名单拦截
                if unread > 0:
                    entry.count = max(entry.count, unread)
                log.info("%s: %s unread=%s mention=%s -> queued (open_all)",
                         source, name, unread, mention)
                events.append(ScanEvent(session=name, mention=mention,
                                        source=source, entry=entry))
                continue
            for name in sessions:
                if not _name_match(e.get("label"), name):
                    continue
                entry = self.queue.push(name, preview=e.get("last_message", ""),
                                        mention=mention, source=source)
                if entry is None:
                    break               # 黑名单拦截
                if unread > 0:
                    entry.count = max(entry.count, unread)
                log.info("%s: %s unread=%s mention=%s -> queued",
                         source, name, unread, mention)
                events.append(ScanEvent(session=name, mention=mention,
                                        source=source, entry=entry))
                break
        if not events:
            log.info("%s: no unread in monitored sessions", source)
        return events

    def _write_home_scan(self, state):
        """首页全部会话条目 → workspace/runtime/home_scan.json（原子写，
        网关实况页"首页红点"卡片读取）。失败只记日志，不影响扫描主流程。"""
        try:
            sessions = []
            for e in state.get("elements", []):
                if e.get("type") != "session_item":
                    continue
                sessions.append({
                    "label": e.get("label") or "",
                    "unread_count": e.get("unread_count", 0),
                    "unread_kind": e.get("unread_kind"),
                    "mention_me": bool(e.get("mention_me")),
                    "muted": bool(e.get("muted")),
                    "partial": bool(e.get("partial")),
                })
            payload = {"ts": time.time(), "sessions": sessions}
            os.makedirs(os.path.dirname(HOME_SCAN_PATH), exist_ok=True)
            tmp = HOME_SCAN_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, HOME_SCAN_PATH)
        except Exception:  # noqa: BLE001
            log.exception("home_scan 快照写入失败")
