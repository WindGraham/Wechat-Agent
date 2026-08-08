#!/usr/bin/env python3
"""scan_parser.py - v2 扫一扫页面解析。

输入 BGR 截图 + OCR items，输出符合 V2_PERCEPTION_ARCH.md 的元素列表。
适配 OnePlus 6T (1080x2340, 深色模式) + 微信 8.0.76。

检测目标：
- 关闭按钮（左上角 X）
- 取景框区域（中央深色相机预览区 + 绿色扫描线）
- 手电筒图标与 "轻触照亮" 文字
- "我的二维码" 按钮
- 底部模式 Tab："扫一扫" / "翻译"
"""

import json
import os

import cv2
import numpy as np

from . import layout_consts as LC
from .img_utils import comps_from_mask, estimate_bg


# ---------------------------------------------------------------- 常量（扫一扫专用）
_CLOSE_CENTER = (50, 150)         # 左上角关闭 X
_CLOSE_R = 45

_VIEWFINDER_Y0 = 250              # 取景框大致垂直范围
_VIEWFINDER_Y1 = 1700
_VIEWFINDER_MIN_AREA = 120000     # 取景框面积较大
_VIEWFINDER_MIN_W = 700
_VIEWFINDER_MIN_H = 700

_FLASHLIGHT_TEXT = "轻触照亮"
_FLASHLIGHT_ICON_Y = 1440         # 手电筒图标中心大致 y
_FLASHLIGHT_ICON_R = 60

_QR_BUTTON_Y0 = 1850              # "我的二维码" 按钮区
_QR_BUTTON_Y1 = 2030

_BOTTOM_TAB_Y0 = 2080             # 底部 Tab 区
_BOTTOM_TAB_Y1 = 2280


# ---------------------------------------------------------------- OCR 归一化
def _normalize_ocr_items(ocr_items):
    out = []
    for it in ocr_items:
        box = it.get("box")
        if box is None:
            b = it.get("bbox")
            if b is None:
                continue
            x0, y0, w, h = float(b["x"]), float(b["y"]), float(b["w"]), float(b["h"])
            x1, y1 = x0 + w, y0 + h
            cx = float(it.get("center", {}).get("x", (x0 + x1) / 2))
            cy = float(it.get("center", {}).get("y", (y0 + y1) / 2))
            conf = float(it.get("score", it.get("conf", 0.0)))
        else:
            if isinstance(box[0], (list, tuple)):
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            else:
                x0, y0, x1, y1 = (float(v) for v in box)
            cx = it.get("cx", (x0 + x1) / 2)
            cy = it.get("cy", (y0 + y1) / 2)
            conf = it.get("conf", it.get("score", 0.0))
        out.append({
            "box": (x0, y0, x1, y1),
            "cx": float(cx), "cy": float(cy),
            "h": float(y1 - y0),
            "text": str(it.get("text", "")).strip(),
            "conf": float(conf),
        })
    return out


# ---------------------------------------------------------------- 元素构造
def _element(id_, type_, name, x, y, w, h,
             label=None, content=None, state=None,
             confidence=0.6, verified=False,
             actions=None, source="layout"):
    return {
        "id": id_,
        "type": type_,
        "name": name,
        "label": label,
        "content": content,
        "state": state,
        "position": {"x": int(x), "y": int(y), "w": int(w), "h": int(h)},
        "outline": None,
        "confidence": float(confidence),
        "verified": bool(verified),
        "actions": list(actions or []),
        "source": source,
    }


def _icon_button(id_, name, cx, cy, r=45, verified=False):
    return _element(id_, "icon_button", name,
                    cx - r, cy - r, 2 * r, 2 * r,
                    confidence=0.7, verified=verified, actions=["tap"],
                    source="layout")


