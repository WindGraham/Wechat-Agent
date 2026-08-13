# -*- coding: utf-8 -*-
"""scripts/scroll_scan.py — 双向滑动扫描，输出全部完整消息

核心闭环：
  截屏 → slice_chat → 每条消息 classify_message 判四态
    ├─ complete     → 记录（指纹去重，身份+内容可用）
    ├─ top_clipped  → 手指下划（看更早）让头像露出
    ├─ bottom_clipped → 手指上划（看更新）让正文露出
    ├─ both_clipped → 超长消息，双向都要补
    └─ unidentifiable → 记录原因，滑动救不了
"""

import os
import json
import time
import cv2

from src.interaction.ports.android.device.device_ctl import DeviceCtl
from src.interaction.ports.android.perception.ocr_engine import run_ocr
from src.interaction.ports.android.perception.chat_slicer import slice_chat, classify_message
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher

GROUP = "交流一下？"
OUT_DIR = "workspace/test_scroll_scan"
MAX_PER_PHASE = 20   # 每个方向最多滑多少屏
STEP = 300           # 滑动步长（px）

os.makedirs(OUT_DIR, exist_ok=True)
dev = DeviceCtl()
rm = RosterMatcher(GROUP)


def fingerprint(m):
    """消息指纹：sender + content_norm 前 40 字。图片消息用头像 x/y 近似。"""
    sender = m.get("matched_user_name") or m.get("nickname") or m.get("side") or "?"
    cn = (m.get("content_norm") or "").strip()
    if cn:
        return f"{sender}|{m.get('content_type')}|{cn[:40]}"
    av = m.get("avatar") or {}
    return f"{sender}|{m.get('content_type')}|img@{av.get('x', 0)},{av.get('y', 0)}"


def in_group(ocr):
    """检查是否还在群聊页：标题栏（y<200）有『交流一下？』。"""
    return any('交流一下' in t["text"] and t["box"][1] < 200 for t in ocr)


def scan_one_direction(direction, max_screens):
    """朝一个方向滑动扫描。direction: 'earlier'(手指下划/看更早) | 'later'(手指上划/看更新)"""
    records = {}
    for i in range(max_screens):
        img = dev.capture_bytes()
        ocr = run_ocr(img)
        if not in_group(ocr):
            print(f"  [滑出群] 第 {i+1} 屏检测到已离开群聊，停止本方向")
            break
        res = slice_chat(img, ocr, is_group=True, title=GROUP, roster_matcher=rm)
        for m in res["messages"]:
            c = classify_message(m)
            fp = fingerprint(m)
            rec = {
                "state": c["state"],
                "sender": m.get("matched_user_name") or m.get("nickname") or (m.get("side") if m.get("side") == "self" else ""),
                "content": m.get("content", ""),
                "content_type": m.get("content_type"),
                "y_top": c["y_top"], "y_bottom": c["y_bottom"],
            }
            old = records.get(fp)
            # complete 优先于残缺（同一条消息，取完整那次）
            if old is None or (c["state"] == "complete" and old["state"] != "complete"):
                records[fp] = rec
        # 滑动
        if i < max_screens - 1:
            if direction == "earlier":
                dev.swipe(540, 1100, 540, 1400, STEP)   # 手指下划，看更早
            else:
                dev.swipe(540, 1400, 540, 1100, STEP)   # 手指上划，看更新
            time.sleep(1.5)
    return records


# Phase 1: 看更早（补全顶部残缺）
print("=== Phase 1: 看更早（手指下划）===")
records = scan_one_direction("earlier", MAX_PER_PHASE)
print(f"Phase1 收集 {len(records)} 条去重消息")

# Phase 2: 看更新（补全底部残缺）
print("=== Phase 2: 看更新（手指上划）===")
records.update(scan_one_direction("later", MAX_PER_PHASE))
print(f"合并后共 {len(records)} 条去重消息")

# 汇总四态
from collections import Counter
cnt = Counter(r["state"] for r in records.values())
print("=== 最终四态统计 ===")
for k, v in cnt.most_common():
    print(f"  {k:16s} {v}")

# 输出
out = sorted(records.values(), key=lambda r: (r["state"], r["y_top"]))
with open(os.path.join(OUT_DIR, "scan_result.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"\n结果已写入 {OUT_DIR}/scan_result.json")

# 打印 complete 的消息
complete = [r for r in records.values() if r["state"] == "complete"]
print(f"\n=== complete 消息（{len(complete)} 条）===")
for r in complete[:40]:
    print(f"  [{r['content_type']:11s}] {r['sender'][:12]:12s} {r['content'][:28]!r}  y={r['y_top']}~{r['y_bottom']}")
