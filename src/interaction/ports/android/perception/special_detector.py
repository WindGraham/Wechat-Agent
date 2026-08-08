#!/usr/bin/env python3
"""special_detector.py - v2 特殊元素检测。

检测四类非文字可操作元素：
- avatar:  圆形/圆角方形头像（聊天页左右列、首页/通讯录左列）
- qr_code: 二维码/二维码名片区域（大而方正、高对比、密集黑白纹理）
- badge:   红点角标 / 数字未读标记
- switch:  设置页开关（绿色=on / 灰色=off 的圆角长条）

返回元素统一遵循 V2_PERCEPTION_ARCH.md schema。
"""

import cv2
import numpy as np

from . import layout_consts as LC
from .img_utils import comps_from_mask, estimate_bg
from .ocr_engine import ocr_badge_digit


# ---------------------------------------------------------------- 工具

def _element(id_, type_, name, x, y, w, h,
             label=None, content=None, state=None,
             confidence=0.6, verified=False,
             actions=None, source="geometry"):
    """构造统一元素 dict。"""
    return {
        "id": id_,
        "type": type_,
        "name": name,
        "label": label,
        "content": content,
        "state": state,
        "position": {"x": int(x), "y": int(y), "w": int(w), "h": int(h)},
        "confidence": float(confidence),
        "verified": bool(verified),
        "actions": list(actions or []),
        "source": source,
    }


def _nonbg_mask(img, gray=None, hsv=None, bg=None):
    """非背景掩膜：亮度明显高于背景或饱和度足够（排除纯黑背景）。"""
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    if bg is None:
        bg = estimate_bg(gray, LC.CONTENT_Y0, LC.TAB_BAR_Y0)
    intense = img.max(axis=2)
    return ((intense > bg + 11) | (hsv[:, :, 1] > 60)).astype(np.uint8)


