# -*- coding: utf-8 -*-
"""notify_watcher.py — 微信系统通知监听 + 通知队列（v2 辅助事件源 + 队列容器）。

2026-08-04 起通知降级为**辅助信号源**（mention 预判/提前插队）；新消息发现
主通道是 agent_core_v2.sweep_by_double_tap（双击"微信"Tab 截图扫描）。

依据 docs/V2_RESEARCH_NOTES.md 方向4 §1.3 与 docs/V2_AGENT_ARCH.md §2.1/§3 实现。

- dump_wechat_notifications(): 解析 `dumpsys notification --noredact` 中
  com.tencent.mm 的 NotificationRecord（key/android.title/android.text/postTime）。
- NotifyWatcher: 线程，3~6s 随机间隔轮询，key+preview 快照去重（微信会原地更新
  同一条通知，"见到就触发"会重复），新出现或内容变化才产出 NotifyEvent。
  快照落盘 data/notify_seen.json，重启不重放存量通知。
- NotificationQueue: dict[session] -> QueueEntry，规则见 V2_AGENT_ARCH §3：
  同会话只出现一次 / 未处理则覆盖更新 / count 从"N条新消息"提取否则 +1 /
  mention 粘滞 / pop_on_process / mention 优先、其余按 first_ts FIFO。

dumpsys 输出实测格式（2026-08-04，Android 15）：
    NotificationRecord(0x0a960e1d: pkg=xxx ... key=0|pkg|id|tag|uid: Notification(...))
      ...
      key=0|pkg|id|tag|uid
      notification=
            when=1785780662075/1785780662075
            extras={
                android.title=String (更新成功)
                android.text=String (更新 pkuwalless 成功)
            }
"""

import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("notify_watcher")

WECHAT_PKG = "com.tencent.mm"
MENTION_MARK = "@陈曦"          # 群通知正文含此串 → mention 预判（V2_AGENT_ARCH §2.1）

SEEN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "notify_seen.json")

NEW_MSG_RE = re.compile(r"(\d+)\s*条新消息")

_RECORD_SPLIT_RE = re.compile(r"^    NotificationRecord\(", re.M)
_PKG_RE = re.compile(r"pkg=(\S+)")
_KEY_LINE_RE = re.compile(r"^\s*key=(\S+)\s*$", re.M)
_WHEN_RE = re.compile(r"^\s*when=(\d+)", re.M)
_TITLE_RE = re.compile(r"^\s*android\.title=\w+ \((.*)\)\s*$", re.M)
_TEXT_RE = re.compile(r"^\s*android\.text=\w+ \((.*)\)\s*$", re.M)


# ------------------------------------------------------------------ 事件
@dataclass
class NotifyEvent:
    session: str        # android.title（群名/昵称）
    preview: str        # android.text（可能是 "N条新消息" 聚合，仅作参考）
    mention: bool       # preview 含 @陈曦
    post_time: int      # 通知 postTime（epoch ms）
    key: str            # NotificationRecord key，去重/清除检测用
    source: str = "notify"

    def __repr__(self):
        return (f"NotifyEvent({self.session!r} mention={self.mention} "
                f"preview={self.preview!r})")


# ------------------------------------------------------------------ 解析
def _extra(block, regex):
    m = regex.search(block)
    if not m:
        return ""
    v = m.group(1)
    return "" if v == "null" else v


def parse_notifications(dump_text, pkg=WECHAT_PKG):
    """从 dumpsys notification --noredact 全文中解析指定包的通知事件列表。"""
    events = []
    # 按 NotificationRecord 切块（块头行缩进 4 空格）
    chunks = _RECORD_SPLIT_RE.split(dump_text)
    for block in chunks[1:]:
        head = block.splitlines()[0] if block.splitlines() else ""
        m = _PKG_RE.search(head)
        if not m or m.group(1) != pkg:
            continue
        km = _KEY_LINE_RE.search(block)
        if not km:
            continue
        key = km.group(1)
        wm = _WHEN_RE.search(block)
        post_time = int(wm.group(1)) if wm else 0
        title = _extra(block, _TITLE_RE)
        text = _extra(block, _TEXT_RE)
        if not title and not text:
            continue
        events.append(NotifyEvent(
            session=title, preview=text,
            mention=MENTION_MARK in text,
            post_time=post_time, key=key))
    return events


def dump_wechat_notifications(dev, pkg=WECHAT_PKG):
    """真机抓取 + 解析。dev 为 DeviceCtl 实例（用其 _shell 统一封装）。"""
    out = dev._shell("dumpsys notification --noredact", timeout=25)
    return parse_notifications(out.decode(errors="replace"), pkg=pkg)


