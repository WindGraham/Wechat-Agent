# -*- coding: utf-8 -*-
"""realtime_scan.py — 实时滚动采集：每屏识别 → 全局去重 → 实时入库。

用户核心诉求「实时处理」：滑一屏立即识别入库，边滑边出结果，不滑完统一处理。

入库策略（务实版）：维护全局 seen 集合跨屏去重，每屏新识别的完整消息
直接 append_incremental(gap_ok=True) 落库——不做「等库尾相接」的严格 seq 回填
（那要求滑过所有新消息到重叠处，且受「有人@我」胶囊污染 sender 影响，容易
一直 gap 卡住）。展示顺序由网关按 ts_hint（发送时间）负责，不受 seq 影响。

跨屏残缺消息拼接（2026-08-13）：看更早方向实测残缺几乎全是 bottom_clipped
（长消息尾部被输入栏截断，身份+可见正文在屏内）。这类消息的完整版大多数在
更早的屏已 complete 入库，少数（采集起点的最新长消息）从未 complete。对策：
  - bottom_clipped（有身份 + 有可见文本）也落库为截断实例（complete=0），
    减少「最新长消息尾部缺失」的遗漏；
  - 去重靠 append_incremental 的 fuzzy_eq 前缀匹配（截断版是完整版前缀时
    ratio>=0.85 → 视为已入库，不重复），跨屏重复由 seen 集合兜底。
"""

import hashlib
import os
import random
import time
from types import SimpleNamespace

import cv2

from src.interaction.ports.android.device.device_ctl import DeviceCtl
from src.interaction.ports.android.perception.ocr_engine import run_ocr
from src.interaction.ports.android.perception.chat_slicer import slice_chat, classify_message
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher
from src.interaction.msglog import message_log
from src.shared.fling_physics import plan_swipe

SCROLL_RANGE = (1350, 1450)   # 目标总滚动 ≈0.75×消息区高(1417px)：大位移快采，
                             # 但 < 一屏 → 必有重叠（设计 §1：0 < dy < M）
SWIPE_RANGE = (600, 900)
X_RANGE = (400, 680)
Y_START_RANGE = (1100, 1300)
DUR_JITTER = (0.9, 1.1)
SLEEP_S = 0.25   # 快滑后等惯性略稳定；find_overlap_dy 实测位移，无需等完全停

# 实时裁图归档：workspace/crops/<群>/<hash>.jpg（网关 /workspace/ 只读图片路由可访问）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CROP_ROOT = os.path.join(PROJECT_ROOT, "workspace", "crops")


def _safe_name(s):
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in s)


