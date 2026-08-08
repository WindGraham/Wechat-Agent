#!/usr/bin/env python3
"""dialog_parser.py - v2 弹窗/对话框/确认面板解析。

适配微信 8.0.76 深色模式下的居中对话框：
- 删除确认、清空确认、系统提示等单按钮/双按钮面板
- 长按会话弹出的菜单（灰色垂直列表）

输出元素：
- type="text" 的 title / message
- type="dialog_button" 的底部按钮
"""

import cv2
import numpy as np

from . import layout_consts as LC
from .img_utils import comps_from_mask, rect_contains, estimate_bg

# ---------------------------------------------------------------- 弹窗关键词
_MODAL_KEYWORDS = (
    "将清空", "删除", "确认", "我知道了",
    "取消", "确定", "是否", "提示",
)

# 常见按钮文案（按优先级，先匹配的可作为按钮中心）
_BUTTON_LABELS = ("我知道了", "确定", "确认", "取消", "是", "否", "删除", "清空")

# 几何/颜色阈值（仅适配 1080x2340 深色模式）
_CENTER_X_TOL = 0.12          # 对话框中心与屏幕中心水平偏差容忍（相对屏幕宽）
_CENTER_Y_TOL = 0.35          # 对话框中心与屏幕中心垂直偏差容忍（相对屏幕高）
_PANEL_Y_MIN_RATIO = 0.22     # 对话框顶部不得低于屏幕高度比例
_PANEL_Y_MAX_RATIO = 0.88     # 对话框底部不得高于屏幕高度比例
_PANEL_H_MAX_RATIO = 0.65     # 对话框高度不超过屏幕高度比例
_MIN_PANEL_WIDTH = 400        # 对话框最小宽度
_MIN_PANEL_HEIGHT = 180       # 对话框最小高度
_MIN_PANEL_AREA = 50000       # 对话框最小面积
_PANEL_S_MAX = 30             # 对话框区域平均饱和度上限（深色模式灰面板）
_OVERLAY_AREA_RATIO = 0.55    # 低饱和度遮罩覆盖屏幕比例阈值


def _panel_by_contrast(gray, hsv):
    """基于与背景对比度找居中矩形面板。返回 (x,y,w,h) 或 None。"""
    h, w = gray.shape
    bg = estimate_bg(gray, 0, h)

    # 与背景差异显著的像素
    diff = cv2.absdiff(gray, np.uint8([bg]))
    _, mask = cv2.threshold(diff, 12, 255, cv2.THRESH_BINARY)
    # 闭运算合并面板内部文字/按钮孔洞
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (41, 41))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)

    cx, cy = w / 2, h / 2
    for x, y, cw, ch, area in comps_from_mask(
            mask, min_area=_MIN_PANEL_AREA, min_w=_MIN_PANEL_WIDTH,
            min_h=_MIN_PANEL_HEIGHT):
        ccx, ccy = x + cw / 2, y + ch / 2
        if abs(ccx - cx) > w * _CENTER_X_TOL:
            continue
        if abs(ccy - cy) > h * _CENTER_Y_TOL:
            continue
        if y < h * _PANEL_Y_MIN_RATIO or y + ch > h * _PANEL_Y_MAX_RATIO:
            continue
        if ch > h * _PANEL_H_MAX_RATIO:
            continue
        s_mean = float(hsv[y:y + ch, x:x + cw, 1].mean())
        if s_mean < _PANEL_S_MAX:
            return (x, y, cw, ch)
    return None


def _panel_by_brightness(gray, hsv):
    """兜底：找比背景略亮的居中灰面板（适应文字较少、对比度不极端的弹窗）。"""
    h, w = gray.shape
    bg = estimate_bg(gray, 0, h)

    panel = ((gray > bg + 5) & (gray < bg + 100)).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (41, 41))
    panel = cv2.morphologyEx(panel, cv2.MORPH_CLOSE, k)

    cx, cy = w / 2, h / 2
    for x, y, cw, ch, area in comps_from_mask(
            panel, min_area=_MIN_PANEL_AREA, min_w=_MIN_PANEL_WIDTH,
            min_h=_MIN_PANEL_HEIGHT):
        ccx, ccy = x + cw / 2, y + ch / 2
        if abs(ccx - cx) > w * _CENTER_X_TOL:
            continue
        if abs(ccy - cy) > h * _CENTER_Y_TOL:
            continue
        if y < h * _PANEL_Y_MIN_RATIO or y + ch > h * _PANEL_Y_MAX_RATIO:
            continue
        if ch > h * _PANEL_H_MAX_RATIO:
            continue
        s_mean = float(hsv[y:y + ch, x:x + cw, 1].mean())
        if s_mean < _PANEL_S_MAX:
            return (x, y, cw, ch)
    return None


