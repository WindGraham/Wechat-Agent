#!/usr/bin/env python3
"""img_utils.py - v2 图像工具：连通域/掩膜/包含判断/轮廓多边形。

大部分函数自 v1 screen_parser.py 零改动平移；新增 comp_outline（气泡凸角轮廓）。
"""

import cv2
import numpy as np


def comps_from_mask(mask, min_area, close_ksize=7, min_w=0, min_h=0):
    """连通域 -> [(x,y,w,h,area), ...]（按 y 排序）。
    close_ksize 可为 (w,h) 元组做矩形闭运算（照片里的横向暗带需要更高的核）。"""
    if close_ksize:
        if isinstance(close_ksize, int):
            close_ksize = (close_ksize, close_ksize)
        k = cv2.getStructuringElement(cv2.MORPH_RECT, close_ksize)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area >= min_area and w >= min_w and h >= min_h:
            out.append((int(x), int(y), int(w), int(h), int(area)))
    out.sort(key=lambda c: (c[1], c[0]))
    return out


def comp_outline(mask, comp, offset=(0, 0), epsilon=4.0):
    """连通域的轮廓多边形（findContours + approxPolyDP），凸角（气泡尾巴）天然包含。
    mask: 该连通域来源的二值掩膜（全局坐标）；comp: (x,y,w,h,area)；
    offset: 若 mask 是裁切出来的子图，给 (dx,dy) 还原全局坐标。
    返回 [[x,y], ...] 全局坐标点列；失败返回 None。"""
    x, y, w, h = comp[:4]
    sub = mask[y:y + h, x:x + w]
    if sub.size == 0:
        return None
    cnts, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    poly = cv2.approxPolyDP(max(cnts, key=cv2.contourArea), epsilon, True)
    ox, oy = offset[0] + x, offset[1] + y
    return [[int(px + ox), int(py + oy)] for px, py in poly[:, 0]]


def rect_contains(r, px, py):
    x, y, w, h = r
    return x <= px <= x + w and y <= py <= y + h


def inside_comps(comps, px, py, pad=0):
    """点是否落在任一连通域（外扩 pad）内"""
    return any(rect_contains((c[0] - pad, c[1] - pad, c[2] + 2 * pad,
                              c[3] + 2 * pad), px, py) for c in comps)


def x_overlap(a, b):
    ax0, ax1 = a[0], a[0] + a[2]
    bx0, bx1 = b[0], b[0] + b[2]
    return max(0, min(ax1, bx1) - max(ax0, bx0))


def y_overlap(a, b):
    ay0, ay1 = a[1], a[1] + a[3]
    by0, by1 = b[1], b[1] + b[3]
    return max(0, min(ay1, by1) - max(ay0, by0))


def estimate_bg(gray_img, y0, y1):
    """区域背景灰度（众数）。步长 2 采样：众数对采样鲁棒，速度 4x。"""
    region = gray_img[y0:y1:2, ::2].ravel()
    vals, counts = np.unique(region, return_counts=True)
    return int(vals[np.argmax(counts)])


def merge_vertical(comps, max_gap=45, xov_ratio=0.6):
    """把水平对齐、垂直间距小的连通域合并（照片被横向暗带切成几段的情况）。
    自 v1 _merge_vertical 零改动平移。"""
    comps = [list(c) for c in comps]
    merged = []
    used = [False] * len(comps)
    for i, a in enumerate(comps):
        if used[i]:
            continue
        cur = a
        for j in range(i + 1, len(comps)):
            if used[j]:
                continue
            b = comps[j]
            gap = b[1] - (cur[1] + cur[3])
            if -20 <= gap <= max_gap and \
                    x_overlap(cur, b) >= min(cur[2], b[2]) * xov_ratio:
                x0, y0 = min(cur[0], b[0]), min(cur[1], b[1])
                x1 = max(cur[0] + cur[2], b[0] + b[2])
                y1 = max(cur[1] + cur[3], b[1] + b[3])
                cur = [x0, y0, x1 - x0, y1 - y0, cur[4] + b[4]]
                used[j] = True
        used[i] = True
        merged.append(tuple(cur))
    return merged
