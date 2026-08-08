# -*- coding: utf-8 -*-
"""plus_panel.py — 聊天页加号扩展面板：卡片网格检测 + 翻页/点按封装（action 层）。

移植自旧仓库 src/v2/plus_panel.py，适配新包路径
（img_utils / ocr_engine 在 ..perception，Rect 在 ..device.random_touch）。

- 网格：列中心 x=166/414/662/910（间距 248），行中心 y=1767/2022（间距 255），
  卡片 151x151 均匀圆角灰块（比背景亮 10~60 灰阶的连通域），标签在卡片正下方 ~30px。
- 页码判定只看卡片数量：8 项=第一页，4 项=第二页（不看分页圆点）。
- 功能清单（8.0.76 实测）：
    第一页 相册/拍摄/语音通话/位置/红包/礼物/转账/语音输入
    第二页 收藏/个人名片/文件/音乐

识别层 detect_plus_panel 纯离线（img + ocr_items 输入，不碰设备）。
动作层 plus_panel_items / plus_panel_next_page / plus_panel_prev_page /
plus_panel_tap 接受 dev 兼容对象（DeviceCtl 接口子集：
capture_bytes() / tap_rect(rect) / swipe_zone(zone, direction=...) / wait_random(a,b)），
只做封装，自身不连手机。
"""

import logging

import cv2
import numpy as np

from ..device.random_touch import Rect
from ..perception.img_utils import comps_from_mask, estimate_bg
from ..perception.ocr_engine import run_ocr

log = logging.getLogger("action.plus_panel")

# ------------------------------------------------------------------ 网格常量（2026-08-06 实测）
COL_X = (166, 414, 662, 910)      # 列中心 x，间距 248
ROW_Y = (1767, 2022)              # 行中心 y，间距 255
CARD_SIZE = 151                   # 卡片边长（均匀圆角灰块）
GRID_TOL = 40                     # 卡片中心对网格槽位的容差

SCAN_Y0, SCAN_Y1 = 1500, 2130     # 卡片检测扫描区
CARD_BG_LO, CARD_BG_HI = 10, 60   # 卡片灰度 = 背景 +10 ~ +60
LABEL_BAND_LO, LABEL_BAND_HI = 5, 80   # 标签带：卡片底边往下 5~80px
LABEL_X_TOL = 95                  # 标签 cx 与卡片中心 x 的容差

PANEL_ZONE = Rect(0, 1500, 1080, 600)   # 面板翻页滑动区（y1500~2100）

PAGE1_ITEMS = ["相册", "拍摄", "语音通话", "位置", "红包", "礼物", "转账", "语音输入"]
PAGE2_ITEMS = ["收藏", "个人名片", "文件", "音乐"]
EXPECTED_ITEMS = {1: PAGE1_ITEMS, 2: PAGE2_ITEMS}


def _norm_label(s):
    return "".join(str(s or "").split())


# ------------------------------------------------------------------ 识别层
def _find_cards(gray):
    """面板扫描区找 151x151 均匀灰块卡片（比背景亮 10~60 灰阶的连通域），
    按网格槽位验证后返回 [(cx, cy, x, y, w, h), ...]，行主序。"""
    bg = estimate_bg(gray, SCAN_Y0, SCAN_Y1)
    region = gray[SCAN_Y0:SCAN_Y1, :]
    mask = ((region >= bg + CARD_BG_LO) &
            (region <= bg + CARD_BG_HI)).astype(np.uint8)
    comps = comps_from_mask(mask, min_area=int(CARD_SIZE * CARD_SIZE * 0.5),
                            close_ksize=(15, 15), min_w=120, min_h=120)
    cards = []
    for (x, y, w, h, area) in comps:
        if not (120 <= w <= 185 and 120 <= h <= 185):
            continue                                  # 输入栏长条等非方卡
        if area < 0.75 * w * h:
            continue                                  # 均匀灰块填充率（图标洞被闭运算填平）
        cx, cy = x + w / 2.0, SCAN_Y0 + y + h / 2.0
        if min(abs(cx - gx) for gx in COL_X) > GRID_TOL:
            continue                                  # 中心必须落在网格槽位上
        if min(abs(cy - gy) for gy in ROW_Y) > GRID_TOL:
            continue
        cards.append((cx, cy, x, SCAN_Y0 + y, w, h))
    # 行主序：按 cy 聚行（容差半卡），行内按 cx 排序
    cards.sort(key=lambda c: (round(c[1] / CARD_SIZE), c[0]))
    return cards


