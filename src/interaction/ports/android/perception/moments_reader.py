# -*- coding: utf-8 -*-
"""moments_reader.py — 朋友圈 feed 读取：跨屏拼接 + 全文展开 + 水位断点续读。

与聊天页 reader.py 同构：截图全走 frame_bus，滑动走 dev.swipe_zone(layout 常量)。

两层结构：
  FeedStitcher   纯拼接器（无设备依赖，可离线单测）：逐屏喂
                 (entries, orphan)，按指纹合并/去重/水位判定；
  MomentsReader  薄设备循环：截屏 → parse_moments_entries + parse_top_orphan
                 → 点"全文"展开 → 喂 stitcher → 下滚，直到命中水位 /
                 到底 / max_scrolls。

关键设计（2026-08-12 定）：
- 条目指纹 = normalize(nickname) + "|" + normalize(text)[:30]。
  **不能含时间**（"6小时前"明天变"1天前"）；正文前缀发后不变，指纹稳定；
  展开"全文"只改尾部，前缀不变，指纹不受展开影响。
- 续接匹配 _same_entry：昵称相同 + 正文前缀 12 字一致（或一侧为空）。
  解决"头像在屏底、正文尚未入屏"时 fp 从 'nick|' 变成 'nick|正文…' 的
  指纹漂移重复问题。
- 水位 = 上次读到的最新条目指纹，存 workspace/runtime/moments_watermark.json；
  读到命中即停（以下全是旧的）。首次/水位失效则全量读到 max_scrolls。
- 全文展开会打乱布局（下方内容全部下移），展开后必须重新截屏解析再继续。
- 产出 entry 的 click_x/click_y 是截屏瞬间坐标，**只对当屏有效**；
  动作层（interactor）要点击时必须重新解析活屏，不可用拼接结果里的旧坐标。
"""

import json
import logging
import os
import re
import time

from ..device import layout
from . import moments_parser as mp

log = logging.getLogger("perception.moments_reader")

PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", ".."))
WATERMARK_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                              "moments_watermark.json")

MAX_SCROLLS = 10                # 全量读取兜底屏数
EXPAND_BUDGET_PER_SCREEN = 2    # 每屏最多点几次"全文"
BOTTOM_STABLE_SCREENS = 2       # 连续几屏无新增判到底


# ================================================================ 指纹/归一化
def _norm(s):
    return re.sub(r"\s+", "", s or "")


def entry_fp(nickname, text):
    """条目指纹：昵称 + 正文前 30 字（不含时间，时间会漂移）。"""
    return f"{_norm(nickname)}|{_norm(text)[:30]}"


def _prefix_eq(ta, tb, n=12):
    """正文前缀一致判定：以较短一侧为准（一侧不足 n 字也能匹配）。"""
    if not ta or not tb:
        return True
    k = min(len(ta), len(tb), n)
    return ta[:k] == tb[:k]


def _same_entry(a, b):
    """跨屏同一条目判定：同昵称 + 正文前缀一致（或一侧正文为空）。"""
    if (a.get("nickname") or "") != (b.get("nickname") or ""):
        return False
    return _prefix_eq(_norm(a.get("text")), _norm(b.get("text")))


def _comment_key(c):
    return (c.get("from_user"), c.get("reply_to"),
            _norm(c.get("content"))[:20])