def _iou(a, b):
    """两个元素 position dict 的 IoU。"""
    ax0, ay0, aw, ah = a["x"], a["y"], a["w"], a["h"]
    bx0, by0, bw, bh = b["x"], b["y"], b["w"], b["h"]
    inter_w = max(0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    inter_h = max(0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = inter_w * inter_h
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


# ---------------------------------------------------------------- Avatars

_AVATAR_CONF = {
    "chat": {
        "cols": [("L", LC.AVATAR_COL_L), ("R", LC.AVATAR_COL_R)],
        "min_w": LC.AVATAR_MIN_W,
        "max_w": LC.AVATAR_MAX_W,
        "min_h": LC.AVATAR_MIN_H,
        "max_h": LC.AVATAR_MAX_H,
        "min_area": LC.AVATAR_MIN_AREA,
        "aspect_lo": LC.AVATAR_ASPECT_LO,
        "aspect_hi": LC.AVATAR_ASPECT_HI,
        "y0": LC.CONTENT_Y0,
        "y1": LC.INPUT_BAR_Y0,
    },
    "home": {
        "cols": [("L", (LC.HOME_AVATAR_X0, LC.HOME_AVATAR_X1))],
        "min_w": LC.HOME_AVATAR_MIN_W,
        "max_w": 160,
        "min_h": LC.HOME_AVATAR_MIN_H,
        "max_h": 170,
        "min_area": LC.HOME_AVATAR_MIN_AREA,
        "aspect_lo": 0.7,
        "aspect_hi": 1.35,
        "y0": LC.CONTENT_Y0,
        "y1": LC.TAB_BAR_Y0,
    },
    "contacts": {
        "cols": [("L", (20, 190))],
        "min_w": 60,
        "max_w": 170,
        "min_h": 60,
        "max_h": 170,
        "min_area": 3600,
        "aspect_lo": 0.65,
        "aspect_hi": 1.35,
        "y0": LC.CONTENT_Y0,
        "y1": LC.TAB_BAR_Y0,
    },
}


def _color_variance_ok(img, x, y, w, h, min_var=180.0):
    """头像通常有非均匀颜色/纹理；纯色块更可能是功能图标，但别太严格。"""
    crop = img[y:y + h, x:x + w]
    if crop.size == 0:
        return False
    # 用灰度方差 + 色相标准差综合判断
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    var_g = float(gray.var())
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # 排除大面积低饱和（纯白/灰图标）
    sat_mean = float(hsv[:, :, 1].mean())
    return var_g > min_var or sat_mean > 35.0


def _detect_avatars_in_columns(img, nonbg, cfg, id_prefix):
    """在指定列中检测头像连通域。"""
    out = []
    idx = 0
    for side, (x0, x1) in cfg["cols"]:
        col = np.zeros_like(nonbg)
        col[cfg["y0"]:cfg["y1"], x0:x1] = nonbg[cfg["y0"]:cfg["y1"], x0:x1]
        # 轻微闭运算：把头像内部暗区连成一体
        for x, y, w, h, area in comps_from_mask(
                col, min_area=cfg["min_area"], close_ksize=5,
                min_w=cfg["min_w"], min_h=cfg["min_h"]):
            if not (cfg["min_w"] <= w <= cfg["max_w"]
                    and cfg["min_h"] <= h <= cfg["max_h"]
                    and cfg["aspect_lo"] < w / max(h, 1) < cfg["aspect_hi"]):
                continue
            # 颜色方差过滤：去掉过纯色块（如橙色"新的朋友"图标仍保留，
            # 因其内部白色图案方差足够；纯白灰图标会被过滤）
            if not _color_variance_ok(img, x0 + x, y, w, h, min_var=120.0):
                continue
            idx += 1
            out.append(_element(
                f"{id_prefix}_{side}_{idx}", "avatar",
                f"头像({side})", x0 + x, y, w, h,
                confidence=round(min(1.0, area / 25000), 3),
                verified=True,
                actions=["tap", "long_press"],
                source="geometry",
            ))
    out.sort(key=lambda e: e["position"]["y"])
    return out


def detect_avatars(img, gray=None, hsv=None):
    """检测头像元素。

    按页面类型在多个候选列中查找：
    - 聊天页：LC.AVATAR_COL_L / LC.AVATAR_COL_R
    - 首页：  LC.HOME_AVATAR_X0..X1
    - 通讯录/我/通用列表页：左侧 20~190 列

    返回 list[element]，每个 type="avatar"，actions=["tap", "long_press"]。
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    nonbg = _nonbg_mask(img, gray=gray, hsv=hsv)

    results = []
    # 通用左列（覆盖通讯录/我/发现等列表页），同时过滤掉已被聊天列检出的重复
    contacts = _detect_avatars_in_columns(
        img, nonbg, _AVATAR_CONF["contacts"], "avatar")

    # 若底部有 Tab 栏，按首页参数再检一次（首页头像比通讯录略大）
    chat = _detect_avatars_in_columns(
        img, nonbg, _AVATAR_CONF["chat"], "avatar_chat")
    home = _detect_avatars_in_columns(
        img, nonbg, _AVATAR_CONF["home"], "avatar_home")

    # 合并：同位置 (IoU>0.5) 只保留一个
    all_cands = contacts + home + chat
    used = [False] * len(all_cands)
    for i, a in enumerate(all_cands):
        if used[i]:
            continue
        for j in range(i + 1, len(all_cands)):
            if used[j]:
                continue
            if _iou(a["position"], all_cands[j]["position"]) > 0.5:
                used[j] = True
        results.append(a)

    # 重排 id 保证页面内唯一
    results.sort(key=lambda e: (e["position"]["y"], e["position"]["x"]))
    for i, e in enumerate(results, 1):
        e["id"] = f"avatar_{i}"
    return results


# ---------------------------------------------------------------- QR codes

def detect_qr_regions(img, gray=None):
    """检测二维码/二维码名片区域。

    不依赖解码，而是用几何+纹理启发式：
    - 在灰度图上寻找大而方正、高对比、内部黑白格密集的区域；
    - 排除细长条、过小/过大区域，并避免与左侧头像列重叠。

    返回 list[element]，type="qr_code"，actions=["tap"]。
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 自适应阈值得到高对比二值图
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 41, 10)

    # 边缘密度图：Scharr 梯度
    gx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    grad = cv2.magnitude(gx, gy)
    edge_mask = (grad > 60).astype(np.uint8)

    # 预检测头像列，避免把高纹理头像误判为二维码
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    avatar_pos = [e["position"] for e in detect_avatars(img, gray=gray, hsv=hsv)]

    candidates = []
    # 从二值图找连通域，再用边缘密度/模块数验证
    for x, y, w, h, area in comps_from_mask(
            cv2.bitwise_not(binary), min_area=9000, close_ksize=7,
            min_w=80, min_h=80):
        aspect = w / max(h, 1)
        if not (0.75 <= aspect <= 1.35 and 80 <= w <= 800 and 80 <= h <= 800):
            continue
        pos = {"x": x, "y": y, "w": w, "h": h}
        # 排除与头像列大幅重叠（头像在左侧 x<190）
        if any(_iou(pos, a) > 0.3 for a in avatar_pos):
            continue
        # 边缘密度：二维码内部纹理丰富
        roi_edge = edge_mask[y:y + h, x:x + w]
        density = float(roi_edge.mean())
        if density < 0.04:
            continue
        # 黑白像素比应接近 1:1（二维码特征）
        roi_bin = binary[y:y + h, x:x + w]
        black_ratio = float((roi_bin < 128).mean())
        if not (0.25 <= black_ratio <= 0.75):
            continue
        # 模块数：二维码内部有大量独立小黑块；纯色图标/头像只有少量大块
        roi_inv = cv2.bitwise_not(roi_bin)
        n_modules, _, _, _ = cv2.connectedComponentsWithStats(roi_inv, connectivity=8)
        n_modules -= 1  # 去掉背景
        if n_modules < 30:
            continue
        # 越大越需要更密集的纹理，避免把大块 logo 误判
        if area > 50000 and density < 0.06:
            continue
        candidates.append(_element(
            f"qr_{len(candidates) + 1}", "qr_code", "二维码",
            x, y, w, h,
            confidence=round(min(1.0, density * 10 + 0.3), 3),
            verified=density > 0.08,
            actions=["tap"],
            source="geometry",
        ))

    # 按面积排序，取前若干（通常只有 1 个）
    candidates.sort(key=lambda e: -e["position"]["w"] * e["position"]["h"])
    for i, e in enumerate(candidates, 1):
        e["id"] = f"qr_{i}"
    return candidates


# ---------------------------------------------------------------- Badges

def _red_mask(hsv):
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    return (((h < LC.RED_H_LO) | (h > LC.RED_H_HI))
            & (s > LC.RED_S_MIN) & (v > LC.RED_V_MIN)).astype(np.uint8)


def _circularity(x, y, w, h, area, mask):
    """基于红色掩膜轮廓计算圆度：圆=1，方块≈0.785，细长/不规则更低。"""
    sub = mask[y:y + h, x:x + w]
    cnts, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return 0.0
    peri = cv2.arcLength(max(cnts, key=cv2.contourArea), True)
    return 4 * np.pi * area / (peri * peri) if peri else 0.0


def detect_badges(img, hsv=None):
    """检测红点角标 / 数字未读标记。

    返回 list[element]，type="badge"：
    - 小红点：content=None, state="dot", unread=-1（语义）
    - 数字红圈：content=整数, state="number"
    """
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    red = _red_mask(hsv)
    # 限制在内容区与标题栏（避开状态栏、Tab 栏偶发红色）
    red[:LC.STATUS_BAR_BOTTOM, :] = 0
    red[LC.TAB_BAR_Y0:, :] = 0

    badges = []
    for x, y, w, h, area in comps_from_mask(
            red, min_area=80, close_ksize=5, min_w=8, min_h=8):
        aspect = w / max(h, 1)
        circ = _circularity(x, y, w, h, area, red)
        is_dot = (LC.BADGE_DOT_MIN_SIZE <= w <= LC.BADGE_DOT_MAX_SIZE
                  and LC.BADGE_DOT_MIN_SIZE <= h <= LC.BADGE_DOT_MAX_SIZE
                  and LC.BADGE_DOT_MIN_AREA <= area <= LC.BADGE_DOT_MAX_AREA
                  and 0.7 < aspect < 1.4
                  and circ > 0.55)
        if is_dot:
            badges.append(_element(
                f"badge_{len(badges) + 1}", "badge", "未读红点",
                x, y, w, h,
                content=None,
                state="dot",
                confidence=round(0.7 + 0.2 * circ, 2),
                verified=True,
                actions=[],
                source="geometry",
            ))
            continue

        # 数字红圈：比红点大，但仍较小，宽高比接近 1，且较圆
        if not (25 <= w <= 90 and 25 <= h <= 90 and 0.75 < aspect < 1.5):
            continue
        if circ <= 0.50:
            continue
        # 尝试 OCR 数字
        digit = ocr_badge_digit(img, x, y, w, h)
        badges.append(_element(
            f"badge_{len(badges) + 1}", "badge", "未读数",
            x, y, w, h,
            content=digit,
            state="number",
            confidence=round(0.55 + 0.20 * circ + (0.15 if digit is not None else 0), 2),
            verified=digit is not None,
            actions=[],
            source="geometry",
        ))

    for i, e in enumerate(badges, 1):
        e["id"] = f"badge_{i}"
    return badges


# ---------------------------------------------------------------- Switches

# 开关颜色/形状常量（深色模式）
_SWITCH_GREEN_H_LO, _SWITCH_GREEN_H_HI = 55, 95
_SWITCH_GREEN_S_MIN, _SWITCH_GREEN_V_MIN = 80, 80
_SWITCH_GRAY_V_RANGE = (30, 110)          # off 开关背景灰度范围
_SWITCH_GRAY_S_MAX = 60
_SWITCH_MIN_W, _SWITCH_MAX_W = 80, 170
_SWITCH_MIN_H, _SWITCH_MAX_H = 38, 95
_SWITCH_ASPECT_LO, _SWITCH_ASPECT_HI = 1.4, 3.2
_SWITCH_MIN_AREA, _SWITCH_MAX_AREA = 3000, 11000


def _switch_mask(hsv):
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    green = ((h >= _SWITCH_GREEN_H_LO) & (h <= _SWITCH_GREEN_H_HI)
             & (s > _SWITCH_GREEN_S_MIN) & (v > _SWITCH_GREEN_V_MIN))
    gray = ((s < _SWITCH_GRAY_S_MAX)
            & (v > _SWITCH_GRAY_V_RANGE[0]) & (v < _SWITCH_GRAY_V_RANGE[1]))
    return (green | gray).astype(np.uint8)


def _switch_state(hsv, x, y, w, h):
    """根据区域绿色像素占比判定开关状态。"""
    sub = hsv[y:y + h, x:x + w]
    if sub.size == 0:
        return "off"
    h, s, v = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
    green = ((h >= _SWITCH_GREEN_H_LO) & (h <= _SWITCH_GREEN_H_HI)
             & (s > _SWITCH_GREEN_S_MIN) & (v > _SWITCH_GREEN_V_MIN))
    return "on" if green.mean() > 0.15 else "off"


def detect_switches(img, hsv=None, roi=None):
    """检测设置页开关。

    Parameters
    ----------
    roi: tuple(x0, y0, x1, y1) | None
        限制检测区域；默认内容区（避开标题栏/Tab 栏）。

    返回 list[element]，type="switch"，actions=["toggle"]，state="on"/"off"。
    """
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = _switch_mask(hsv)
    if roi is not None:
        x0, y0, x1, y1 = roi
        tmp = np.zeros_like(mask)
        tmp[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
        mask = tmp
    else:
        # 开关在微信 UI 中一律右对齐；排除顶部搜索框/标题栏、左半区和底部 Tab 区
        mask[:LC.STATUS_BAR_BOTTOM, :] = 0
        mask[LC.TAB_BAR_Y0:, :] = 0
        mask[:LC.CONTENT_Y0 + 140, :] = 0
        mask[:, :int(LC.SCREEN_W * 0.45)] = 0

    # 排除与二维码区域重合的误检（二维码中心绿块/Logo 形状像开关）
    qr_pos = [e["position"] for e in detect_qr_regions(img, gray=cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))]

    switches = []
    for x, y, w, h, area in comps_from_mask(
            mask, min_area=_SWITCH_MIN_AREA, close_ksize=9,
            min_w=_SWITCH_MIN_W, min_h=_SWITCH_MIN_H):
        aspect = w / max(h, 1)
        if not (_SWITCH_MIN_W <= w <= _SWITCH_MAX_W
                and _SWITCH_MIN_H <= h <= _SWITCH_MAX_H
                and _SWITCH_ASPECT_LO <= aspect <= _SWITCH_ASPECT_HI
                and _SWITCH_MIN_AREA <= area <= _SWITCH_MAX_AREA):
            continue
        # 圆角验证：开关两端是半圆，轮廓应近似椭圆/圆角矩形；
        # 白色旋钮会在绿色/灰色开关上形成洞，因此填充后再算一次更稳健。
        bbox_area = w * h
        if bbox_area and area / bbox_area < 0.40:
            continue
        pos = {"x": x, "y": y, "w": w, "h": h}
        cx, cy = x + w / 2, y + h / 2
        # 开关不应落在二维码内部，也不应与二维码大面积重叠
        if any(_iou(pos, q) > 0.3 or
                (q["x"] <= cx <= q["x"] + q["w"] and q["y"] <= cy <= q["y"] + q["h"])
                for q in qr_pos):
            continue
        state = _switch_state(hsv, x, y, w, h)
        switches.append(_element(
            f"switch_{len(switches) + 1}", "switch", "开关",
            x, y, w, h,
            state=state,
            confidence=0.85,
            verified=True,
            actions=["toggle"],
            source="geometry",
        ))

    for i, e in enumerate(switches, 1):
        e["id"] = f"switch_{i}"
    return switches


# ----------------------------------------------------------------  sanity check

def _summarize(elements):
    return [
        (e["id"], e["type"], e.get("state"), e.get("content"),
         e["position"], round(e["confidence"], 2))
        for e in elements
    ]


if __name__ == "__main__":
    import sys
    import json

    base = "/media/data_old/wechat-agent/samples/ui_inventory"
    samples = {
        "avatars_badges": f"{base}/05_contacts/contacts_main_v2.png",
        "switches": f"{base}/10_settings/settings_account_security.png",
        "qr": f"{base}/14_other/add_friend.png",
    }

    results = {}
    for name, path in samples.items():
        img = cv2.imread(path)
        if img is None:
            results[name] = {"error": f"cannot read {path}"}
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        if name == "avatars_badges":
            avatars = detect_avatars(img, gray=gray, hsv=hsv)
            badges = detect_badges(img, hsv=hsv)
            results[name] = {
                "avatars": _summarize(avatars),
                "badges": _summarize(badges),
            }
        elif name == "switches":
            sw = detect_switches(img, hsv=hsv)
            results[name] = {"switches": _summarize(sw)}
        else:
            qr = detect_qr_regions(img, gray=gray)
            results[name] = {"qr_codes": _summarize(qr)}

    print(json.dumps(results, ensure_ascii=False, indent=2))
