#!/usr/bin/env python3
"""avatar_detector.py - v2 头像区域验证检测器（WP1）。

需求来源：docs/PERCEPTION_DISCUSSION_20260805.md §2.7（分层可靠性模型）、
§2.8（死命令：全页面头像必须谨慎识别）。

与 special_detector.detect_avatars（开放式检测，保留不动）的区别：
本模块做**锚定验证**——

- 行锚定（verify_avatars_in_rows）：行段已知（L0 分割线）时，每个行的
  头像列里**必然**有一个头像。检测从"找不找得到"变成"在行段头像区里
  验证/定位"：首选现有阈值（非背景 bg+11 连通域），检不到逐级放宽
  （放宽阈值 -> 局部对比：梯度能量 + 方差），仍检不到产出
  low_confidence=True 的占位头像（位置=行段内规格先验中心），
  **不允许静默缺失**。
- 列锚定（verify_chat_avatars）：聊天页左右两列（LC.AVATAR_COL_L/R）
  扫描全部头像，暗头像用局部对比补救；宁可误检（交给形状/位置窗口
  过滤）不可漏检。

元素 schema 与 special_detector 一致（id/type/position/confidence/
verified/actions/source），另加 side / top / center / low_confidence。
"""

import cv2
import numpy as np

from . import layout_consts as LC
from .img_utils import comps_from_mask, estimate_bg


# ---------------------------------------------------------------- 规格先验（§2.8）
SPEC_HOME_AVATAR = {
    "prior_w": 130, "prior_h": 130,          # 首页会话 ~130x130
    "min_w": 70, "max_w": 170,
    "min_h": 80, "max_h": 180,
    "min_area": 4500,
    "aspect_lo": 0.55, "aspect_hi": 1.6,
    "side": "L",
}
SPEC_CHAT_AVATAR = {
    "prior_w": 108, "prior_h": 108,          # 聊天页 ~108x108
    "min_w": LC.AVATAR_MIN_W, "max_w": LC.AVATAR_MAX_W,
    "min_h": LC.AVATAR_MIN_H, "max_h": LC.AVATAR_MAX_H,
    "min_area": LC.AVATAR_MIN_AREA,
    "aspect_lo": LC.AVATAR_ASPECT_LO, "aspect_hi": LC.AVATAR_ASPECT_HI,
    "side": None,                             # 由列决定 L/R
}
SPEC_CONTACTS_AVATAR = {
    "prior_w": 110, "prior_h": 110,          # 通讯录 ~110x110
    "min_w": 60, "max_w": 170,
    "min_h": 60, "max_h": 170,
    "min_area": 3600,
    "aspect_lo": 0.55, "aspect_hi": 1.6,
    "side": "L",
}

# 置信度分级：逐级放宽，越晚命中的越不可信
_CONF_PRIMARY = 0.90      # 现有阈值（bg+11 非背景连通域）
_CONF_RELAXED = 0.70      # 放宽阈值（bg+5 + 大核闭运算）
_CONF_GRADIENT = 0.55     # 局部对比（梯度能量 + 方差）
_CONF_PLACEHOLDER = 0.20  # 占位（规格先验中心）


# ---------------------------------------------------------------- 元素构造
def _avatar_element(id_, side, x, y, w, h, confidence,
                    verified=True, low_confidence=False, row=None):
    """构造头像元素（schema 与 special_detector._element 一致 + 锚定字段）。"""
    el = {
        "id": id_,
        "type": "avatar",
        "name": f"头像({side})" if side else "头像",
        "label": None,
        "content": None,
        "state": None,
        "position": {"x": int(x), "y": int(y), "w": int(w), "h": int(h)},
        "confidence": float(confidence),
        "verified": bool(verified),
        "actions": ["tap", "long_press"],
        "source": "geometry",
        "side": side,
        "top": int(y),
        "center": float(y + h / 2),
        "low_confidence": bool(low_confidence),
    }
    if row is not None:
        el["row"] = [int(row[0]), int(row[1])]
    return el


# ---------------------------------------------------------------- 带内候选检测
def _in_windows(x, y, w, h, area, spec, slack=0):
    """形状/位置窗口过滤（误检交给你——§2.8：宁可误检不可漏检）。"""
    return (spec["min_w"] - slack <= w <= spec["max_w"] + slack
            and spec["min_h"] - slack <= h <= spec["max_h"] + slack
            and area >= spec["min_area"] * (0.6 if slack else 1.0)
            and spec["aspect_lo"] < w / max(h, 1) < spec["aspect_hi"])


