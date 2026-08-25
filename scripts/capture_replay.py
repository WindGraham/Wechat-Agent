# -*- coding: utf-8 -*-
"""scripts/capture_replay.py — 滚动回放采集：从最新屏回溯，逐屏保存截图+裁切+分割+位移。

供网关 /scroll_replay 页面展示（书签+union 缝合算法的可视化验证）：
每屏保存 full（原图）、crop（消息区），并在 manifest.json 记录：
  - dy：与上一屏的内容位移（重叠区域标注用）
  - crop_top/crop_bottom：消息区裁切范围（排除置顶条/输入栏）
  - split_y：第一条完整头像顶（消息区坐标），分割残缺块 B(上)/A(下) 用

前置：用户已进入群且停在最新底部（与 collect_group_history 相同语义）。
停止：union 完整包含上次书签（新消息采完）/ 滑到顶 / --max-screens。

用法：
    ~/.venvs/wechat-agent/bin/python scripts/capture_replay.py [群名] [--max-screens 12]
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
from src.interaction.loop.scroll_stitch import find_overlap_dy, stitch_union
from src.interaction.loop.history_collect import (
    _load_anchor, _anchor_in_union, _content_crop_bounds, _screen_bounds)
from src.interaction.ports.android.perception.page_detector import (
    detect_pinned_bar_end, detect_input_bar_top)
from src.interaction.ports.android.perception.chat_slicer import (
    _build_masks, _merge_avatars, _detect_avatars, AVATAR_EDGE_MARGIN)
from src.interaction.ports.android.perception.img_utils import estimate_bg
from src.interaction.ports.android.perception import layout_consts as LC


def _first_avatar_y(crop):
    """消息区（crop）内第一条完整头像顶（分割残缺块 B/A 用），无头像返回 None。

    2026-08-14 修复：残缺头像（顶部被裁、只露底部长方条）会被旧逻辑误判为
    「完整」（h=92 ≥ AVATAR_MIN_H=85 通过），把分割线标到 y=0（screen_04
    实测）。真实完整头像高 108~112px（近正方形）。修正：贴裁切顶
    (y ≤ AVATAR_EDGE_MARGIN) 且高度未达标准高(≥105) 的候选视为残缺跳过；
    高度 ≥105 的贴顶候选仍算完整（内容区顶部真头像顶恰好在 y=0 的场景）。
    """
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    bg = estimate_bg(gray, 0, crop.shape[0])
    _, _, nonbg = _build_masks(crop, gray, hsv, bg, 0, crop.shape[0])
    avs = _merge_avatars(_detect_avatars(gray, hsv, nonbg, 0, crop.shape[0]))
    avs.sort(key=lambda a: (a["low_confidence"], a["y"]))
    if not avs:
        return None
    AVATAR_STD_H = 105   # 标准完整头像高下限（实测 108~112）
    for a in avs:
        if a["h"] >= LC.AVATAR_MIN_H and not a["low_confidence"]:
            if a["y"] > AVATAR_EDGE_MARGIN or a["h"] >= AVATAR_STD_H:
                return int(a["y"])
    return int(avs[0]["y"])


def main():
    ap = argparse.ArgumentParser(description="回溯滚动回放采集（可视化书签+union 缝合）")
    ap.add_argument("group", nargs="?", default="被打信科2026游泳馆")
    ap.add_argument("--max-screens", type=int, default=12)
    args = ap.parse_args()

    dev = DeviceCtl()
    out_dir = os.path.join(PROJECT_ROOT, "workspace", "replays",
                           time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    anchor = _load_anchor(args.group)
    print(f"群: {args.group} | 输出: {out_dir}")
    print(f"书签存在: {anchor is not None}"
          + (f" 尺寸={anchor.shape}" if anchor is not None else ""))

    manifest = {"group": args.group, "screens": []}
    prev = None
    prev_crop = None
    prev_split = None
    for i in range(args.max_screens):
        img = dev.capture_bytes()
        if float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean()) < 5:
            print(f"[{i}] 黑屏，停止")
            break

        dy = 0.0
        if prev is not None:
            # 2026-08-14 用户定稿：重叠检测在裁切消息区上做（排除固定 UI 污染）
            prev_c, _, _ = _content_crop_bounds(prev)
            cur_c, _, _ = _content_crop_bounds(img)
            dy, conf = find_overlap_dy(prev_c, cur_c)
            if conf < 0.5:
                time.sleep(1.0)
                img = dev.capture_bytes()
                cur_c, _, _ = _content_crop_bounds(img)
                dy, conf = find_overlap_dy(prev_c, cur_c)
                if conf < 0.5:
                    print(f"[{i}] 糊帧，停止")
                    break
            if dy < 40:
                time.sleep(3.0)
                RS.do_swipe(dev, "earlier")
                time.sleep(3.0)
                img = dev.capture_bytes()
                cur_c, _, _ = _content_crop_bounds(img)
                dy2, conf2 = find_overlap_dy(prev_c, cur_c)
                if dy2 < 40 or conf2 < 0.5:
                    print(f"[{i}] 滑到顶(dy={dy2:.0f})，停止")
                    break
                dy = dy2

        crop, top, bottom = _content_crop_bounds(img)
        split_y = _first_avatar_y(crop)
        fn_full = f"screen_{i:02d}_full.jpg"
        fn_crop = f"screen_{i:02d}_crop.jpg"
        cv2.imwrite(os.path.join(out_dir, fn_full), img,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
        cv2.imwrite(os.path.join(out_dir, fn_crop), crop,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])

        # ---- 去重+拼接结果（第三列展示，2026-08-14 用户定稿）：
        # 去掉与上一张的重叠（本屏底部 [dy,H]），补上上一张的残缺部分
        # （B1 = 上一张 [0:split]）——即 A/B 缝合段 A2(上) + B1(下)。
        # screen0 无上一张：取完整段 A1 = [split:H]。
        fn_stitch = f"screen_{i:02d}_stitch.jpg"
        stitch_top_rel = split_y or 0          # 缝合段在【本屏裁切】里的顶偏移
        if i == 0:
            stitch = crop[stitch_top_rel:]
        else:
            parts = []
            if split_y is not None and split_y < dy:
                parts.append(crop[split_y:int(dy)])      # A2：本屏新增部分的完整段
            if prev_split is not None and prev_split > 0:
                parts.append(prev_crop[0:prev_split])    # B1：上一张顶部残缺
            stitch = np.vstack(parts) if parts else crop
        cv2.imwrite(os.path.join(out_dir, fn_stitch), stitch,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
        manifest["screens"].append({
            "full": fn_full, "crop": fn_crop,
            "dy": round(float(dy), 1),
            "crop_top": int(top), "crop_bottom": int(bottom),
            "split_y": split_y,
            "stitch": fn_stitch,
            "stitch_top_rel": int(stitch_top_rel),
            "stitch_h": int(stitch.shape[0]),
        })
        print(f"[{i:02d}] dy={dy:6.1f} 裁切[{top},{bottom}] 分割@{split_y} "
              f"缝合{stitch.shape[0]}px")
        prev_crop, prev_split = crop, split_y

        # 书签检测：union 完整包含上次书签 = 新消息采完
        if anchor is not None and prev is not None:
            st = stitch_union(prev, img, dy)
            if st is not None and _anchor_in_union(anchor, st[0]) >= 0.8:
                print(f"[{i:02d}] 已到上次书签（union 完整包含），停止")
                break

        prev = img
        if i < args.max_screens - 1:
            RS.do_swipe(dev, "earlier")
            time.sleep(0.8)

    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"\n完成：{len(manifest['screens'])} 屏 → {out_dir}")
    print("网关查看: http://127.0.0.1:13014/scroll_replay?r="
          + os.path.basename(out_dir))


if __name__ == "__main__":
    main()