# ------------------------------------------------------------------ 监听线程
class NotifyWatcher(threading.Thread):
    """3~6s 随机间隔轮询微信通知；新 key 或 preview 变化 → 事件入队。

    dump_fn 可注入（默认真机 dump），便于单测。事件去向为 queue.put(NotifyEvent)，
    queue 可以是 NotificationQueue（推荐）或任何有 put() 的对象。
    """

    def __init__(self, dump_fn, queue, seen_path=SEEN_PATH,
                 interval=(3.0, 6.0), replay_existing=False):
        super().__init__(daemon=True, name="notify-watcher")
        self._dump_fn = dump_fn
        self._queue = queue
        self._seen_path = seen_path
        self._interval = interval
        self._stop_ev = threading.Event()
        self._seen = {}                     # key -> preview（上一次快照）
        if not replay_existing:
            self._load_seen()

    # -- 快照落盘：重启不把存量通知重放（V2_AGENT_ARCH §2.1）
    def _load_seen(self):
        try:
            with open(self._seen_path, encoding="utf-8") as f:
                self._seen = json.load(f)
            log.info("notify seen snapshot loaded: %d keys", len(self._seen))
        except (OSError, ValueError):
            self._seen = {}

    def _save_seen(self):
        try:
            os.makedirs(os.path.dirname(self._seen_path), exist_ok=True)
            tmp = self._seen_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._seen, f, ensure_ascii=False)
            os.replace(tmp, self._seen_path)
        except OSError:
            log.exception("save notify seen snapshot failed")

    def poll_once(self):
        """抓一轮：新出现或 text 变化的 key 产出事件；消失的 key 不产出（=已读）。"""
        events = self._dump_fn()
        cur = {e.key: e for e in events}
        n_new = 0
        for k, e in cur.items():
            old_preview = self._seen.get(k)
            if old_preview is None or old_preview != e.preview:
                self._queue.put(e)
                n_new += 1
        if n_new:
            log.info("notify poll: %d new/updated event(s): %s",
                     n_new, [repr(e) for e in cur.values()
                             if self._seen.get(e.key) != e.preview])
        self._seen = {k: e.preview for k, e in cur.items()}
        self._save_seen()

    def run(self):
        while not self._stop_ev.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("notify poll failed")     # 单轮失败不致命
            self._stop_ev.wait(random.uniform(*self._interval))

    def stop(self):
        self._stop_ev.set()


# ------------------------------------------------------------------ 通知队列
# 系统/信息流"会话"黑名单：不是真实聊天，任何通道都不得入队
# （公众号/服务通知是 feed 不是对话；"微信"是桌面登录等系统通知的标题噪声）
BLOCKED_SESSIONS = frozenset({
    "微信", "微信团队", "微信支付", "服务通知", "公众号", "腾讯新闻",
    "微信运动", "QQ邮箱提醒", "腾讯充值", "微信游戏", "企业微信",
})


@dataclass
class QueueEntry:
    session: str                # 会话名（匹配监控列表用 wechat_tools._name_match）
    count: int = 0              # 通知条数（"N条新消息"提取，提取不到则逐条 +1）
    latest_preview: str = ""    # 最近一次通知正文
    mention: bool = False       # 任一条含 @我 即 True，粘滞到出队
    first_ts: float = 0.0
    last_ts: float = 0.0
    sources: set = field(default_factory=set)   # {'notify'} / {'sweep'} / {'heartbeat'} / 组合


class NotificationQueue:
    """每会话一条记录的通知队列（V2_AGENT_ARCH §3 逐条落地）。

    worker 开始处理某会话即 pop_on_process（不是处理完才 pop）——处理期间新到的
    通知重新入队，由 follow-up 检查兜底。
    priority_names：高优先级会话名单（如 YOUSAOBI），出队时排在 mention 之前。
    """

    def __init__(self, priority=()):
        self._entries = {}      # session -> QueueEntry
        self._lock = threading.Lock()
        self.priority_names = tuple(priority)

    def _is_priority(self, session):
        for p in self.priority_names:
            if session == p or p in session or session in p:
                return True
        return False

    def _sort_key(self, e):
        # 优先级 > mention > FIFO
        return (0 if self._is_priority(e.session) else 1,
                0 if e.mention else 1,
                e.first_ts)

    # -- 入队（覆盖更新语义）
    def push(self, session, preview="", mention=False, source="notify", ts=None):
        if session in BLOCKED_SESSIONS:
            log.debug("queue push blocked (system session): %s", session)
            return None
        ts = time.time() if ts is None else ts
        with self._lock:
            e = self._entries.get(session)
            if e is None:
                e = QueueEntry(session=session, first_ts=ts)
                self._entries[session] = e
            e.last_ts = ts
            e.latest_preview = preview or e.latest_preview
            e.sources.add(source)
            m = NEW_MSG_RE.search(preview or "")
            if m:
                e.count = max(e.count, int(m.group(1)))    # 取通知里的最新计数
            else:
                e.count += 1
            e.mention = e.mention or mention               # 粘滞
            log.info("queue push: %s count=%d mention=%s sources=%s",
                     session, e.count, e.mention, sorted(e.sources))
            return e

    def put(self, ev: NotifyEvent):
        """NotifyWatcher 的事件入口（兼容 queue.put 协议）。"""
        return self.push(ev.session, preview=ev.preview,
                         mention=ev.mention, source=ev.source)

    # -- 出队：优先级 > mention 优先，其余按 first_ts FIFO
    def pop_next(self):
        with self._lock:
            if not self._entries:
                return None
            entries = sorted(self._entries.values(), key=self._sort_key)
            e = entries[0]
            del self._entries[e.session]
            return e

    def pop_on_process(self, session):
        """worker 开始处理该会话时调用：无条件移出（存在与否都安全）。"""
        with self._lock:
            return self._entries.pop(session, None)

    # -- 只读视图（prompt / 可视化面板用）
    def snapshot(self):
        with self._lock:
            return sorted((QueueEntry(**{**vars(e), "sources": set(e.sources)})
                           for e in self._entries.values()),
                          key=self._sort_key)

    def describe(self):
        """进 prompt 的一行描述：'怨憎会(3条) | Doo(1条)'，空队列返回 ''。"""
        return " | ".join(f"{e.session}({e.count}条)"
                          + ("@我" if e.mention else "")
                          for e in self.snapshot())

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def __contains__(self, session):
        with self._lock:
            return session in self._entries
