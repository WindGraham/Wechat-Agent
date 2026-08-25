# -*- coding: utf-8 -*-
"""scripts/collect_realtime_replay.py — 真机实时采集一轮（修复版 2026-08-20）。

流程：
  1. 前置：已进群停在最新底部；
  2. 每轮：fast fling 滑动 → 等待滚动【完全停稳且内容到位】
     （连续 4 帧像素差 < 阈值、且持续 1.5s 不再变）→ 截图 → 后台拼接识别
     → 落盘 → 检查旧书签；
  3. 滑动异常（dy 过小/负）自动重试一次；
  4. 结束：自动生成 prompt.txt。

滑动修复（2026-08-20）：
  - 旧版：慢拖 700ms 方向反（1550→550 = 看更新），微信不滚动；
  - 新版：fast fling 150ms 方向正（550→1550 = 看更早），触发惯性滚动。
  - 用户定稿：手指向下（y 小→大）= 看更早；fast fling <300ms 才滚动。
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
from src.interaction.loop.cutline_segment import segment_cutlines
from src.interaction.loop.history_collect import (
    _content_crop_bounds, _anchor_in_union)
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher
from scripts.capture_replay import _first_avatar_y


def fast_scroll_earlier(dev, swipe_px=1000, dur_ms=150):
    """滚动看更早：fast fling 向下（y1=550 → y2=550+swipe_px）。

    用户定稿（2026-08-15 多次纠正，权威）：
      - 手指向下（y 小→大）= 看更早（页面上滚，更早内容从顶部进入）
      - 手指向上（y 大→小）= 看更新（页面下滑，新消息从底部进入）
      - 慢拖 ≥700ms → 微信视为拖动结束，不触发滚动 ❌
      - fast fling <300ms → 触发惯性滚动 ✅
      - fling 位移随机 → 不能预设步长，必须每屏实测 dy

    参数：
      - swipe_px=1000：手指位移（550→1550，穿越消息区中部）
      - dur_ms=150：fast fling，确保触发惯性
    """
    x = 540
    y1 = 550
    y2 = y1 + swipe_px  # 1550
    dev._shell(f"input swipe {x} {y1} {x} {y2} {dur_ms}")


SETTLE_FRAMES = 4       # 连续 N 帧像素差 < 阈值才算停
SETTLE_DIFF = 2.0
SETTLE_HOLD = 1.5       # 停稳后还要保持 1.5s 不变（排除惯性/懒加载后续变化）
SETTLE_TIMEOUT = 15.0
MAX_SCREENS = 60
DY_MIN = 300            # dy 低于此值视为滑动异常（内容没滚动）


def wait_settled(dev, thresh=SETTLE_DIFF, timeout=SETTLE_TIMEOUT):
    """滑动后等待滚动完全停稳：连续 SETTLE_FRAMES 帧像素差 < 阈值，
    且停稳后保持 SETTLE_HOLD 秒。返回 (img, settled, hold_ok)。"""
    t0 = time.time()
    frames = []
    stable_since = None
    img = dev.capture_bytes()
    while time.time() - t0 < timeout:
        time.sleep(0.3)
        cur = dev.capture_bytes()
        d = float(cv2.absdiff(cur, img).mean())
        if d < thresh:
            frames.append(cur)
            if len(frames) >= SETTLE_FRAMES:
                if stable_since is None:
                    stable_since = time.time()
                if time.time() - stable_since >= SETTLE_HOLD:
                    return cur, True, True
            else:
                stable_since = None
        else:
            frames = []
            stable_since = None
        img = cur
    return img, len(frames) >= SETTLE_FRAMES, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", nargs="?", default="被打信科2026游泳馆")
    ap.add_argument("--anchor-bak", default=None)
    ap.add_argument("--resume", default=None,
                    help="续采：已有 replay 目录名，从最后一屏继续")
    args = ap.parse_args()
    group = args.group

    dev = DeviceCtl()
    rm = RosterMatcher(group)

    # ---- 书签（截止线）：默认当前 runtime 书签 = 上次采集的【第一屏】
    # （最新底部）。上次采集结束时 _save_anchor 把当次第一屏存为 runtime
    # 书签 → 本次对准它 = 滚到上次截止点。_old_bak（更早）只在显式指定时用。
    from src.interaction.loop.history_collect import _load_anchor
    if args.anchor_bak:
        apath = args.anchor_bak
        anchor = cv2.imread(apath) if os.path.exists(apath) else None
    else:
        anchor = _load_anchor(group)
    if anchor is not None:
        print(f"书签(截止线): 上次第一屏 尺寸={anchor.shape}")
    else:
        print("警告：无书签，仅采集不按书签停")

    if args.resume:
        out_dir = os.path.join(PROJECT_ROOT, "workspace", "replays", args.resume)
        manifest = json.load(open(os.path.join(out_dir, "manifest.json"), encoding="utf-8"))
        prev_img = cv2.imread(os.path.join(out_dir, manifest["screens"][-1]["full"]))
        prev_crop = cv2.imread(os.path.join(out_dir, manifest["screens"][-1]["crop"]))
        start_i = len(manifest["screens"])
        print(f"续采 {out_dir}：已有 {start_i} 屏，继续")
    else:
        out_dir = os.path.join(PROJECT_ROOT, "workspace", "replays",
                               time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(out_dir, exist_ok=True)
        manifest = {"group": group, "screens": []}
        prev_img = None
        prev_crop = None
        start_i = 0
    prev_split = None
    print(f"输出: {out_dir}")
    print("开始实时滚动采集（修复版：等滚动完全停稳+内容到位再截）...")

    try:
        for i in range(start_i, start_i + MAX_SCREENS):
            # 首屏直接截（不滑动）；后续屏先滑动
            if i > 0:
                if i == 1:
                    # 首屏在最新底部：fling 可靠触发滚动离开底部
                    # （在底部慢拖会触发列表回弹 → dy 负）
                    RS.do_swipe(dev, "earlier")
                else:
                    fast_scroll_earlier(dev)
                time.sleep(0.5)          # fast fling 启动快，减少缓冲

            img, settled, hold_ok = wait_settled(dev)
            if float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean()) < 5.0:
                print(f"[{i:02d}] 黑屏，停止")
                break

            # ---- dy 检测 + 滑动异常重试 ----
            dy = 0.0
            conf = 0.0
            st = None
            if prev_img is not None:
                prev_c, _, _ = _content_crop_bounds(prev_img)
                cur_c, _, _ = _content_crop_bounds(img)
                dy, conf = find_overlap_dy(prev_c, cur_c)
                # 滑动异常：dy 过小/负（内容没滚）→ 重滑重截一次
                if dy < DY_MIN:
                    print(f"  [{i:02d}] dy={dy:.0f} 异常（可能滑动失败/懒加载），"
                          f"等 2s 重滑重截")
                    time.sleep(2.0)
                    fast_scroll_earlier(dev)
                    time.sleep(1.5)
                    img, settled, hold_ok = wait_settled(dev)
                    cur_c, _, _ = _content_crop_bounds(img)
                    dy, conf = find_overlap_dy(prev_c, cur_c)
                # conf 低：内容变化 → 等 1s 重截一次
                if conf < 0.4:
                    time.sleep(1.0)
                    img2 = dev.capture_bytes()
                    cur_c2, _, _ = _content_crop_bounds(img2)
                    dy2, conf2 = find_overlap_dy(prev_c, cur_c2)
                    if conf2 >= 0.4:
                        dy, conf, img = dy2, conf2, img2
                st = stitch_union(prev_img, img, dy)

            # 识别段
            if st is not None:
                rec_img, cy0, cy1 = st
                rec_hint = "union"
            else:
                cy0, cy1 = _content_crop_bounds(img)[1:]
                rec_img = img
                rec_hint = "screen"

            # 统一裁切线分段
            segs = []
            try:
                segs = segment_cutlines(rec_img, roster_matcher=rm, title=group)
            except Exception as e:
                print(f"  [{i:02d}] 分段失败: {e}")

            # 落盘
            crop, top, bottom = _content_crop_bounds(img)
            split_y = _first_avatar_y(crop)
            fn_full = f"screen_{i:02d}_full.jpg"
            fn_crop = f"screen_{i:02d}_crop.jpg"
            cv2.imwrite(os.path.join(out_dir, fn_full), img,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            cv2.imwrite(os.path.join(out_dir, fn_crop), crop,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            fn_stitch = f"screen_{i:02d}_stitch.jpg"
            stitch_top_rel = split_y or 0
            if i == 0:
                stitch = crop[stitch_top_rel:]
            else:
                parts = []
                if split_y is not None and split_y < dy:
                    parts.append(crop[split_y:int(dy)])
                if prev_split is not None and prev_split > 0:
                    parts.append(prev_crop[0:prev_split])
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
                "msgs": segs,
            })
            with open(os.path.join(out_dir, "manifest.json"), "w",
                      encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=1)
            print(f"[{i:02d}] {rec_hint} dy={dy:5.0f} conf={conf:.2f} "
                  f"停稳={settled}保持={hold_ok} 分段={len(segs)}", flush=True)

            # 书签截止
            if anchor is not None and st is not None:
                aconf = _anchor_in_union(anchor, st[0])
                if aconf >= 0.8:
                    print(f"[{i:02d}] 已到旧书签（conf={aconf:.2f}），停止")
                    break

            prev_img = img
            prev_crop, prev_split = crop, split_y
    except KeyboardInterrupt:
        print("\n用户暂停")

    print(f"\n完成：{len(manifest['screens'])} 屏 → {out_dir}")
    print("网关查看: http://127.0.0.1:13014/scroll_flow?r="
          + os.path.basename(out_dir))

    # ---- 构建 prompt ----
    try:
        from src.interaction.loop.prompt_builder import build_prompt_from_manifest
        prompt_text = build_prompt_from_manifest(
            os.path.join(out_dir, "manifest.json"),
            format="text",
            group_name=group,
            include_meta=True,
        )
        prompt_path = os.path.join(out_dir, "prompt.txt")
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt_text)
        print(f"Prompt 已生成: {prompt_path} ({len(prompt_text)} 字符)")
    except Exception as e:
        print(f"Prompt 生成失败: {e}")


if __name__ == "__main__":
    main()