def save_crop(img, group, y0, y1, key):
    """裁下消息区域 [y0:y1] 存 jpg，返回相对 workspace 的路径（空串=失败/太矮）。

    裁切本身是 numpy 切片 + imencode，每条约几 ms，不构成实时瓶颈。
    """
    H = img.shape[0]
    y0 = max(0, int(y0))
    y1 = min(H, int(y1))
    if y1 - y0 < 8:
        return ""
    crop = img[y0:y1, :]
    d = os.path.join(CROP_ROOT, _safe_name(group))
    os.makedirs(d, exist_ok=True)
    fn = hashlib.sha1(key.encode()).hexdigest()[:16] + ".jpg"
    ok = cv2.imwrite(os.path.join(d, fn), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return os.path.join("crops", _safe_name(group), fn) if ok else ""


def to_entry(m, group):
    ctype = m.get("content_type")
    is_mine = m.get("side") == "self"
    sender = "我" if is_mine else (m.get("matched_user_name") or m.get("nickname") or "")
    # 洗掉「有人@我」「N条新消息」等悬浮胶囊污染进昵称的尾缀
    import re
    sender = re.sub(r"有人[@＠]?我.*$", "", sender).strip()
    return SimpleNamespace(
        sender=sender, is_mine=is_mine, content=m.get("content", ""),
        content_type=ctype, complete=1,
        partial_top=bool(m.get("partial_top")),
        partial_bottom=bool(m.get("partial_bottom")),
        mentions=m.get("mentions") or [], media_path=m.get("media_path") or "",
        ocr_conf=m.get("ocr_conf"),
        kind="divider" if ctype == "time_divider" else "msg",
    )


def _entry_key(e):
    from src.interaction.msglog.message_log import normalize
    return f"{e.sender}|{e.content_type}|{normalize(e.content)[:30]}"


def scroll_to_latest(dev, n=30):
    for _ in range(n):
        dev.swipe(540, 1400, 540, 1100, 150)
        time.sleep(0.3)


def do_swipe(dev, direction="earlier"):
    """控制滚动距离的快滑：fling 物理模型反解时长，总滚动压在 target(1350~1450px)。

    总滚动 = 0.75×消息区高 ≈1417px（设计 §1）：大位移快采，但必须 < 消息区高
    (≈1910px) 且 < find_overlap_dy 横带可配准上限(~1640px)——否则相邻两屏无
    重叠、配不上（重叠 = 消息区高 − dy ≈ 473px > 0）。故保留 plan_swipe 反解
    时长控制位移，只把 dev.swipe（swipe_zone 含 wait_random 100~250ms）换成
    直发 input swipe 省开销。
    """
    target = random.uniform(*SCROLL_RANGE)       # 目标总滚动 1000~1400px
    swipe_px = random.uniform(*SWIPE_RANGE)      # 手指位移 600~900px
    plan = plan_swipe(target, swipe=swipe_px)    # 反解时长，控制总滚动=target
    dur_ms = int(plan.duration_ms * random.uniform(*DUR_JITTER))
    x = int(random.uniform(*X_RANGE))
    y = int(random.uniform(*Y_START_RANGE))
    if direction == "earlier":
        dev._shell(f"input swipe {x} {y} {x} {y + int(plan.swipe_px)} {dur_ms}")
    else:
        dev._shell(f"input swipe {x} {y} {x} {y - int(plan.swipe_px)} {dur_ms}")


def realtime_scan(conn, group, max_rounds=40, on_new=None, scroll_back=True,
                  dev=None, stop_empty_rounds=0, reconcile_uncertain=False):
    """实时滚动采集：每屏识别 → 全局去重 → 实时入库。返回累计入库消息数。

    stop_empty_rounds>0 时：连续 N 屏入库 0 条（已滚到库里已有的历史、即
    「上一次的位置」）就提前停止，不再继续往上翻。

    reconcile_uncertain=True 时：识别双因子失配（slice_chat 标 uncertain_entity）
    的消息，会在其仍在屏上时点头像进资料页调和（改名/换头像），再退回聊天页
    继续滚动。默认 False（不打断滚动）。
    """
    dev = dev or DeviceCtl()
    rm = RosterMatcher(group)
    session_id = message_log.get_or_create_session(conn, group, is_group=True)

    if scroll_back:
        print("滑回最新位置...")
        scroll_to_latest(dev)
        time.sleep(1.0)

    seen = set()
    reconciled = set()   # 已调和的成员（按昵称去重，避免每屏重复点头像）
    total = 0
    empty_streak = 0     # 连续入库 0 条的屏数（用于「翻到上一次位置」提前停）
    cur_divider = None   # 最近的时间分割线文本（跨屏传播，供 ts_hint 解析）

    for rnd in range(max_rounds):
        img = dev.capture_bytes()
        ocr = run_ocr(img)
        res = slice_chat(img, ocr, is_group=True, title=group, roster_matcher=rm)

        new_entries = []
        clipped = 0
        clipped_saved = 0
        for m in res["messages"]:
            c = classify_message(m)
            ctype = m.get("content_type")
            if ctype == "time_divider":
                # 时间分割线：更新跨屏时间锚 + 入库（为后续消息提供 ts_hint，
                # 网关「最新在前」按发送时间排序依赖此锚）。
                cur_divider = m.get("content") or cur_divider
                e = to_entry(m, group)
                k = _entry_key(e)
                if k not in seen:
                    seen.add(k)
                    new_entries.append(e)
                continue
            if c["state"] == "complete":
                e = to_entry(m, group)
                e.time_hint = cur_divider
                k = _entry_key(e)
                if k not in seen:
                    seen.add(k)
                    e.crop_path = save_crop(img, group, c["y_top"], c["y_bottom"], k)
                    new_entries.append(e)
            elif c["state"] == "bottom_clipped":
                # 长消息尾部被输入栏截断：有身份+可见文本则落库为截断实例
                # （complete 字段由 partial_bottom 在 _insert_rows 里自动置 0）。
                # multimedia 空内容跳过（图片完整版靠其他屏 complete，无文本可存）。
                e = to_entry(m, group)
                e.time_hint = cur_divider
                if e.sender and e.content.strip() \
                        and e.content_type in ("text", "quote"):
                    k = _entry_key(e)
                    if k not in seen:
                        seen.add(k)
                        e.crop_path = save_crop(img, group, c["y_top"], c["y_bottom"], k)
                        new_entries.append(e)
                        clipped_saved += 1
                clipped += 1
            else:  # top_clipped / both_clipped / unidentifiable：无身份锚，暂跳过
                clipped += 1

        # 识别失配 → 点头像进资料页调和（inline：消息仍在屏上时做，然后退回聊天页）
        if reconcile_uncertain:
            from src.interaction.ports.android.perception.roster_update import \
                reconcile_on_mismatch
            for m in res["messages"]:
                if not m.get("uncertain_entity"):
                    continue
                key = (m.get("nickname") or m.get("matched_user_name") or "").strip()
                if not key or key in reconciled:
                    continue
                reconciled.add(key)
                try:
                    actions = reconcile_on_mismatch(dev, group, m, rm=rm)
                    if actions:
                        print(f"    [reconcile] {key}: {actions}")
                except Exception as e:  # noqa: BLE001
                    print(f"    [reconcile] {key} 失败: {e}")
                try:
                    dev.back()          # 从资料页退回聊天页，继续滚动
                    time.sleep(0.8)
                except Exception:
                    pass

        n = 0
        if new_entries:
            r = message_log.append_incremental(
                conn, session_id, new_entries, source="incremental", gap_ok=True)
            n = r.get("inserted", 0)
            total += n
            print(f"  [rnd{rnd+1}] 新识别 {len(new_entries)} 条，入库 {n} 条"
                  f"（残缺 {clipped} 条，其中落库截断 {clipped_saved} 条），"
                  f"累计 {total} 条")
            if on_new and n:
                on_new(new_entries)
        else:
            print(f"  [rnd{rnd+1}] 无新消息（残缺 {clipped} 条）")

        # 停止判定：连续 N 屏入库 0 条 → 已到「上一次的位置」，提前停
        empty_streak = empty_streak + 1 if n == 0 else 0
        if stop_empty_rounds > 0 and empty_streak >= stop_empty_rounds:
            print(f"  [rnd{rnd+1}] 连续 {empty_streak} 屏无新入库，已到上一次位置，停止")
            break

        if rnd < max_rounds - 1:
            do_swipe(dev, "earlier")
            time.sleep(SLEEP_S)

    return total
