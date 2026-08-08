#!/usr/bin/env python3
"""generic_parser.py - v2 通用列表页解析器。

覆盖页面：wechat_contacts / wechat_discover / wechat_me /
          wechat_settings / wechat_add_friend / wechat_profile。

输入为 BGR 截图 + OCR items，输出统一元素列表、input_area（None）、
可用动作列表和 page_extra。

设计原则：
- 优先使用外部已检测到的 icon_elements / special_elements（未来集成层传入）。
- 未提供时，回退到 layout 常量 + icon_detector / special_detector 做兜底。
- 不修改 state_builder.py，仅在需要底部 Tab 时延迟导入其 _tab_elements。
"""

from typing import List, Dict, Any, Optional, Tuple

import cv2
import numpy as np

from . import layout_consts as LC
from . import list_parser


# ---------------------------------------------------------------- 常量
_TAB_PAGES = {"wechat_contacts", "wechat_discover", "wechat_me"}

_PAGE_TITLE_FALLBACK = {
    "wechat_contacts": "通讯录",
    "wechat_discover": "发现",
    "wechat_me": "我",
    "wechat_settings": "设置",
    "wechat_add_friend": "添加朋友",
    "wechat_profile": "个人信息",
}

_TOP_RIGHT_ICON_NAMES = {"搜索", "加号", "更多"}


# ---------------------------------------------------------------- 元素构造辅助
def _element(
    id_: str,
    type_: str,
    name: str,
    x: int,
    y: int,
    w: int,
    h: int,
    label: Optional[str] = None,
    content: Optional[str] = None,
    state: Optional[str] = None,
    confidence: float = 0.5,
    verified: bool = False,
    actions: Optional[List[str]] = None,
    source: str = "layout",
) -> Dict[str, Any]:
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


def _norm_ocr_items(ocr_items):
    """复用 list_parser 的规范化，兼容内部 OCR dict 与样本 JSON dict。"""
    return list_parser._normalize_ocr_items(ocr_items)


def _title_element(ocr_items, page_type: str) -> Dict[str, Any]:
    """标题栏元素：从标题区 OCR 取文字；取不到时用 page_type 兜底。"""
    cands = [
        it for it in _norm_ocr_items(ocr_items)
        if LC.TITLE_Y0 <= it["cy"] <= LC.TITLE_Y1
        and 180 <= it["cx"] <= LC.SCREEN_W - 250
    ]
    cands.sort(key=lambda it: it["box"][0])
    title = "".join(it["text"] for it in cands).strip()
    confs = [it["conf"] for it in cands if it["conf"] > 0]
    confidence = round(sum(confs) / len(confs), 3) if confs else 0.0
    source = "ocr"
    if not title:
        title = _PAGE_TITLE_FALLBACK.get(page_type, "")
        confidence = 0.3
        source = "layout"
    return _element(
        "title_bar", "title_bar", title,
        0, LC.TITLE_Y0, LC.SCREEN_W, LC.TITLE_Y1 - LC.TITLE_Y0,
        label=title, confidence=confidence, verified=source == "ocr",
        actions=[], source=source,
    )


def _layout_back_button() -> Dict[str, Any]:
    r = LC.CHAT_BTN_R
    cx, cy = LC.CHAT_BACK_CENTER
    return _element(
        "btn_back", "back_button", "返回",
        cx - r, cy - r, 2 * r, 2 * r,
        actions=["back", "tap"], source="layout",
    )


def _find_in_icon_elements(icon_elements, predicate):
    if not icon_elements:
        return None
    for el in icon_elements:
        if predicate(el):
            return dict(el)
    return None


def _back_button_from_icons(icon_elements) -> Optional[Dict[str, Any]]:
    def _is_back(el):
        return el.get("type") == "back_button" or el.get("name") in ("返回", "back")
    el = _find_in_icon_elements(icon_elements, _is_back)
    if el is None:
        return None
    el.setdefault("actions", ["back", "tap"])
    return el


def _top_right_icons_from_icons(icon_elements) -> List[Dict[str, Any]]:
    out = []
    if not icon_elements:
        return out
    for el in icon_elements:
        name = el.get("name", "")
        if el.get("type") in ("icon_button", "button") and name in _TOP_RIGHT_ICON_NAMES:
            e = dict(el)
            e.setdefault("actions", ["tap"])
            out.append(e)
    return out


def _search_box_from_ocr(ocr_items) -> Optional[Dict[str, Any]]:
    """从内容区顶部 '搜索...' 占位文字构造 search_box。"""
    for it in _norm_ocr_items(ocr_items):
        if LC.CONTENT_Y0 <= it["cy"] <= LC.CONTENT_Y0 + 220 and "搜索" in it["text"]:
            x0, y0, x1, y1 = (int(v) for v in it["box"])
            y0 = max(LC.CONTENT_Y0, y0 - 8)
            y1 = min(LC.TAB_BAR_Y0, y1 + 8)
            return _element(
                "search_box", "search_box", "搜索框",
                0, y0, LC.SCREEN_W, y1 - y0,
                label=it["text"], confidence=it["conf"],
                verified=True, actions=["focus_search", "tap"], source="ocr",
            )
    return None