def _bbox_from_keywords(ocr_items):
    """OCR 命中弹窗关键词时，用所有命中项及附近文本的 bbox 估算对话框区域。"""
    hits = [it for it in (ocr_items or [])
            if any(k in it["text"] for k in _MODAL_KEYWORDS)]
    if not hits:
        return None
    xs = [it["box"][0] for it in hits] + [it["box"][2] for it in hits]
    ys = [it["box"][1] for it in hits] + [it["box"][3] for it in hits]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    # 外扩，让后续分割有余量
    pad_x = int((x1 - x0) * 0.15) + 40
    pad_y = int((y1 - y0) * 0.15) + 60
    x0 = int(max(0, x0 - pad_x))
    y0 = int(max(LC.STATUS_BAR_BOTTOM, y0 - pad_y))
    x1 = int(min(LC.SCREEN_W, x1 + pad_x))
    y1 = int(min(LC.SCREEN_H, y1 + pad_y))
    return (x0, y0, x1 - x0, y1 - y0)


def _overlay_around_panel(hsv, gray, region):
    """对话框面板外是否存在大面积低饱和度半透明遮罩（真弹窗的暗化背景）。"""
    h, w = gray.shape
    rx, ry, rw, rh = region
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]
    # 遮罩像素：极低饱和度且较暗
    overlay = ((s < 35) & (v < 85)).astype(np.uint8)
    # 挖掉对话框面板区域
    overlay[ry:ry + rh, rx:rx + rw] = 0
    # 小闭运算只合并细碎噪声，保留面板边缘
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    overlay = cv2.morphologyEx(overlay, cv2.MORPH_CLOSE, k)
    total_outside = h * w - rw * rh
    if total_outside <= 0:
        return False
    covered = np.count_nonzero(overlay)
    return covered / total_outside >= _OVERLAY_AREA_RATIO