# ================================================================ 纯拼接器
class FeedStitcher:
    """逐屏合并条目。entries 顺序 = 新→旧（feed 自上而下）。"""

    def __init__(self, watermark_fp=None):
        self.entries = []
        self.watermark_fp = watermark_fp
        self.hit_watermark = False
        self._by_fp = {}

    # ---------------- 主入口
    def feed(self, entries, orphan=None):
        """喂一屏。返回是否有内容变化（到底检测用）。"""
        changed = False
        cur = self.entries[-1] if self.entries else None
        if orphan and cur is not None:
            changed |= self._merge_orphan(cur, orphan)
        for i, e in enumerate(entries):
            e["fp"] = entry_fp(e.get("nickname"), e.get("text"))
            if self.watermark_fp and e["fp"] == self.watermark_fp:
                self.hit_watermark = True
                break                       # 水位以下全是旧的
            cur = self.entries[-1] if self.entries else None
            if i == 0 and cur is not None and _same_entry(cur, e):
                changed |= self._merge_entry(cur, e)
                self._rekey(cur)
                continue
            old = self._by_fp.get(e["fp"])
            if old is not None:
                changed |= self._merge_entry(old, e)
                self._rekey(old)
            else:
                self.entries.append(e)
                self._by_fp[e["fp"]] = e
                changed = True
        return changed

    def _rekey(self, entry):
        """正文变长后指纹可能变化（短文本 <30 字），合并后重挂索引。"""
        new_fp = entry_fp(entry.get("nickname"), entry.get("text"))
        if new_fp != entry.get("fp"):
            self._by_fp.pop(entry.get("fp"), None)
            entry["fp"] = new_fp
            self._by_fp[new_fp] = entry

    # ---------------- 合并
    def _merge_orphan(self, cur, orphan):
        """孤儿段（第一头像之上）拼到当前条目尾部。"""
        changed = False
        # 正文行：跳过与尾部重复的行（滚动重叠区）
        tail_norms = {_norm(it["text"]) for it in cur["text_items"][-6:]}
        for it in orphan["text_items"]:
            if _norm(it["text"]) and _norm(it["text"]) not in tail_norms:
                cur["text_items"].append(it)
                changed = True
        if changed:
            cur["text"] = "\n".join(it["text"] for it in cur["text_items"])
        # 时间/两个点 = 正文收尾证据
        if orphan["time"] and not cur.get("time"):
            cur["time"] = orphan["time"]
            cur["time_cy"] = orphan["time_cy"]
            changed = True
        if orphan["dots"] and not cur.get("dots"):
            cur["dots"] = orphan["dots"]
            changed = True
        # 点赞/评论
        changed |= self._merge_likes(cur, orphan["likes"])
        changed |= self._merge_comments(cur, orphan["comments"])
        self._refresh_complete(cur)
        return changed

    def _merge_entry(self, old, new):
        """同一条目再次入屏（滚动重叠）：补齐新信息。"""
        changed = False
        # 正文：新的更长且前缀一致（或旧的还是空的）→ 采用新的
        if len(new.get("text") or "") > len(old.get("text") or "") \
                and _prefix_eq(_norm(old["text"]), _norm(new["text"])):
            old["text"] = new["text"]
            old["text_items"] = new["text_items"]
            changed = True
        for k in ("time", "time_cy", "dots", "fulltext_btn"):
            if new.get(k) and not old.get(k):
                old[k] = new[k]
                changed = True
        changed |= self._merge_likes(old, new.get("likes") or [])
        changed |= self._merge_comments(old, new.get("comments") or [])
        old["partial_bottom"] = new.get("partial_bottom", old["partial_bottom"])
        old["partial_top"] = new.get("partial_top", old["partial_top"])
        self._refresh_complete(old)
        return changed

    @staticmethod
    def _merge_likes(entry, names):
        changed = False
        for n in names:
            if n and n not in entry["likes"]:
                entry["likes"].append(n)
                changed = True
        return changed

    @staticmethod
    def _merge_comments(entry, comments):
        changed = False
        have = {_comment_key(c) for c in entry["comments"]}
        for c in comments:
            if _comment_key(c) not in have:
                entry["comments"].append(c)
                have.add(_comment_key(c))
                changed = True
        return changed

    @staticmethod
    def _refresh_complete(entry):
        entry["text_complete"] = bool(entry.get("time")) \
            and bool(entry.get("dots"))
        entry["complete"] = entry["text_complete"] \
            and not entry.get("partial_bottom")

    # ---------------- 水位
    def latest_fp(self):
        return self.entries[0]["fp"] if self.entries else None