def _center_in_rect(it, rect: Tuple[int, int, int, int]) -> bool:
    """判断一个原始 OCR item 的中心点是否落在 rect (x,y,w,h) 内。"""
    x, y, w, h = rect
    if "box" in it:
        box = it["box"]
        if isinstance(box[0], (list, tuple)):
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            cx = (min(xs) + max(xs)) / 2.0
            cy = (min(ys) + max(ys)) / 2.0
        else:
            cx = (float(box[0]) + float(box[2])) / 2.0
            cy = (float(box[1]) + float(box[3])) / 2.0
    elif "bbox" in it:
        b = it["bbox"]
        cx = float(b["x"]) + float(b["w"]) / 2.0
        cy = float(b["y"]) + float(b["h"]) / 2.0
    else:
        return False
    return x <= cx <= x + w and y <= cy <= y + h


def _fallback_special_elements(
    img: np.ndarray,
    gray: np.ndarray,
    hsv: np.ndarray,
    page_type: str,
) -> List[Dict[str, Any]]:
    """未提供 special_elements 时，调用 special_detector 做几何/颜色启发式兜底。"""
    from .special_detector import detect_avatars, detect_badges

    out: List[Dict[str, Any]] = []
    try:
        out.extend(detect_avatars(img, gray=gray, hsv=hsv))
    except Exception:
        pass
    try:
        out.extend(detect_badges(img, hsv=hsv))
    except Exception:
        pass

    if page_type == "wechat_settings":
        try:
            from .special_detector import detect_switches
            out.extend(detect_switches(img, hsv=hsv))
        except Exception:
            pass

    if page_type == "wechat_profile":
        try:
            from .special_detector import detect_qr_regions
            out.extend(detect_qr_regions(img, gray=gray))
        except Exception:
            pass

    return out


