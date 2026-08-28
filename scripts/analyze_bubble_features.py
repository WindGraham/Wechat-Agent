# -*- coding: utf-8 -*-
"""analyze_bubble_features.py — 文本气泡 vs 多媒体消息的 CV 特征分析。

依据用户实测发现的三条文本气泡特征：
  1. 尖角：文本气泡在【固定位置】（他人=气泡左缘、自己=右缘，相对气泡顶
     固定高度）带一个凸起小三角。表情包内容若是气泡截图，尖角不在固定位置；
  2. 气泡颜色回归：文本气泡内部是固定的单一颜色（深色模式他人≈深灰、
     自己≈绿色），拟合后是非常规律的值；图片/表情包内部颜色杂；
  3. 宽度规律：超过两行的气泡宽度固定（顶到最大宽），宽度非固定的气泡
     只有一行。

输入：collect_debug 落盘目录（含 screen_XX_msg_XX.jpg 单条消息裁切 +
screen_XX_full.jpg 原图 + manifest.json）。
输出：每条消息的特征表（stdout）+ 汇总统计。
"""
import json
import os
import sys
import glob

import cv2
import numpy as np

# 消息内容区 X 范围（他人消息气泡在头像右侧）
AVATAR_L_RIGHT = 150      # 左列头像右缘
SELF_AVATAR_X = 930       # 右列自己头像左缘
SCREEN_W = 1080

# 尖角检测参数（相对气泡顶部的固定高度带，真机校准）
TAIL_Y0, TAIL_Y1 = 8, 34          # 尖角纵向位置（气泡顶向下）
TAIL_MAX_PROTRUDE = 30            # 尖角最大凸出宽度


def dominant_bg_color(img):
    """消息裁切条的背景色：四边众数。"""
    edges = np.concatenate([img[0].reshape(-1, 3), img[-1].reshape(-1, 3),
                            img[:, 0].reshape(-1, 3), img[:, -1].reshape(-1, 3)])
    vals, counts = np.unique(edges.reshape(-1, 3), axis=0, return_counts=True)
    return vals[counts.argmax()]


def content_mask(img, bg, tol=18):
    """非背景掩膜：与背景色差 > tol 的像素。"""
    diff = np.abs(img.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    mask = (diff > tol).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return mask


def tail_protrusion(img, comp_box, side="L"):
    """尖角凸出量：组件主矩形左/右缘外、固定高度带内的额外凸出像素宽度。

    文本气泡尖角在固定高度带向外凸出 8~20px 的三角；矩形图片/表情包
    边缘是直的（凸出≈0）；气泡截图表情包的尖角不在固定高度带。
    返回凸出像素数（0=无尖角）。
    """
    x, y, w, h = comp_box
    if side == "L":
        # 看组件左缘以左 [x-30, x) 区域在固定高度带内是否有内容
        band_x0 = max(0, x - TAIL_MAX_PROTRUDE)
        roi = img[y + TAIL_Y0: min(y + TAIL_Y1, y + h), band_x0:x]
    else:
        roi = img[y + TAIL_Y0: min(y + TAIL_Y1, y + h), x + w: x + w + TAIL_MAX_PROTRUDE]
    if roi.size == 0:
        return 0
    bg = dominant_bg_color(img)
    m = content_mask(roi, bg)
    cols = np.where(m.any(axis=0))[0]
    return int(len(cols))


def color_uniformity(img, comp_box, bg):
    """气泡颜色回归：组件内部的【主导色占比】。

    文本气泡内部≈100% 是固定气泡色（除文字像素）；图片/表情包内部杂色
    → 主导色占比低。返回 (主导色BGR, 占比)。
    """
    x, y, w, h = comp_box
    roi = img[y:y + h, x:x + w]
    if roi.size == 0:
        return None, 0.0
    # 量化到 16 级/通道减少文字抗锯齿噪声
    q = (roi // 16 * 16).reshape(-1, 3)
    vals, counts = np.unique(q, axis=0, return_counts=True)
    i = counts.argmax()
    dom, frac = vals[i], counts[i] / len(q)
    # 排除背景色（组件外溢），取组件内部主导非背景色
    if np.abs(dom.astype(np.int16) - bg.astype(np.int16)).max() <= 18:
        counts[i] = 0
        if counts.sum() == 0:
            return dom, 0.0
        i = counts.argmax()
        dom, frac = vals[i], counts[i] / len(q)
    return dom, float(frac)


def analyze_msg_crop(path):
    """分析单条消息裁切，返回特征 dict 列表（每个内容组件一条）。"""
    img = cv2.imread(path)
    if img is None:
        return []
    bg = dominant_bg_color(img)
    mask = content_mask(img, bg)
    # 昵称行/时间小字在头像右侧上方，先不管；找大连通域作为内容候选
    n, lab, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 800 or w < 20 or h < 15:
            continue
        dom, frac = color_uniformity(img, (x, y, w, h), bg)
        side = "L" if x < SCREEN_W // 2 else "R"
        tail = tail_protrusion(img, (x, y, w, h), side)
        out.append({
            "box": (int(x), int(y), int(w), int(h)),
            "area": int(area),
            "fill": round(float(area / (w * h)), 3),
            "aspect": round(w / h, 2),
            "dom_color": dom.tolist() if dom is not None else None,
            "dom_frac": round(frac, 3),
            "tail_px": tail,
            "side": side,
        })
    out.sort(key=lambda c: c["box"][1])
    return out


def main(run_dir):
    manifest = json.load(open(os.path.join(run_dir, "manifest.json")))
    rows = []
    for s in manifest["screens"]:
        for msg in s.get("msgs", []):
            p = os.path.join(run_dir, msg["file"])
            feats = analyze_msg_crop(p)
            rows.append((s, msg, feats))
            main = max(feats, key=lambda c: c["area"], default=None)
            content = (msg.get("content") or "")[:22].replace("\n", " ")
            if main:
                print(f"{msg['file']}: box={main['box']} fill={main['fill']} "
                      f"dom%={main['dom_frac']} tail={main['tail_px']}px "
                      f"side={main['side']} | {msg['factor']} | {content}")
            else:
                print(f"{msg['file']}: (无内容组件) | {msg['factor']} | {content}")
    return rows


if __name__ == "__main__":
    rd = sys.argv[1] if len(sys.argv) > 1 else sorted(
        glob.glob("workspace/collect_debug/特高课/smoke_*"))[-1]
    main(rd)