def _touches_full_span(x, w, band_w):
    """候选横跨整个头像列且贴到裁切边界：几乎一定是被列裁切的气泡/胶囊
    （如"N 条新消息"胶囊、顶部残缺宽泡），不是头像。"""
    return w >= band_w * 0.9 and (x <= 2 or x + w >= band_w - 2)


def _nonbg_candidates(img, gray, hsv, bg, spec, delta, close_ksize,
                      slack=0):
    """非背景连通域候选：亮度高于 bg+delta 或饱和度足够。带内局部坐标。"""
    intense = img.max(axis=2)
    mask = ((intense > bg + delta) | (hsv[:, :, 1] > 60)).astype(np.uint8)
    band_w = img.shape[1]
    out = []
    for x, y, w, h, area in comps_from_mask(
            mask, min_area=int(spec["min_area"] * 0.6),
            close_ksize=close_ksize,
            min_w=spec["min_w"] - slack, min_h=spec["min_h"] - slack):
        if not _in_windows(x, y, w, h, area, spec, slack):
            continue
        if _touches_full_span(x, w, band_w):
            continue
        out.append((x, y, w, h, area))
    return out


def _gradient_candidates(gray, spec, slack=20):
    """局部对比候选（暗头像补救）：Sobel 梯度能量 + 灰度方差双判。

    深色头像在 bg+delta 阈值下整体隐身，但内部纹理/边缘的梯度能量
    显著高于纯背景（背景梯度≈0、方差≈0），用梯度掩膜定位后再用
    窗口方差复核，空背景不会误检。"""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    grad = cv2.magnitude(gx, gy)
    mask = (grad > 40).astype(np.uint8)
    band_w = gray.shape[1]
    out = []
    for x, y, w, h, area in comps_from_mask(
            mask, min_area=1500, close_ksize=9,
            min_w=spec["min_w"] - slack, min_h=spec["min_h"] - slack):
        if not _in_windows(x, y, w, h, area, spec, slack):
            continue
        if _touches_full_span(x, w, band_w):
            continue
        # 方差复核：拒绝纯噪声/纯色块
        win = gray[y:y + h, x:x + w]
        if win.size == 0 or float(win.std()) < 5.0:
            continue
        out.append((x, y, w, h, area))
    return out


def _dedupe(cands, min_dist=60):
    """按中心距离去重（先出现者优先 = 阈值级数更可靠）。"""
    kept = []
    for x, y, w, h, area in cands:
        cx, cy = x + w / 2, y + h / 2
        if any(abs(cx - (k[0] + k[2] / 2)) < min_dist
               and abs(cy - (k[1] + k[3] / 2)) < min_dist for k in kept):
            continue
        kept.append((x, y, w, h, area))
    return kept


def _detect_candidates(img, gray, hsv, bg, spec):
    """带内逐级放宽检测，返回 [(x,y,w,h,area,conf), ...]（带内局部坐标）。"""
    staged = []
    for delta, close_ksize, slack, conf in (
            (11, 5, 0, _CONF_PRIMARY),
            (5, 13, 10, _CONF_RELAXED)):
        found = _nonbg_candidates(img, gray, hsv, bg, spec,
                                  delta=delta, close_ksize=close_ksize,
                                  slack=slack)
        staged.append((found, conf))
    staged.append((_gradient_candidates(gray, spec), _CONF_GRADIENT))

    out = []
    for found, conf in staged:
        found = _dedupe([c for c in found
                         if not any(abs(c[0] + c[2] / 2 - (o[0] + o[2] / 2)) < 60
                                    and abs(c[1] + c[3] / 2 - (o[1] + o[3] / 2)) < 60
                                    for o in out)])
        out.extend((x, y, w, h, area, conf) for x, y, w, h, area in found)
    out.sort(key=lambda c: c[1])
    return out


