#!/usr/bin/env python3
"""icon_detector.py - v2 图标多尺度模板匹配检测器

对外提供：
    detect_icons(img, gray=None, hsv=None, roi=None,
                 threshold=0.75, scale_range=(0.9, 1.1, 0.05)) -> list[dict]
    detect_back_button(img, hsv) -> dict | None
    detect_top_right_buttons(img, hsv) -> list[dict]

匹配策略：
- 灰度图 + cv2.matchTemplate(TM_CCOEFF_NORMED)
- 多尺度缩放模板（默认 0.9~1.1，步长 0.05）
- 全阈值点收集 + IoU NMS（默认 IoU > 0.3 抑制）
- 输出元素遵循 V2_PERCEPTION_ARCH.md 的 schema
"""

import logging
from typing import List, Dict, Any, Optional, Tuple

import cv2
import numpy as np

from . import layout_consts as LC
from .icon_templates import load_template_variants

logger = logging.getLogger(__name__)

# 从 icon_templates 文件名映射到元素元数据（name / type / 默认 state / min_confidence）
_TEMPLATE_META = {
    "back_arrow":       {"name": "返回",     "type": "back_button", "min_confidence": 0.80},
    "search_icon":      {"name": "搜索",     "type": "icon_button", "min_confidence": 0.80},
    "home_plus_icon":   {"name": "加号",     "type": "icon_button", "min_confidence": 0.80},
    "chat_more_icon":   {"name": "更多",     "type": "icon_button", "min_confidence": 0.80},
    "chat_voice_icon":  {"name": "语音切换", "type": "icon_button", "min_confidence": 0.80},
    "chat_emoji_icon":  {"name": "表情",     "type": "icon_button", "min_confidence": 0.80},
    "chat_plus_icon":   {"name": "扩展加号", "type": "icon_button", "min_confidence": 0.80},
    "send_button":      {"name": "发送",     "type": "button",      "min_confidence": 0.90},
    "tab_wechat":       {"name": "微信",     "type": "tab",         "min_confidence": 0.75},
    "tab_contacts":     {"name": "通讯录",   "type": "tab",         "min_confidence": 0.75},
    "tab_discover":     {"name": "发现",     "type": "tab",         "min_confidence": 0.75},
    "tab_me":           {"name": "我",       "type": "tab",         "min_confidence": 0.75},
    "switch_on":        {"name": "开关",     "type": "switch",      "state": "on",  "min_confidence": 0.88},
    "switch_off":       {"name": "开关",     "type": "switch",      "state": "off", "min_confidence": 0.88},
    "avatar_placeholder": {"name": "默认头像", "type": "avatar",    "min_confidence": 0.85},
    "qr_code_icon":     {"name": "二维码",   "type": "icon_button", "min_confidence": 0.85},
    "close_x":          {"name": "关闭",     "type": "icon_button", "min_confidence": 0.90},
    "camera_icon":      {"name": "相机",     "type": "icon_button", "min_confidence": 0.85},
}


def _build_registry():
    """把 icon_templates 的简单 dict 转换为 icon_detector 内部需要的注册表。"""
    registry = []
    for key, variants in load_template_variants().items():
        meta = _TEMPLATE_META.get(key)
        if meta is None:
            continue
        out_vars = []
        for variant_label, img in variants:
            var = {"image": img}
            # 变体标签 -> state；特殊处理 tab active/inactive
            if variant_label in ("on", "off"):
                var["state"] = variant_label
            elif variant_label in ("active", "inactive"):
                var["state"] = "selected" if variant_label == "active" else "unselected"
            elif meta.get("state"):
                var["state"] = meta["state"]
            out_vars.append(var)
        registry.append({
            "key": key,
            "name": meta["name"],
            "type": meta["type"],
            "min_confidence": meta.get("min_confidence", 0.0),
            "variants": out_vars,
        })
    return registry

# 标题栏图标搜索区域：图标中心在 y≈150，模板高可达 100，需要比 TITLE_Y1 更大的下边界
# 才能完整容纳模板，否则 matchTemplate 会被迫向上平移导致得分骤降。
_TOP_ICON_Y1 = 250