# ================================================================ 设备循环
class MomentsReader:
    """MomentsReader(tools, frame_bus[, watermark_path])：feed 滚动读取。"""

    def __init__(self, tools, frame_bus, watermark_path=WATERMARK_PATH):
        self.tools = tools
        self.frame_bus = frame_bus
        self.dev = tools.dev
        self.watermark_path = watermark_path

    # ---------------- 水位存取
    def _load_watermark(self):
        try:
            with open(self.watermark_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def _save_watermark(self, fp, read_count):
        if not fp:
            return
        data = {"last_fp": fp, "last_read_ts": int(time.time()),
                "read_count": read_count}
        try:
            os.makedirs(os.path.dirname(self.watermark_path), exist_ok=True)
            tmp = self.watermark_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.watermark_path)
        except Exception:  # noqa: BLE001
            log.exception("save moments watermark failed")

    # ---------------- 截屏解析
    def _parse_current_screen(self):
        """截屏 → (entries, orphan)。唯一截图入口 frame_bus。"""
        import cv2
        import numpy as np
        img = self.frame_bus.capture_raw()
        if isinstance(img, (bytes, bytearray)):
            img = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
        from .ocr_engine import run_ocr
        items = run_ocr(img)
        entries, _extra = mp.parse_moments_entries(img, items)
        first_y = (entries[0]["avatar"]["y"] - mp.NICK_DY - 10) \
            if entries else mp.SCREEN_H
        orphan = mp.parse_top_orphan(img, items, first_y)
        return entries, orphan

    # ---------------- 全文展开
    def _expand_visible_fulltext(self, budget=EXPAND_BUDGET_PER_SCREEN):
        """点当前屏可见的"全文"按钮（布局会突变，点后重新截屏解析）。
        返回最新的 (entries, orphan)。"""
        entries, orphan = None, None
        for _ in range(budget):
            entries, orphan = self._parse_current_screen()
            target = next(
                (e for e in entries
                 if e.get("fulltext_btn")
                 and e["fulltext_btn"]["text"] in ("全文", "展开")),
                None)
            if target is None:
                break
            fb = target["fulltext_btn"]
            log.info("展开全文: entry=%s btn=(%d,%d)",
                     target.get("nickname"), fb["cx"], fb["cy"])
            self.dev.tap(fb["cx"], fb["cy"])
            self.dev.wait_random(900, 1400)
        if entries is None:
            entries, orphan = self._parse_current_screen()
        return entries, orphan

    # ---------------- 主流程
    def read_feed(self, max_scrolls=MAX_SCROLLS, expand_fulltext=True):
        """从 feed 当前位置向下读，直到命中水位/到底/屏数上限。

        返回 {'entries': [...新→旧], 'stopped_by': str, 'screens': int}。
        调用方负责先进到朋友圈页（action.moments_poster._enter_moments）。"""
        wm = self._load_watermark()
        stitcher = FeedStitcher(watermark_fp=wm.get("last_fp"))
        stopped_by = "max_scrolls"
        no_change = 0
        screens = 0

        for i in range(max_scrolls + 1):
            if expand_fulltext:
                entries, orphan = self._expand_visible_fulltext()
            else:
                entries, orphan = self._parse_current_screen()
            screens += 1
            changed = stitcher.feed(entries, orphan)

            if stitcher.hit_watermark:
                stopped_by = "watermark"
                break
            if not changed:
                no_change += 1
                if no_change >= BOTTOM_STABLE_SCREENS:
                    stopped_by = "bottom"
                    break
            else:
                no_change = 0

            if i < max_scrolls:
                self._scroll_down()

        new_fp = stitcher.latest_fp()
        self._save_watermark(new_fp, len(stitcher.entries))
        log.info("feed read: %d 屏 %d 条新条目, stopped_by=%s",
                 screens, len(stitcher.entries), stopped_by)
        return {"entries": stitcher.entries, "stopped_by": stopped_by,
                "screens": screens}

    def _scroll_down(self):
        """下滚一屏（看更旧的条目）：保守步长保重叠区，慢速减 fling。"""
        self.dev.swipe_zone(layout.MOMENTS_SCROLL_ZONE, direction="up",
                            length_ratio=(0.4, 0.55),
                            duration_ms=(500, 800))
        self.dev.wait_random(1200, 1800)    # 等懒加载评论渲染
