# -*- coding: utf-8 -*-
"""scripts/backfill_replay_msgs.py — 对 replay 每张 stitch 图做【统一裁切线】识别，
写入 manifest 的 msgs 字段，供 /scroll_flow 第4/5列展示。

裁切标准（2026-08-14 用户定稿，非常单一）：
  ① 头像上边缘（完整头像顶，贴顶残缺跳过）
  ② 时间戳上下边沿（水平中间段的连通圆角矩形，OCR 时间格式定位 + 扩展）

每段（相邻裁切线之间）输出：内容 + 识别方式（双因子/单因子/未知/时间/自己）。

用法：
    .venv/bin/python（项目内 venv） scripts/backfill_replay_msgs.py <replay名>
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from src.interaction.ports.android.perception.ocr_engine import run_ocr
from src.interaction.ports.android.perception.chat_slicer import (
    _build_masks, _detect_avatars, _merge_avatars, AVATAR_EDGE_MARGIN)
from src.interaction.ports.android.perception.img_utils import estimate_bg, comps_from_mask
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher

AVATAR_STD_H = 105


def avatar_top_lines(img):
    """完整头像上边缘（统一裁切线①）。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    H = img.shape[0]
    bg = estimate_bg(gray, 0, H)
    _, _, nonbg = _build_masks(img, gray, hsv, bg, 0, H)
    avs = _merge_avatars(_detect_avatars(gray, hsv, nonbg, 0, H))
    lines = []
    for a in avs:
        if a["h"] >= 85 and not a["low_confidence"]:
            if a["y"] > AVATAR_EDGE_MARGIN or a["h"] >= AVATAR_STD_H:
                lines.append(int(a["y"]))
    return lines


def timestamp_edges(img):
    """时间戳圆角矩形上下边沿（统一裁切线②）。OCR 时间格式定位 + 非背景扩展。"""
    items = run_ocr(img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    bg = float(np.median(gray[200:min(600, H), 100:980]))
    edges = set()
    for it in items:
        txt = (it["text"] or "").strip()
        if not re.fullmatch(r"\d{1,2}[:：]\d{2}", txt) and "昨天" not in txt:
            continue
        b = it["box"]
        x0, y0, x1, y1 = (int(v) for v in b)
        cx = (x0 + x1) / 2
        if not (250 <= cx <= 830):
            continue
        row = gray[y0:y1, 250:830]
        frac = float((np.abs(row.astype(int) - int(bg)) > 25).mean())
        if frac < 0.05:
            continue

        def bg_frac(yy):
            if yy < 0 or yy >= H:
                return 0.0
            r = gray[yy, 250:830]
            return float((np.abs(r.astype(int) - int(bg)) > 25).mean())

        yt = y0
        while yt > 0 and bg_frac(yt - 1) >= 0.05:
            yt -= 1
        yb = y1
        while yb < H and bg_frac(yb) >= 0.05:
            yb += 1
        if 15 <= (yb - yt) <= 170:
            edges.add(yt)
            edges.add(yb)
    return sorted(edges)


def factor_of(m):
    ctype = m.get("content_type")
    if ctype == "time_divider":
        return "时间"
    side = m.get("side")
    if side == "self":
        return "自己"
    matched = m.get("matched_user_name")
    unc = m.get("uncertain_entity")
    if matched and not unc:
        return "双因子"
    if matched or m.get("nickname"):
        return "单因子"
    return "未知"


def main():
    name = sys.argv[1]
    d = os.path.join(PROJECT_ROOT, "workspace", "replays", name)
    mp = os.path.join(d, "manifest.json")
    manifest = json.load(open(mp, encoding="utf-8"))
    group = manifest.get("group", "")
    from src.interaction.loop.cutline_segment import segment_cutlines, avatar_top_lines
    from src.interaction.ports.android.perception.roster_matcher import RosterMatcher
    rm = RosterMatcher(group)
    screens = manifest["screens"]
    total = 0
    for i, s in enumerate(screens):
        sp = os.path.join(d, s["stitch"])
        if not os.path.isfile(sp):
            s["msgs"] = []
            continue
        img = cv2.imread(sp)
        segs = segment_cutlines(img, roster_matcher=rm, title=group)
        s["msgs"] = segs
        # 蓝虚线 = 头像上边缘（用户定稿：裁切线只有头像上边缘+时间戳边沿）
        s["avatar_lines"] = avatar_top_lines(img)
        total += len(segs)
        print(f"[{i:02d}] {s['stitch']}: 裁切线分段 → {len(segs)} 段")
    json.dump(manifest, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"完成：共 {total} 段 → {mp}")


if __name__ == "__main__":
    main()
