# -*- coding: utf-8 -*-
"""scripts/scroll_capture.py — 实时滑动采集 + 边采边判（看更早消息）

连续手指下划看更早消息，每屏截屏 + slice_chat 识别，实时标记 partial_top / partial_bottom。
采集结果存 workspace/test_scroll_screenshots/，识别汇总存 scroll_report.json
"""

import os
import json
import time
import cv2

from src.interaction.ports.android.device.device_ctl import DeviceCtl
from src.interaction.ports.android.perception.ocr_engine import run_ocr
from src.interaction.ports.android.perception.chat_slicer import slice_chat
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher

GROUP = "交流一下？"
OUT_DIR = "workspace/test_scroll_screenshots"
SWIPE = (540, 1100, 540, 1400)   # 手指下划（看更早），步长 300px（保证屏间重叠）
N_SCREENS = 16                   # 采集屏数
BACK_SWIPES = 12                 # 先滑回最新的次数

os.makedirs(OUT_DIR, exist_ok=True)
dev = DeviceCtl()
rm = RosterMatcher(GROUP)

def summarize(img, name):
    ocr = run_ocr(img)
    res = slice_chat(img, ocr, is_group=True, title=GROUP, roster_matcher=rm)
    msgs = res["messages"]
    first = msgs[0] if msgs else None
    last = msgs[-1] if msgs else None
    return {
        "screenshot": name,
        "message_count": len(msgs),
        "first": {
            "content": (first.get("content") or "")[:30] if first else "",
            "nickname": first.get("nickname") or first.get("matched_user_name") or "",
            "partial_top": bool(first.get("partial_top")) if first else False,
            "avatar_h": first["avatar"]["h"] if first and first.get("avatar") else None,
        } if first else None,
        "last": {
            "content": (last.get("content") or "")[:30] if last else "",
            "nickname": last.get("nickname") or last.get("matched_user_name") or "",
            "partial_bottom": bool(last.get("partial_bottom")) if last else False,
        } if last else None,
        "partial_top_msgs": [(m.get("nickname") or m.get("matched_user_name") or "?", (m.get("content") or "")[:20])
                             for m in msgs if m.get("partial_top")],
        "partial_bottom_msgs": [(m.get("nickname") or m.get("matched_user_name") or "?", (m.get("content") or "")[:20])
                                for m in msgs if m.get("partial_bottom")],
    }

# 1. 滑回最新（手指上划，看更新消息）
print(f"滑回最新（{BACK_SWIPES} 次）...")
for _ in range(BACK_SWIPES):
    dev.swipe(540, 1400, 540, 1100, 300)
    time.sleep(0.5)
time.sleep(1.5)

# 2. 重新采集
reports = []
for i in range(N_SCREENS):
    img = dev.capture_bytes()
    name = f"scroll_{i:02d}.png"
    cv2.imwrite(os.path.join(OUT_DIR, name), img)
    s = summarize(img, name)
    reports.append(s)
    f, l = s["first"], s["last"]
    print(f"[{i+1}/{N_SCREENS}] {name}: {s['message_count']}条 | 顶pt={f['partial_top'] if f else None}(h={f['avatar_h'] if f else None}) | 底pb={l['partial_bottom'] if l else None}")
    if i < N_SCREENS - 1:
        dev.swipe(*SWIPE, 300)
        time.sleep(1.6)

with open(os.path.join(OUT_DIR, "scroll_report.json"), "w", encoding="utf-8") as fp:
    json.dump(reports, fp, ensure_ascii=False, indent=2)
print(f"\n✅ 采集完成：{N_SCREENS} 屏 → {OUT_DIR}/")