# ---------------------------------------------------------------- 取景框检测
def _detect_viewfinder(gray, hsv):
    """检测中央深色取景框区域。

    返回 (x, y, w, h) 或 None。
    """
    bg = estimate_bg(gray, 0, LC.SCREEN_H)
    # 扫一扫页面整体很暗（相机预览），背景众数可能为 0；
    # 用固定阈值 + 区域众数自适应：取中央区域灰度众数 + 裕量
    center_zone = gray[_VIEWFINDER_Y0:_VIEWFINDER_Y1, 50:LC.SCREEN_W - 50]
    if center_zone.size:
        vals, counts = np.unique(center_zone, return_counts=True)
        center_bg = int(vals[np.argmax(counts)])
    else:
        center_bg = bg
    # 阈值：背景较亮用相对阈值；背景很暗用固定阈值
    thresh = max(center_bg + 30, 50) if center_bg > 20 else 50
    dark = (gray < thresh).astype(np.uint8)
    # 限制在中央区域
    mask = np.zeros_like(dark)
    mask[_VIEWFINDER_Y0:_VIEWFINDER_Y1, 50:LC.SCREEN_W - 50] = \
        dark[_VIEWFINDER_Y0:_VIEWFINDER_Y1, 50:LC.SCREEN_W - 50]

    candidates = []
    for x, y, w, h, area in comps_from_mask(
            mask, min_area=_VIEWFINDER_MIN_AREA, close_ksize=15,
            min_w=_VIEWFINDER_MIN_W, min_h=_VIEWFINDER_MIN_H):
        # 应该是较方正、占满宽度
        if w < 900 or h < 500:
            continue
        candidates.append((area, x, y, w, h))
    if not candidates:
        return None
    # 取面积最大
    candidates.sort(key=lambda c: -c[0])
    _, x, y, w, h = candidates[0]
    return x, y, w, h


def _detect_scan_line(hsv, vf_rect):
    """在取景框内检测绿色扫描线，返回 (x, y, w, h) 或 None。"""
    if vf_rect is None:
        return None
    x0, y0, w, h = vf_rect
    hsv_roi = hsv[y0:y0 + h, x0:x0 + w]
    hh, ss, vv = hsv_roi[:, :, 0], hsv_roi[:, :, 1], hsv_roi[:, :, 2]
    green = ((hh >= LC.GREEN_H_LO) & (hh <= LC.GREEN_H_HI)
             & (ss > LC.GREEN_S_MIN) & (vv > LC.GREEN_V_MIN)).astype(np.uint8)
    # 扫描线应为细长水平条
    lines = []
    for lx, ly, lw, lh, larea in comps_from_mask(
            green, min_area=300, close_ksize=3, min_w=100, min_h=3):
        if lw / max(lh, 1) > 8 and 0.3 < (lx + lw / 2) / w < 0.7:
            lines.append((larea, lx, ly, lw, lh))
    if not lines:
        return None
    lines.sort(key=lambda c: -c[0])
    _, lx, ly, lw, lh = lines[0]
    return x0 + lx, y0 + ly, lw, lh


# ---------------------------------------------------------------- 手电筒/我的二维码/底部 Tab
def _find_text(items, text, y0=None, y1=None):
    for it in items:
        if text in it["text"]:
            if y0 is not None and not (y0 <= it["cy"] <= y1):
                continue
            return it
    return None


def _detect_flashlight_icon(hsv, text_cy):
    """在手电筒文字上方推断图标位置；同时尝试用白色图标验证。"""
    cx, cy = LC.SCREEN_W // 2, int(text_cy) - 100
    # 验证：以 (cx,cy) 为中心的 ROI 内是否有足够白色描边像素
    r = _FLASHLIGHT_ICON_R
    x0, y0 = max(0, cx - r), max(0, cy - r)
    x1, y1 = min(LC.SCREEN_W, cx + r), min(LC.SCREEN_H, cy + r)
    s = hsv[y0:y1, x0:x1, 1]
    v = hsv[y0:y1, x0:x1, 2]
    white_ratio = float(((s < LC.ICON_S_MAX) & (v > LC.ICON_V_MIN)).mean())
    verified = white_ratio > 0.02
    return cx, cy, verified


def _find_qr_button(items):
    """从 OCR 找到 "我的二维码" 按钮区域。"""
    it = _find_text(items, "我的二维码", _QR_BUTTON_Y0, _QR_BUTTON_Y1)
    if it is None:
        return None
    bx0, by0, bx1, by1 = it["box"]
    # 扩展为按钮整区：左侧 QR 图标 + 右侧文字 + 下方说明
    x0 = max(0, bx0 - 80)
    y0 = max(_QR_BUTTON_Y0, by0 - 30)
    x1 = min(LC.SCREEN_W, bx1 + 80)
    y1 = min(LC.SCREEN_H, by1 + 80)
    return x0, y0, x1 - x0, y1 - y0, it