def _card_label(ocr_items, cx, bottom):
    """卡片正下方标签带内的 OCR 文字（按 x 序拼接）。"""
    hits = [it for it in ocr_items
            if bottom + LABEL_BAND_LO <= it["cy"] <= bottom + LABEL_BAND_HI
            and abs(it["cx"] - cx) <= LABEL_X_TOL and it["text"]]
    hits.sort(key=lambda it: it["cx"])
    return "".join(it["text"] for it in hits)


def detect_plus_panel(img, ocr_items):
    """检测加号面板是否展开，返回 {'page': 1|2, 'items': [{'label', 'center'}]} 或 None。

    img: BGR 或灰度整图；ocr_items: run_ocr 输出（需含 cx/cy/text）。
    页码只看卡片数量：>4 → 第一页（8 项），否则第二页（4 项），不看分页圆点。
    检测到的页与功能清单不符时仅记 warning（OCR 错字不否决检测结果）。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    cards = _find_cards(gray)
    if not cards:
        return None
    page = 1 if len(cards) > 4 else 2
    items = []
    for cx, cy, x, y, w, h in cards:
        items.append({
            "label": _card_label(ocr_items, cx, y + h),
            "center": (int(round(cx)), int(round(cy))),
        })
    labels = [it["label"] for it in items]
    if labels != EXPECTED_ITEMS[page]:
        log.warning("plus panel page%d labels mismatch: %s (expect %s)",
                    page, labels, EXPECTED_ITEMS[page])
    return {"page": page, "items": items}


# ------------------------------------------------------------------ 动作层（dev 兼容对象封装）
def _snap_detect(dev):
    """截图 -> OCR -> 面板检测，返回 detect_plus_panel 结果或 None。"""
    img = dev.capture_bytes()
    return detect_plus_panel(img, run_ocr(img))


def plus_panel_items(dev):
    """截图识别当前页功能项 -> {'success', 'page', 'items'}（失败带 error）。"""
    panel = _snap_detect(dev)
    if panel is None:
        return {"success": False, "page": None, "items": [],
                "error": "未检测到加号面板（面板未展开？）"}
    return {"success": True, **panel}


def _flip_page(dev, direction):
    """面板区内横滑翻页，滑后重读卡片网格。"""
    dev.swipe_zone(PANEL_ZONE, direction=direction,
                   length_ratio=(0.55, 0.75), diag_ratio=0.10)
    dev.wait_random(600, 1000)
    panel = _snap_detect(dev)
    if panel is None:
        return {"success": False, "page": None, "items": [],
                "error": f"翻页（{direction}）后未检测到加号面板"}
    return {"success": True, **panel}


def plus_panel_next_page(dev):
    """翻到下一页（面板区左滑），返回翻页后的面板识别结果。"""
    return _flip_page(dev, "left")


def plus_panel_prev_page(dev):
    """翻到上一页（面板区右滑），返回翻页后的面板识别结果。"""
    return _flip_page(dev, "right")


def _find_item(panel, target):
    for it in panel["items"]:
        lab = _norm_label(it["label"])
        if lab == target or (target and (target in lab or lab in target)):
            return it
    return None


def plus_panel_tap(dev, label):
    """按标签点卡片中心。当前页找不到则按页码翻一次页再找，仍找不到返回失败。"""
    target = _norm_label(label)
    panel = _snap_detect(dev)
    if panel is None:
        return {"success": False, "label": label, "tap": None,
                "error": "未检测到加号面板（面板未展开？）"}
    hit = _find_item(panel, target)
    if hit is None:
        flip = plus_panel_next_page if panel["page"] == 1 else plus_panel_prev_page
        panel2 = flip(dev)
        if panel2["success"]:
            hit = _find_item(panel2, target)
            panel = panel2
    if hit is None:
        return {"success": False, "label": label, "tap": None,
                "page": panel.get("page"),
                "error": f"加号面板两页都找不到功能项：{label}"}
    cx, cy = hit["center"]
    half = CARD_SIZE // 2
    pt = dev.tap_rect(Rect(cx - half, cy - half, CARD_SIZE, CARD_SIZE))
    log.info("plus_panel_tap(%r) -> page%d %r at %s", label, panel["page"],
             hit["label"], pt)
    return {"success": True, "page": panel["page"], "label": hit["label"], "tap": pt}
