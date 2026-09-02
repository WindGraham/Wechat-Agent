# -*- coding: utf-8 -*-
"""scripts/capture_scroll_live.py — 新背景采集验证（只截图，不写识别/拼接逻辑）。

流程（用户定稿 2026-08-14）：
  1) 截当前屏存为【书签2】（独立副本 bookmark2_*，不覆盖书签1）
  2) 循环：上滑 → 等滚动完全停止（连续两帧像素差 < 阈值）→ 截图保存
  3) 持续运行直到外部喊停（job_kill / Ctrl-C）

用法：
    .venv/bin/python（项目内 venv） scripts/capture_scroll_live.py [群名]
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from src.interaction.ports.android.device.device_ctl import DeviceCtl
from src.interaction.loop import realtime_scan as RS
from src.interaction.loop.history_collect import _content_crop_bounds

SETTLE_DIFF_THRESH = 2.0    # 两帧 mean absdiff 低于此值 = 滚动停止
SETTLE_TIMEOUT = 10.0       # 等待停止超时（秒）
SETTLE_INTERVAL = 0.3       # 检测间隔
MAX_SCREENS = 200           # 上限（跑着等用户喊停）


def _safe(s):
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in s)


def wait_settled(dev, thresh=SETTLE_DIFF_THRESH, timeout=SETTLE_TIMEOUT):
    """滑动后等待滚动完全停止：连续两帧像素差 < 阈值。返回 (最后一帧, 最终diff, 是否停稳)。"""
    t0 = time.time()
    prev = dev.capture_bytes()
    d = 999.0
    while time.time() - t0 < timeout:
        time.sleep(SETTLE_INTERVAL)
        cur = dev.capture_bytes()
        d = float(cv2.absdiff(cur, prev).mean())
        if d < thresh:
            return cur, d, True
        prev = cur
    return cur, d, False


def main():
    ap = argparse.ArgumentParser(description="新背景采集验证：书签2 + 上滑逐屏截图")
    ap.add_argument("group", nargs="?", default="被打信科2026游泳馆")
    args = ap.parse_args()
    group = args.group

    dev = DeviceCtl()
    safe = _safe(group)
    bm_dir = os.path.join(PROJECT_ROOT, "workspace", "bookmarks", safe)
    os.makedirs(bm_dir, exist_ok=True)

    # ---- 1) 书签2：当前屏（独立副本，不覆盖书签1 的 full.jpg/crop.jpg）----
    bm = dev.capture_bytes()
    g = cv2.cvtColor(bm, cv2.COLOR_BGR2GRAY)
    print(f"书签2 尺寸={bm.shape} 亮度={float(g.mean()):.1f}")
    if float(g.mean()) < 5.0:
        print("黑屏，放弃书签2")
        return 1
    bm_crop, top, bottom = _content_crop_bounds(bm)
    cv2.imwrite(os.path.join(bm_dir, "bookmark2_full.jpg"), bm,
                [cv2.IMWRITE_JPEG_QUALITY, 95])
    cv2.imwrite(os.path.join(bm_dir, "bookmark2_crop.jpg"), bm_crop,
                [cv2.IMWRITE_JPEG_QUALITY, 95])
    with open(os.path.join(bm_dir, "meta2.json"), "w", encoding="utf-8") as f:
        json.dump({"full_h": int(bm.shape[0]), "full_w": int(bm.shape[1]),
                   "crop_top": int(top), "crop_bottom": int(bottom)}, f)
    print(f"书签2 已存: {bm_dir}/bookmark2_*  (裁切 [{top},{bottom}])")

    # ---- 2) 循环：上滑 → 停止检测 → 截图 ----
    out_dir = os.path.join(PROJECT_ROOT, "workspace", "replays",
                           time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    manifest = {"group": group, "bookmark2": "bookmarks/%s/bookmark2_full.jpg" % safe,
                "screens": []}
    print(f"输出目录: {out_dir}")
    print("开始上滑采集（说「暂停」即停）...")

    try:
        for i in range(MAX_SCREENS):
            RS.do_swipe(dev, "earlier")
            img, d, settled = wait_settled(dev)
            if float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean()) < 5.0:
                print(f"[{i:02d}] 黑屏，停止")
                break
            crop, top, bottom = _content_crop_bounds(img)
            fn_full = f"screen_{i:02d}_full.jpg"
            fn_crop = f"screen_{i:02d}_crop.jpg"
            cv2.imwrite(os.path.join(out_dir, fn_full), img,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            cv2.imwrite(os.path.join(out_dir, fn_crop), crop,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            manifest["screens"].append({
                "full": fn_full, "crop": fn_crop,
                "settled": bool(settled), "settle_diff": round(d, 2),
                "crop_top": int(top), "crop_bottom": int(bottom),
            })
            with open(os.path.join(out_dir, "manifest.json"), "w",
                      encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=1)
            print(f"[{i:02d}] 已截 {fn_full} 停稳={settled} diff={d:.2f} "
                  f"裁切[{top},{bottom}]", flush=True)
    except KeyboardInterrupt:
        print("\n用户暂停")
    print(f"\n完成：{len(manifest['screens'])} 屏 → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