# ---------------------------------------------------------------- 行锚定验证
def verify_avatars_in_rows(img, gray=None, hsv=None, rows=None,
                           col_roi=None, spec=None):
    """行锚定头像验证：每行必须产出一个头像元素，不允许静默缺失。

    Parameters
    ----------
    rows: list[(y0, y1)]  行段（L0 分割线产出）
    col_roi: (x0, x1)     头像列 ROI
    spec: 规格先验 dict（SPEC_HOME_AVATAR / SPEC_CONTACTS_AVATAR / ...）

    返回 list[element]，与 rows 等长、按行序排列。
    检不到真实头像的行产出 low_confidence=True 的占位元素
    （位置 = 行段内规格先验中心）。
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    rows = rows or []
    spec = spec or SPEC_HOME_AVATAR
    x0, x1 = col_roi
    side = spec.get("side") or "L"

    elements = []
    for i, (y0, y1) in enumerate(rows, 1):
        band_i = img[y0:y1, x0:x1]
        band_g = gray[y0:y1, x0:x1]
        band_h = hsv[y0:y1, x0:x1]
        if band_i.size == 0:
            continue
        # 局部背景：行内文字区（头像列右侧）的灰度众数，
        # 对置顶条目（bg+11）等局部底色变化鲁棒
        ref = gray[y0:y1, min(x1 + 210, gray.shape[1]):gray.shape[1]]
        bg = estimate_bg(ref, 0, ref.shape[0]) if ref.size else \
            estimate_bg(gray, LC.CONTENT_Y0, 2000)

        cands = _detect_candidates(band_i, band_g, band_h, bg, spec)
        if cands:
            # 每行取一个最可靠的（置信度最高，其次面积最大）
            x, y, w, h, area, conf = max(
                cands, key=lambda c: (c[5], c[4]))
            elements.append(_avatar_element(
                f"avatar_row_{i}", side, x0 + x, y0 + y, w, h,
                confidence=conf, verified=True, row=(y0, y1)))
            continue

        # 占位：规格先验中心（行段中心 × 列中心），low_confidence 上抛
        pw = min(spec["prior_w"], x1 - x0)
        ph = min(spec["prior_h"], y1 - y0)
        px = x0 + max(0, (x1 - x0 - pw) // 2)
        py = y0 + max(0, (y1 - y0 - ph) // 2)
        elements.append(_avatar_element(
            f"avatar_row_{i}", side, px, py, pw, ph,
            confidence=_CONF_PLACEHOLDER, verified=False,
            low_confidence=True, row=(y0, y1)))
    return elements


# ---------------------------------------------------------------- 聊天页列锚定
def verify_chat_avatars(img, gray=None, hsv=None):
    """聊天页左右两列头像检测（§2.8 泡锚定的列扫描部分）。

    每列在 CONTENT_Y0~INPUT_BAR_Y0 内逐级放宽扫描；暗头像用局部对比
    补救。宁可误检（交形状/位置窗口过滤）不可漏检。

    返回 list[element]，按 top 排序，带 side("L"/"R")/top/center/confidence。
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    spec = dict(SPEC_CHAT_AVATAR)

    elements = []
    counts = {"L": 0, "R": 0}
    for side, (x0, x1) in (("L", LC.AVATAR_COL_L), ("R", LC.AVATAR_COL_R)):
        y0, y1 = LC.CONTENT_Y0, LC.INPUT_BAR_Y0
        band_i = img[y0:y1, x0:x1]
        band_g = gray[y0:y1, x0:x1]
        band_h = hsv[y0:y1, x0:x1]
        # 整列背景：头像只占列内小部分面积，众数=聊天页背景
        bg = estimate_bg(band_g, 0, band_g.shape[0])
        spec["side"] = side
        for x, y, w, h, area, conf in _detect_candidates(
                band_i, band_g, band_h, bg, spec):
            counts[side] += 1
            elements.append(_avatar_element(
                f"avatar_chat_{side}_{counts[side]}", side,
                x0 + x, y0 + y, w, h, confidence=conf, verified=True))

    elements.sort(key=lambda e: (e["top"], e["side"]))
    # 重排 id 保证页面内唯一且有序
    for i, e in enumerate(elements, 1):
        e["id"] = f"avatar_chat_{i}"
    return elements


# ---------------------------------------------------------------- sanity check
if __name__ == "__main__":
    import json
    import sys

    base = "/media/data_old/wechat-agent/samples/ui_inventory"
    for path, mode in (
            (f"{base}/03_chat/chat_group_jiaoliu.png", "chat"),
            (f"{base}/03_chat/chat_group_leisure.png", "chat")):
        img = cv2.imread(path)
        avatars = verify_chat_avatars(img)
        print(path.split("/")[-1], json.dumps([
            (e["side"], e["top"], e["position"], round(e["confidence"], 2))
            for e in avatars], ensure_ascii=False, indent=1))