def _find_bottom_tabs(items, hsv):
    """检测底部 "扫一扫" / "翻译" Tab。

    深色模式下 active Tab 文字更亮（V 更高），取亮度最高者为选中。
    """
    tabs = []
    for label, x_center in (("扫一扫", 540), ("翻译", 740)):
        it = _find_text(items, label, _BOTTOM_TAB_Y0, _BOTTOM_TAB_Y1)
        if it is None:
            continue
        bx0, by0, bx1, by1 = (int(v) for v in it["box"])
        roi = hsv[by0:by1, bx0:bx1]
        v_mean = float(roi[:, :, 2].mean()) if roi.size else 0.0
        tabs.append((v_mean, {"label": label, "it": it, "x_center": x_center}))
    if not tabs:
        return []
    tabs.sort(key=lambda c: -c[0])
    max_v = tabs[0][0]
    second_v = tabs[1][0] if len(tabs) > 1 else 0
    result = []
    for v_mean, tab in tabs:
        tab["active"] = v_mean > 20 and (v_mean - second_v) > 5
        result.append(tab)
    return result


# ---------------------------------------------------------------- 主入口
def parse_scan(img, ocr_items, gray=None, hsv=None):
    """返回 (elements, page_extra)。"""
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    items = _normalize_ocr_items(ocr_items)
    elements = []
    page_extra = {"mode": "scan"}

    # ---- 关闭按钮
    elements.append(_icon_button("btn_close", "关闭", *_CLOSE_CENTER,
                                 r=_CLOSE_R, verified=True))

    # ---- 取景框
    vf_rect = _detect_viewfinder(gray, hsv)
    if vf_rect is not None:
        x, y, w, h = vf_rect
        elements.append(_element(
            "viewfinder", "image", "取景框",
            x, y, w, h,
            confidence=0.85, verified=True, actions=[],
            source="geometry"))

        # 绿色扫描线
        line_rect = _detect_scan_line(hsv, vf_rect)
        if line_rect is not None:
            lx, ly, lw, lh = line_rect
            elements.append(_element(
                "scan_line", "text", "扫描线",
                lx, ly, lw, lh,
                confidence=0.75, verified=True, actions=[],
                source="geometry"))

    # ---- 手电筒
    flash_text = _find_text(items, _FLASHLIGHT_TEXT,
                            _VIEWFINDER_Y0, _VIEWFINDER_Y1)
    if flash_text is not None:
        bx0, by0, bx1, by1 = flash_text["box"]
        elements.append(_element(
            "flashlight_hint", "text", "轻触照亮",
            bx0, by0, bx1 - bx0, by1 - by0,
            label=flash_text["text"],
            confidence=flash_text["conf"], verified=True,
            source="ocr"))
        cx, cy, verified = _detect_flashlight_icon(hsv, flash_text["cy"])
        elements.append(_icon_button("btn_flashlight", "手电筒", cx, cy,
                                     r=_FLASHLIGHT_ICON_R,
                                     verified=verified))

    # ---- 我的二维码
    qr_btn = _find_qr_button(items)
    if qr_btn is not None:
        x, y, w, h, it = qr_btn
        elements.append(_element(
            "btn_my_qr", "button", "我的二维码",
            x, y, w, h,
            label=it["text"],
            confidence=it["conf"], verified=True, actions=["tap"],
            source="ocr"))

    # ---- 底部 Tab
    tabs = _find_bottom_tabs(items, hsv)
    for i, tab in enumerate(tabs, 1):
        it = tab["it"]
        bx0, by0, bx1, by1 = it["box"]
        # 扩展为可点击 Tab 区域：以文字为中心，覆盖底部一段
        x0 = max(0, tab["x_center"] - 120)
        x1 = min(LC.SCREEN_W, tab["x_center"] + 120)
        y0 = _BOTTOM_TAB_Y0
        y1 = _BOTTOM_TAB_Y1
        elements.append(_element(
            f"tab_{tab['label']}", "tab", tab["label"],
            x0, y0, x1 - x0, y1 - y0,
            label=tab["label"],
            state="selected" if tab["active"] else "unselected",
            confidence=it["conf"], verified=True, actions=["tap"],
            source="ocr"))
        if tab["active"]:
            page_extra["mode"] = tab["label"]

    return elements, page_extra


# ---------------------------------------------------------------- sanity check
def _load_sample_ocr(path):
    json_path = path + ".ocr.json"
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("items", [])
    from .ocr_engine import run_ocr
    return run_ocr(path)


def main():
    import sys
    path = "samples/ui_inventory/14_other/scan_page.png"
    if len(sys.argv) > 1:
        path = sys.argv[1]
    img = cv2.imread(path)
    if img is None:
        print(f"cannot read {path}", file=sys.stderr)
        sys.exit(1)
    ocr_items = _load_sample_ocr(path)
    elements, extra = parse_scan(img, ocr_items)
    print(json.dumps({
        "sample": path,
        "page_extra": extra,
        "elements": elements,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