def _merge_special_elements(special_elements: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """外部传入的特殊元素：校验类型、补全动作。"""
    out = []
    if not special_elements:
        return out
    for el in special_elements:
        t = el.get("type")
        if t not in ("avatar", "badge", "switch", "qr_code"):
            continue
        e = dict(el)
        if t == "avatar":
            e.setdefault("actions", ["tap", "long_press"])
        elif t == "switch":
            e.setdefault("actions", ["toggle"])
        elif t == "qr_code":
            e.setdefault("actions", ["tap"])
        else:
            e.setdefault("actions", [])
        out.append(e)
    return out


def _bottom_tabs(current_tab: str) -> List[Dict[str, Any]]:
    """延迟导入 state_builder._tab_elements，并把动作改为 switch_tab。"""
    from .state_builder import _tab_elements
    tabs = _tab_elements(current_tab)
    for t in tabs:
        t["actions"] = ["switch_tab"]
    return tabs


# ---------------------------------------------------------------- 主入口
def parse_generic_page(
    img: np.ndarray,
    ocr_items: List[Dict[str, Any]],
    page_type: str,
    gray: Optional[np.ndarray] = None,
    hsv: Optional[np.ndarray] = None,
    icon_elements: Optional[List[Dict[str, Any]]] = None,
    special_elements: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """解析通用微信列表页。

    返回：
        elements:   统一元素列表（含 back/title/search/list/special/tabs）
        input_area: 通用列表页无输入栏，固定为 None
        actions:    可用动作列表（back / scroll / switch_tab / tap 等）
        page_extra: {current_tab, item_count, has_search, has_back, title}
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    elements: List[Dict[str, Any]] = []

    # 1) 返回键
    back_el = _back_button_from_icons(icon_elements)
    if back_el is None:
        try:
            from .icon_detector import detect_back_button
            back_el = detect_back_button(img, hsv)
        except Exception:
            back_el = None
    if back_el is None:
        back_el = _layout_back_button()
    elements.append(back_el)

    # 2) 标题栏
    title_el = _title_element(ocr_items, page_type)
    elements.append(title_el)

    # 3) 顶部图标（搜索/加号/更多）+ 搜索框
    top_icons = _top_right_icons_from_icons(icon_elements)
    if not top_icons:
        try:
            from .icon_detector import detect_top_right_buttons
            top_icons = detect_top_right_buttons(img, hsv)
        except Exception:
            top_icons = []

    has_search = False
    search_box = _search_box_from_ocr(ocr_items)
    if search_box is not None:
        elements.append(search_box)
        has_search = True

    for el in top_icons:
        name = el.get("name", "")
        if name == "搜索":
            el["actions"] = ["focus_search", "tap"]
            if not has_search:
                # 仅有搜索图标时，也把图标作为可操作元素暴露
                elements.append(el)
                has_search = True
        elif name in ("加号", "更多"):
            el.setdefault("actions", ["tap"])
            elements.append(el)

    # 4) 列表项（若已生成 search_box，把对应 OCR 项从列表解析中排除，避免重复）
    list_ocr_items = ocr_items
    if search_box is not None:
        sb = search_box["position"]
        sb_rect = (sb["x"], sb["y"], sb["w"], sb["h"])
        list_ocr_items = [it for it in ocr_items if not _center_in_rect(it, sb_rect)]
    list_els = list_parser.parse_list_items(list_ocr_items, page_type=page_type)
    elements.extend(list_els)

    # 5) 特殊元素（头像/角标/开关/二维码）
    if special_elements is not None:
        special_els = _merge_special_elements(special_elements)
    else:
        special_els = _fallback_special_elements(img, gray, hsv, page_type)
    elements.extend(special_els)

    # 6) 底部 Tab（通讯录/发现/我）
    current_tab = None
    tab_els = []
    if page_type in _TAB_PAGES:
        reverse = {v: k for k, v in LC.TAB_PAGE_MAP.items()}
        current_tab = reverse.get(page_type)
        if current_tab:
            tab_els = _bottom_tabs(current_tab)
            elements.extend(tab_els)

    # 7) 动作列表
    actions: List[Dict[str, Any]] = [
        {"action": "back", "target": back_el["id"], "description": "返回上一页"},
        {"action": "scroll_up", "description": "向上滚动列表"},
        {"action": "scroll_down", "description": "向下滚动列表"},
    ]
    if has_search:
        actions.append({"action": "focus_search", "description": "聚焦搜索框"})
    for el in list_els:
        actions.append({
            "action": "tap",
            "target": el["id"],
            "description": el.get("label") or el.get("name") or "列表项",
        })
    for el in special_els:
        if el.get("actions"):
            primary = el["actions"][0]
            actions.append({
                "action": primary,
                "target": el["id"],
                "description": el.get("name") or el.get("type"),
            })
    for t in tab_els:
        actions.append({
            "action": "switch_tab",
            "target": t["id"],
            "description": f"切换到{t['name']}",
        })

    page_extra = {
        "page_type": page_type,
        "current_tab": current_tab,
        "item_count": len(list_els),
        "has_search": has_search,
        "has_back": bool(back_el),
        "title": title_el.get("label") or title_el.get("name"),
    }

    return elements, None, actions, page_extra


# ---------------------------------------------------------------- 独立 sanity check
def _load_sample_ocr(path: str) -> List[Dict[str, Any]]:
    """优先复用同目录 .ocr.json；否则跑 OCR。"""
    import json
    import os
    json_path = path + ".ocr.json"
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("items", [])
    from .ocr_engine import run_ocr
    return run_ocr(path)


def _summarize(elements: List[Dict[str, Any]]) -> str:
    lines = []
    for e in elements:
        pos = e["position"]
        actions = ",".join(e.get("actions", []))
        label = f" label={e.get('label')!r}" if e.get("label") else ""
        content = f" content={e.get('content')!r}" if e.get("content") else ""
        lines.append(
            f"  {e['id']:20s} {e['type']:14s} {e['name']!r:10s} "
            f"bbox=({pos['x']},{pos['y']},{pos['w']},{pos['h']}) "
            f"acts=[{actions}]{label}{content}"
        )
    return "\n".join(lines)


def main():
    samples = [
        ("samples/ui_inventory/05_contacts/contacts_main_v2.png", "wechat_contacts"),
        ("samples/ui_inventory/07_discover/discover_main_v2.png", "wechat_discover"),
        ("samples/ui_inventory/09_me/me_main_v2.png", "wechat_me"),
        ("samples/ui_inventory/10_settings/settings_about_v2.png", "wechat_settings"),
        ("samples/ui_inventory/14_other/add_friend.png", "wechat_add_friend"),
    ]

    for path, page_type in samples:
        img = cv2.imread(path)
        if img is None:
            print(f"[SKIP] cannot load {path}")
            continue
        try:
            ocr_items = _load_sample_ocr(path)
        except Exception as e:
            print(f"[SKIP] {path}: OCR failed: {e}")
            continue

        elements, input_area, actions, page_extra = parse_generic_page(
            img, ocr_items, page_type=page_type)

        print(f"\n=== {path} ({page_type}) ===")
        print(f"elements={len(elements)}, actions={len(actions)}, input_area={input_area is not None}")
        print(f"page_extra={page_extra}")
        print(_summarize(elements))


if __name__ == "__main__":
    main()
