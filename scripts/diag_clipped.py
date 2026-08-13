# -*- coding: utf-8 -*-
"""scripts/diag_clipped.py — 观察真实扫描中 clipped 消息的形态（方向/状态/内容）。"""
import sys
sys.path.insert(0, ".")
import time

from src.interaction.ports.android.device.device_ctl import DeviceCtl
from src.interaction.ports.android.perception.ocr_engine import run_ocr
from src.interaction.ports.android.perception.chat_slicer import slice_chat, classify_message
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher
from src.interaction.loop.scroll_stitch import find_overlap_dy
from src.interaction.loop.realtime_scan import do_swipe, scroll_to_latest

GROUP = "YOUSAOBI"
N = 8

dev = DeviceCtl()
rm = RosterMatcher(GROUP)


def show(m, tag):
    c = classify_message(m)
    sender = m.get("matched_user_name") or m.get("nickname") or ("我" if m.get("side") == "self" else "?")
    av = m.get("avatar")
    av_s = f"av@{av['y']}" if av else "av=None"
    pt = bool(m.get("partial_top")); pb = bool(m.get("partial_bottom"))
    content = (m.get("content") or "").replace("\n", "⏎")
    print(f"      [{tag}] state={c['state']:16s} pt={int(pt)} pb={int(pb)} "
          f"y={c['y_top']}..{c['y_bottom']} {av_s} sender={sender[:10]!r} "
          f"ct={m.get('content_type'):11s} len={len(content)} content={content[:36]!r}")


print(f"=== clipped 诊断（群 {GROUP}，看更早方向）===")
scroll_to_latest(dev, n=25)
time.sleep(1.0)
prev_img = None

for rnd in range(N):
    img = dev.capture_bytes()
    dy, conf = (find_overlap_dy(prev_img, img) if prev_img is not None else (0.0, 0.0))
    print(f"\n[屏 {rnd+1}] dy={dy:5.0f} conf={conf:.3f}")
    ocr = run_ocr(img)
    res = slice_chat(img, ocr, is_group=True, title=GROUP, roster_matcher=rm)
    for m in res["messages"]:
        c = classify_message(m)
        if c["state"] in ("top_clipped", "bottom_clipped", "both_clipped"):
            show(m, "clip")
        elif c["state"] == "complete":
            show(m, "comp")
    prev_img = img
    if rnd < N - 1:
        do_swipe(dev, "earlier")
        time.sleep(0.7)