def _to_gray(img: np.ndarray) -> np.ndarray:
    """确保返回单通道灰度图。"""
    return img if len(img.shape) == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _box_iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    """两矩形 (x0, y0, x1, y1) 的 IoU。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    inter_w = max(0, min(ax1, bx1) - max(ax0, bx0))
    inter_h = max(0, min(ay1, by1) - max(ay0, by0))
    inter = inter_w * inter_h
    if inter == 0:
        return 0.0
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    return inter / float(area_a + area_b - inter)


def _nms(detections: List[Dict[str, Any]], iou_thr: float = 0.3) -> List[Dict[str, Any]]:
    """按置信度降序，抑制 IoU 超过阈值的重复检测。"""
    if not detections:
        return []
    boxes = []
    for d in detections:
        p = d["position"]
        boxes.append([
            p["x"], p["y"], p["x"] + p["w"], p["y"] + p["h"],
            d["confidence"], d,
        ])
    # 置信度降序
    boxes.sort(key=lambda x: x[4], reverse=True)
    kept = []
    while boxes:
        best = boxes.pop(0)
        kept.append(best[5])
        boxes = [b for b in boxes if _box_iou(tuple(best[:4]), tuple(b[:4])) <= iou_thr]
    return kept


def _scales_from_range(scale_range: Tuple[float, float, float]) -> List[float]:
    """将 (start, stop, step) 转换为包含 stop 的浮点列表。"""
    start, stop, step = scale_range
    vals = []
    s = start
    while s <= stop + 1e-6:
        vals.append(s)
        s += step
    return vals


def detect_icons(
    img: np.ndarray,
    gray: Optional[np.ndarray] = None,
    hsv: Optional[np.ndarray] = None,
    roi: Optional[Tuple[int, int, int, int]] = None,
    threshold: float = 0.75,
    scale_range: Tuple[float, float, float] = (0.9, 1.1, 0.05),
) -> List[Dict[str, Any]]:
    """多尺度模板匹配检测所有已注册图标。

    参数：
        img:         BGR 截图（gray 为 None 时用于转灰度）
        gray:        可选预计算灰度图
        hsv:         可选预计算 HSV（接口一致性，当前未用于匹配）
        roi:         可选 (x0, y0, x1, y1) 限制搜索区域
        threshold:   匹配得分阈值
        scale_range: (min_scale, max_scale, step)

    返回：
        元素 dict 列表，每个 dict 包含：
            id, type, name, position, confidence,
            verified, actions=["tap"], source="template", state
    """
    if gray is None:
        gray = _to_gray(img)

    search_img = gray
    offset_x, offset_y = 0, 0
    if roi is not None:
        x0, y0, x1, y1 = roi
        offset_x, offset_y = x0, y0
        search_img = gray[y0:y1, x0:x1]
        if search_img.size == 0:
            logger.warning("Empty search ROI: %s", roi)
            return []

    templates = _build_registry()
    scales = _scales_from_range(scale_range)
    detections: List[Dict[str, Any]] = []

    for tmpl in templates:
        tmpl_thr = max(threshold, tmpl.get("min_confidence", 0.0))
        for var in tmpl["variants"]:
            timg = var.get("image")
            if timg is None:
                continue
            tgray = _to_gray(timg)
            th, tw = tgray.shape[:2]

            for scale in scales:
                nw = int(round(tw * scale))
                nh = int(round(th * scale))
                if nw < 8 or nh < 8:
                    continue
                if nw > search_img.shape[1] or nh > search_img.shape[0]:
                    continue

                interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                resized = cv2.resize(tgray, (nw, nh), interpolation=interp)
                res = cv2.matchTemplate(search_img, resized, cv2.TM_CCOEFF_NORMED)

                ys, xs = np.where(res >= tmpl_thr)
                for y, x in zip(ys, xs):
                    conf = float(res[y, x])
                    detections.append({
                        "key": tmpl["key"],
                        "name": tmpl["name"],
                        "type": tmpl["type"],
                        "state": var.get("state"),
                        "position": {
                            "x": int(x + offset_x),
                            "y": int(y + offset_y),
                            "w": nw,
                            "h": nh,
                        },
                        "confidence": conf,
                    })

    kept = _nms(detections, iou_thr=0.3)

    elements = []
    for idx, d in enumerate(kept):
        elements.append({
            "id": f"icon_{d['key']}_{idx}",
            "type": d.get("type", "icon_button"),
            "name": d["name"],
            "label": None,
            "content": None,
            "position": d["position"],
            "outline": None,
            "confidence": round(d["confidence"], 3),
            "verified": d["confidence"] >= 0.85,
            "actions": ["tap"],
            "state": d.get("state"),
            "source": "template",
        })
    return elements


def detect_back_button(img: np.ndarray, hsv: np.ndarray) -> Optional[Dict[str, Any]]:
    """检测左上角返回箭头，命中则返回 back_button 元素，否则 None。

    只搜索标题栏左上区域，避免正文内容中的相似箭头误检。
    """
    roi = (0, LC.STATUS_BAR_BOTTOM, 180, _TOP_ICON_Y1)
    icons = detect_icons(img, roi=roi, threshold=0.75, scale_range=(0.9, 1.1, 0.05))
    for el in icons:
        if el.get("name") == "返回":
            el["type"] = "back_button"
            el["id"] = "btn_back"
            el["actions"] = ["back", "tap"]
            return el
    return None


def detect_top_right_buttons(img: np.ndarray, hsv: np.ndarray) -> List[Dict[str, Any]]:
    """检测右上角功能图标：搜索 / 加号 / 更多。

    限定在标题栏右上区域，减少误检。
    """
    roi = (LC.SCREEN_W - 300, LC.STATUS_BAR_BOTTOM, LC.SCREEN_W, _TOP_ICON_Y1)
    icons = detect_icons(img, roi=roi, threshold=0.75, scale_range=(0.9, 1.1, 0.05))
    names = {"搜索", "加号", "更多"}
    return [el for el in icons if el.get("name") in names]


# ---------------------------------------------------------------------------
# 简易自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    default_sample = "samples/ui_inventory/02_home/home_main_v2.png"
    path = sys.argv[1] if len(sys.argv) > 1 else default_sample

    img = cv2.imread(path)
    if img is None:
        print(f"Failed to load sample: {path}", file=sys.stderr)
        sys.exit(1)

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    print("=== detect_icons ===")
    for el in detect_icons(img, hsv=hsv):
        print(f"{el['id']:25s} {el['name']:8s} conf={el['confidence']:.3f} "
              f"pos={el['position']} state={el['state']}")

    print("\n=== detect_back_button ===")
    back = detect_back_button(img, hsv)
    print(back if back is not None else "(not detected)")

    print("\n=== detect_top_right_buttons ===")
    for el in detect_top_right_buttons(img, hsv):
        print(f"{el['id']:25s} {el['name']:8s} conf={el['confidence']:.3f} "
              f"pos={el['position']}")
