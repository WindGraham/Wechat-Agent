# -*- coding: utf-8 -*-
"""scripts/backfill_replay_stitch.py — 给已采集的 replay 目录补算 dy/split/stitch。

capture_scroll_live.py 只存 full+crop（新背景验证阶段不写识别逻辑）；本脚本
用与 capture_replay.py 相同的算法补上第三列/重叠线所需的字段并生成 stitch 图：
  - dy：相邻两屏裁切区实测位移（find_overlap_dy）
  - split_y：本屏裁切内第一条完整头像顶
  - stitch：A2(本屏[split:dy]) + B1(上一张[0:split])，screen0 = A1[split:]
用法：
    .venv/bin/python（项目内 venv） scripts/backfill_replay_stitch.py <replay名>
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from src.interaction.loop.scroll_stitch import find_overlap_dy
from src.interaction.loop.history_collect import _content_crop_bounds
from scripts.capture_replay import _first_avatar_y   # 复用首头像顶

def main():
    name = sys.argv[1]
    d = os.path.join(PROJECT_ROOT, "workspace", "replays", name)
    mp = os.path.join(d, "manifest.json")
    m = json.load(open(mp, encoding="utf-8"))
    screens = m["screens"]
    prev_crop = None
    prev_split = None
    for i, s in enumerate(screens):
        img = cv2.imread(os.path.join(d, s["full"]))
        crop, top, bottom = _content_crop_bounds(img)
        s["crop_top"], s["crop_bottom"] = int(top), int(bottom)
        # 重写 crop 图片（边界检测修正后，旧 crop 文件可能带黑带/偏差）
        cv2.imwrite(os.path.join(d, s["crop"]), crop,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
        dy = 0.0
        s["dy"] = 0.0
        if prev_crop is not None:
            dy, conf = find_overlap_dy(prev_crop, crop)
            if conf < 0.5:
                print(f"[{i}] dy conf={conf:.2f} 低，尝试全屏回退…")
            s["dy"] = round(float(dy), 1)
        split_y = _first_avatar_y(crop)
        s["split_y"] = split_y
        s["stitch_top_rel"] = int(split_y or 0)
        if i == 0:
            stitch = crop[s["stitch_top_rel"]:]
        else:
            parts = []
            if split_y is not None and split_y < dy:
                parts.append(crop[split_y:int(dy)])
            if prev_split is not None and prev_split > 0:
                parts.append(prev_crop[0:prev_split])
            stitch = np.vstack(parts) if parts else crop
        fn = f"screen_{i:02d}_stitch.jpg"
        cv2.imwrite(os.path.join(d, fn), stitch, [cv2.IMWRITE_JPEG_QUALITY, 92])
        s["stitch"] = fn
        s["stitch_h"] = int(stitch.shape[0])
        print(f"[{i:02d}] dy={s['dy']:6.1f} split={split_y} stitch_h={stitch.shape[0]}")
        prev_crop, prev_split = crop, split_y
    json.dump(m, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("manifest 已更新:", mp)

if __name__ == "__main__":
    main()