def detect_dialog(img, gray=None, hsv=None, ocr_items=None):
    """检测当前截图是否被弹窗/对话框覆盖。返回 bool。

    启发式（任一命中即认为是弹窗）：
    1. OCR：命中模态关键词（最可靠）；
    2. 图像：找到居中的低饱和度灰面板，且底部存在按钮色块。

    注："大面积低饱和度遮罩"在深色模式下与正常页面背景难以区分，
    因此当前实现以面板+按钮为主，关键词兜底。
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    if ocr_items and any(any(k in it["text"] for k in _MODAL_KEYWORDS)
                         for it in ocr_items):
        return True

    region = _panel_by_contrast(gray, hsv)
    if region is None:
        region = _panel_by_brightness(gray, hsv)
    if region is not None:
        blocks = _detect_button_blocks(gray, hsv, region)
        if blocks:
            return True

    return False


def _find_dialog_region(gray, hsv, ocr_items):
    """综合图像与 OCR 线索定位对话框 bbox。"""
    region = _panel_by_contrast(gray, hsv)
    if region is None:
        region = _panel_by_brightness(gray, hsv)
    if region is None and ocr_items:
        region = _bbox_from_keywords(ocr_items)
    return region


def _group_text_lines(items):
    """按 y 聚类为行，每行内按 x 排序。返回 [[item, ...], ...]。"""
    if not items:
        return []
    items = sorted(items, key=lambda it: it["cy"])
    lines = []
    for it in items:
        placed = False
        for line in lines:
            if abs(it["cy"] - line[0]["cy"]) < 28:
                line.append(it)
                placed = True
                break
        if not placed:
            lines.append([it])
    for line in lines:
        line.sort(key=lambda it: it["cx"])
    return lines


def _detect_button_blocks(gray, hsv, region):
    """在对话框底部找灰色/绿色按钮色块。返回 [{"rect":(x,y,w,h), "color":str}, ...]。"""
    rx, ry, rw, rh = region
    bottom_y0 = ry + int(rh * 0.58)
    if bottom_y0 >= ry + rh:
        return []

    dialog_bg = estimate_bg(gray, ry, ry + rh)
    bottom_gray = gray[bottom_y0:ry + rh, rx:rx + rw]
    bottom_hsv = hsv[bottom_y0:ry + rh, rx:rx + rw]

    # 灰色按钮：比对话框背景更亮，但不要太亮（避免捕获白字）
    gray_btn = ((bottom_gray > dialog_bg + 6) & (bottom_gray < dialog_bg + 90)).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31))
    gray_btn = cv2.morphologyEx(gray_btn, cv2.MORPH_CLOSE, k)

    # 绿色按钮
    hh, ss, vv = bottom_hsv[:, :, 0], bottom_hsv[:, :, 1], bottom_hsv[:, :, 2]
    green_btn = ((hh >= LC.GREEN_H_LO) & (hh <= LC.GREEN_H_HI)
                 & (ss > LC.GREEN_S_MIN) & (vv > LC.GREEN_V_MIN)).astype(np.uint8)
    green_btn = cv2.morphologyEx(green_btn, cv2.MORPH_CLOSE, k)

    blocks = []
    for mask, color in ((gray_btn, "gray"), (green_btn, "green")):
        for x, y, bw, bh, area in comps_from_mask(
                mask, min_area=6000, min_w=100, min_h=50):
            # 按钮：扁平矩形，高度有上限，宽度至少占对话框一定比例
            if bh > 130 or bh > rh * 0.35:
                continue
            if bw < rw * 0.15:
                continue
            if bw / max(bh, 1) < 1.8:
                continue
            blocks.append({
                "rect": (rx + x, bottom_y0 + y, bw, bh),
                "color": color,
            })
    return blocks


def _assign_buttons(inner_items, blocks):
    """把 OCR 项与按钮色块匹配，生成按钮元素列表。

    优先用色块位置；没有色块时用已知按钮文案。
    """
    buttons = []
    used = set()

    for block in blocks:
        bx, by, bw, bh = block["rect"]
        hits = [it for it in inner_items
                if id(it) not in used
                and rect_contains((bx, by, bw, bh), it["cx"], it["cy"])]
        if not hits:
            continue
        hits.sort(key=lambda it: it["cy"])
        label = "\n".join(it["text"] for it in hits)
        for it in hits:
            used.add(id(it))
        buttons.append({
            "label": label,
            "position": {"x": bx, "y": by, "w": bw, "h": bh},
            "verified": True,
            "color": block["color"],
        })

    # 未被色块捕获的已知按钮文案（兜底，适用于无背景色块的纯文字菜单按钮）
    for it in inner_items:
        if id(it) in used:
            continue
        txt = it["text"].strip()
        if any(txt == bl or txt.startswith(bl) for bl in _BUTTON_LABELS):
            x0, y0, x1, y1 = (int(v) for v in it["box"])
            pad = 30
            buttons.append({
                "label": txt,
                "position": {
                    "x": max(0, x0 - pad), "y": max(0, y0 - pad),
                    "w": min(LC.SCREEN_W, x1 + pad) - max(0, x0 - pad),
                    "h": min(LC.SCREEN_H, y1 + pad) - max(0, y0 - pad),
                },
                "verified": False,
                "color": "unknown",
            })
            used.add(id(it))

    return buttons, used


def _split_title_body(content_items, region):
    """把对话框内非按钮文本拆分为 title 与 body。"""
    rx, ry, rw, rh = region
    lines = _group_text_lines(content_items)
    if not lines:
        return None, None

    # 标题启发式：第一行，位于对话框上半部分，较短，且下方还有内容
    title = None
    first_text = "".join(it["text"] for it in lines[0])
    if (lines[0][0]["cy"] < ry + rh * 0.42
            and len(first_text) <= 14
            and len(lines) > 1
            and lines[1][0]["cy"] > lines[0][0]["cy"] + rh * 0.12):
        title = first_text
        body_lines = lines[1:]
    else:
        body_lines = lines

    body = "\n".join("".join(it["text"] for it in line) for line in body_lines)
    return title, body


def parse_dialog(img, ocr_items=None, gray=None, hsv=None):
    """解析弹窗内容。

    返回 (elements, page_extra)：
    - elements: 符合 V2_PERCEPTION_ARCH.md schema 的字典列表
    - page_extra: {"dialog_region":(x,y,w,h), "title":..., "message":...,
                   "button_labels":[...], "detected_by":...}
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    if ocr_items is None:
        from .ocr_engine import run_ocr
        ocr_items = run_ocr(img)

    region = _find_dialog_region(gray, hsv, ocr_items)
    if region is None:
        return [], {}

    rx, ry, rw, rh = region
    inner_items = [it for it in ocr_items
                   if rect_contains(region, it["cx"], it["cy"])]

    blocks = _detect_button_blocks(gray, hsv, region)
    buttons, used_ids = _assign_buttons(inner_items, blocks)

    content_items = [it for it in inner_items if id(it) not in used_ids]
    title, body = _split_title_body(content_items, region)

    elements = []
    seq = 0

    if title:
        seq += 1
        title_items = [it for it in content_items
                       if it["cy"] < ry + rh * 0.42]
        xs = [it["box"][0] for it in title_items] + [it["box"][2] for it in title_items]
        ys = [it["box"][1] for it in title_items] + [it["box"][3] for it in title_items]
        elements.append({
            "id": "dialog_title",
            "seq": seq,
            "type": "text",
            "name": "弹窗标题",
            "label": title,
            "content": None,
            "position": {
                "x": int(min(xs)) if xs else rx,
                "y": int(min(ys)) if ys else ry,
                "w": int(max(xs) - min(xs)) if xs else rw,
                "h": int(max(ys) - min(ys)) if ys else int(rh * 0.2),
            },
            "outline": None,
            "confidence": round(float(np.mean([it["conf"] for it in title_items])), 3) if title_items else 0.7,
            "verified": len(title_items) > 0,
            "actions": [],
            "state": None,
            "source": "ocr",
        })

    if body:
        seq += 1
        body_items = [it for it in content_items if id(it) not in used_ids]
        if title:
            body_items = [it for it in body_items if it["cy"] >= ry + rh * 0.35]
        xs = [it["box"][0] for it in body_items] + [it["box"][2] for it in body_items]
        ys = [it["box"][1] for it in body_items] + [it["box"][3] for it in body_items]
        elements.append({
            "id": "dialog_message",
            "seq": seq,
            "type": "text",
            "name": "弹窗正文",
            "label": body,
            "content": None,
            "position": {
                "x": int(min(xs)) if xs else rx + 40,
                "y": int(min(ys)) if ys else ry + int(rh * 0.25),
                "w": int(max(xs) - min(xs)) if xs else rw - 80,
                "h": int(max(ys) - min(ys)) if ys else int(rh * 0.5),
            },
            "outline": None,
            "confidence": round(float(np.mean([it["conf"] for it in body_items])), 3) if body_items else 0.7,
            "verified": len(body_items) > 0,
            "actions": [],
            "state": None,
            "source": "ocr",
        })

    for i, btn in enumerate(buttons, 1):
        seq += 1
        elements.append({
            "id": f"dialog_btn_{i}",
            "seq": seq,
            "type": "dialog_button",
            "name": btn["label"] or "按钮",
            "label": btn["label"],
            "content": None,
            "position": btn["position"],
            "outline": None,
            "confidence": 0.9 if btn["verified"] else 0.7,
            "verified": btn["verified"],
            "actions": ["tap"],
            "state": None,
            "source": "geometry" if btn["verified"] else "ocr",
        })

    page_extra = {
        "dialog_region": region,
        "title": title,
        "message": body,
        "button_labels": [btn["label"] for btn in buttons],
        "button_count": len(buttons),
    }
    return elements, page_extra


if __name__ == "__main__":
    import sys
    import json

    path = "samples/ui_inventory/11_dialogs/dialog_delete_confirm.png"
    if len(sys.argv) > 1:
        path = sys.argv[1]

    img = cv2.imread(path)
    if img is None:
        print(f"无法读取图片: {path}", file=sys.stderr)
        sys.exit(1)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    from .ocr_engine import run_ocr
    ocr_items = run_ocr(img)

    detected = detect_dialog(img, gray=gray, hsv=hsv, ocr_items=ocr_items)
    print(f"detect_dialog: {detected}")

    elements, extra = parse_dialog(img, ocr_items=ocr_items, gray=gray, hsv=hsv)
    print(json.dumps({
        "detected": detected,
        "dialog_region": extra.get("dialog_region"),
        "title": extra.get("title"),
        "message": extra.get("message"),
        "button_labels": extra.get("button_labels"),
        "elements": elements,
    }, ensure_ascii=False, indent=2))
