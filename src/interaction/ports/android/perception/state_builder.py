#!/usr/bin/env python3
"""state_builder.py - v2 状态组装：截图 -> v2 状态 JSON。

用法:
    python -m src.v2.state_builder <截图路径> [--bench] [--overlay out.png]

输出 JSON：meta / page（含分页信息）/ elements（id 帧内稳定唯一：
s1..sn 会话、m1..mn 消息、btn_* 按键、tab_* 底部栏、cap_* 胶囊，
气泡带 outline 多边形）/ available_actions。
"""

import sys
import json
import time
import datetime

import cv2
import numpy as np

from . import layout_consts as LC
from .page_detector import detect_page
from .ocr_engine import run_ocr, run_ocr_tiled, enhance_small_text


def _home_buttons():
    """首页固定按键元素（搜索/加号/4 Tab），bbox 硬编码 ROI"""
    els = []
    for id_, name, (cx, cy) in (
            ("btn_search", "搜索", LC.HOME_SEARCH_CENTER),
            ("btn_plus", "扩展加号", LC.HOME_PLUS_CENTER)):
        r = LC.HOME_BTN_R
        els.append({"id": id_, "type": "button", "name": name,
                    "position": {"x": cx - r, "y": cy - r, "w": 2 * r, "h": 2 * r},
                    "actions": ["tap"]})
    return els


def _tab_elements(current_tab):
    """底部 4 Tab 元素（所有 tab 页共用）"""
    els = []
    for name, (icon, text) in LC.TAB_ROIS.items():
        x0 = min(icon[0], text[0]); y0 = icon[1]
        x1 = max(icon[2], text[2]); y1 = text[3]
        els.append({"id": f"tab_{LC.TAB_PAGE_MAP[name].split('_')[1]}",
                    "type": "tab", "name": name,
                    "active": name == current_tab,
                    "position": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                    "actions": ["tap"]})
    return els


def _collect_perception_elements(img, gray, hsv, page_type):
    """调用 icon/special detectors 收集非文字可操作元素。
    返回 (icon_elements, special_elements)。chat/home 页返回空列表（它们自带）。"""
    if page_type in ("wechat_home", "wechat_chat"):
        return [], []
    try:
        from .icon_detector import detect_icons
        icon_elements = detect_icons(img, hsv=hsv)
    except Exception:
        icon_elements = []
    special_elements = []
    try:
        from .special_detector import detect_avatars, detect_badges
        special_elements.extend(detect_avatars(img, gray=gray, hsv=hsv))
        special_elements.extend(detect_badges(img, hsv=hsv))
    except Exception:
        pass
    if page_type in ("wechat_settings", "wechat_profile"):
        try:
            from .special_detector import detect_switches
            special_elements.extend(detect_switches(img, hsv=hsv))
        except Exception:
            pass
    if page_type in ("wechat_add_friend", "wechat_profile"):
        try:
            from .special_detector import detect_qr_regions
            special_elements.extend(detect_qr_regions(img, gray=gray))
        except Exception:
            pass
    return icon_elements, special_elements


def _chat_elements_from_slices(sliced, title):
    """chat_slicer 输出 -> v2 消息/头像元素列表（state_builder 适配器）。

    每个消息段产出 message_bubble（text 带 bubble_rect；multimedia 为段条带），
    附带头像元素（L1）；time_divider/system 独立成元素。"""
    msgs = sliced.get("messages", [])
    elements = []
    seq_b, seq_a = 0, 0
    for i, m in enumerate(msgs):
        ctype = m.get("content_type", "text")
        y = int(m.get("y", 0))
        next_y = int(msgs[i + 1]["y"]) if i + 1 < len(msgs) else LC.INPUT_BAR_Y0
        seg_h = max(60, next_y - y)
        if ctype in ("time_divider", "system"):
            elements.append({
                "id": f"time_{i + 1}",
                "type": "time_divider" if ctype == "time_divider" else "system_message",
                "name": m.get("content", ""),
                "content": m.get("content", ""),
                "position": {"x": 0, "y": y, "w": LC.SCREEN_W,
                             "h": min(seg_h, 80)},
                "actions": [], "source": "geometry",
            })
            continue
        seq_b += 1
        is_mine = m.get("side") == "self"
        nick = m.get("nickname")
        sender = "我" if is_mine else (nick or title)
        br = m.get("bubble_rect")
        if br:
            pos = {"x": int(br[0]), "y": int(br[1]),
                   "w": int(br[2]), "h": int(br[3])}
        else:
            pos = {"x": 0, "y": y, "w": LC.SCREEN_W, "h": seg_h}
        elements.append({
            "id": f"m{seq_b}", "seq": seq_b,
            "type": "message_bubble",
            "sender": sender,
            "sender_nickname": nick,
            "content": m.get("content", ""),
            "content_norm": m.get("content_norm", ""),
            "content_type": ctype,                # text / multimedia
            "is_mine": is_mine,
            "mentions": [],
            "low_confidence": bool(m.get("low_confidence")),
            "partial_top": bool(m.get("partial_top")),
            "partial_bottom": bool(m.get("partial_bottom")),
            "position": pos,
            "outline": m.get("outline"),
            "actions": (["copy_text", "long_press_message"] if ctype == "text"
                        else ["long_press_message"]),
            "source": "geometry",
        })
        av = m.get("avatar")
        if av:
            seq_a += 1
            elements.append({
                "id": f"avatar_{seq_a}", "type": "avatar",
                "name": f"头像({av.get('side')})",
                "position": {"x": int(av["x"]), "y": int(av["y"]),
                             "w": int(av["w"]), "h": int(av["h"])},
                "confidence": 0.5 if m.get("low_confidence") else 0.9,
                "verified": not m.get("low_confidence"),
                "actions": ["tap", "long_press"],
                "source": "geometry",
            })
    return elements


def build_state(img, source="<memory>", timing=None):
    """img(BGR) -> v2 状态 dict。timing: 可选 dict 收集各阶段耗时(ms)。"""
    t0 = time.perf_counter()

    # 灰度/HSV 全图只算一次，detect/parsers 共享（性能收口 2026-08-04）
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # 全图 OCR 先行：分块并发（3 横带，overlap 防切行，det 小字检出更好，
    # 5060Ti 3 线程并发与整图单遍相当）；detect_page 复用其结果取标题
    ocr_items = run_ocr_tiled(img)
    t_ocr = time.perf_counter()
    # IME 锁定 ADBKeyBoard 后，输入聚焦时底部出现 "ADB Keyboard {ON}" 深色细条
    # （y≈2260-2320）：不是 UI 组件也不是消息，过滤掉防止污染解析
    bx0, by0, bx1, by1 = LC.ADB_IME_BAR
    ocr_items = [it for it in ocr_items
                 if not (by0 <= it["cy"] <= by1
                         and any(k in it["text"] for k in LC.ADB_IME_BAR_KEYWORDS))]

    page = detect_page(img, ocr_items, hsv=hsv, gray=gray)
    t_detect = time.perf_counter()

    h, w = img.shape[:2]
    elements, actions, input_area = [], [], None
    page_dict = {"type": page.type, "title": page.title,
                 "path": f"wechat/{page.type}"}

    if page.type == "wechat_home":
        from .home_parser import parse_home
        enhance_small_text(img, ocr_items)
        elements, extra = parse_home(img, ocr_items, gray=gray, hsv=hsv)
        page_dict.update(extra)
        page_dict["title"] = "微信"
        page_dict["path"] = "wechat/home"
        page_dict["current_tab"] = page.current_tab
        elements.extend(_home_buttons())
        elements.extend(_tab_elements(page.current_tab))
        actions = [
            {"action": "enter_session", "target": e["label"],
             "description": f"进入和{e['label']}的聊天"}
            for e in elements
            if e["type"] == "session_item" and e["label"] and not e["partial"]]
        actions += [
            {"action": "scroll_down", "description": "向下滚动会话列表"},
            {"action": "scroll_up", "description": "向上滚动会话列表（第一页继续上滑拉出小程序面板）"},
        ]
        actions += [
            {"action": "switch_tab", "target": t, "description": f"切换到{t}"}
            for t in ("通讯录", "发现", "我")]
    elif page.type == "wechat_chat":
        from .chat_parser import parse_chat
        from .page_detector import _title_from_ocr
        # 标题以全图 OCR 结果为准（检测区域裁剪的兜底 OCR 质量差，会丢字）
        title_full = _title_from_ocr(img, ocr_items)
        enhance_small_text(img, ocr_items)
        title, elements, input_area, actions, extra = parse_chat(
            img, ocr_items, title_full or page.title, gray_img=gray, hsv=hsv)
        page_dict["title"] = title
        page_dict["path"] = f"wechat/chat/{title}"
        page_dict.update(extra)
        # WP2 集成：头像顶切段解析（头像元素 + 昵称 + multimedia 标注），
        # 替换气泡驱动解析的消息元素；按钮/胶囊等非消息元素保留
        try:
            from .chat_slicer import slice_chat
            is_group = bool(extra.get("is_group") or extra.get("member_count"))
            sliced = slice_chat(img, ocr_items, is_group=is_group, title=title)
            kept = [e for e in elements
                    if e.get("type") not in ("message_bubble", "time_divider")]
            elements = _chat_elements_from_slices(sliced, title) + kept
            page_dict["avatar_count"] = sliced.get("avatar_count", 0)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("v2.state_builder").exception(
                "chat_slicer failed, fallback to parse_chat elements")
    elif page.type == "wechat_miniapp_panel":
        # 小程序面板：只暴露 back（= 一次向下滑动，由操控层执行）
        elements.extend(_tab_elements(page.current_tab))
        actions = [{"action": "back", "description": "下滑返回首页"}]
        page_dict["current_tab"] = page.current_tab
    elif page.type in ("wechat_contacts", "wechat_discover", "wechat_me",
                       "wechat_settings", "wechat_add_friend", "wechat_profile",
                       "wechat_search", "wechat_generic_list", "wechat_popup_menu"):
        from .generic_parser import parse_generic_page
        icon_elements, special_elements = _collect_perception_elements(
            img, gray, hsv, page.type)
        elements, input_area, actions, extra = parse_generic_page(
            img, ocr_items, page.type, gray=gray, hsv=hsv,
            icon_elements=icon_elements, special_elements=special_elements)
        page_dict.update(extra)
        page_dict["path"] = f"wechat/{page.type.replace('wechat_', '')}"
    elif page.type == "wechat_moments":
        from .moments_parser import parse_moments
        elements, extra = parse_moments(img, ocr_items, gray=gray, hsv=hsv)
        page_dict.update(extra)
        page_dict["path"] = "wechat/moments"
        actions = [
            {"action": "back", "description": "返回上一页"},
            {"action": "scroll_up", "description": "向上滚动时间线"},
            {"action": "scroll_down", "description": "向下滚动时间线"},
        ]
    elif page.type == "wechat_scan":
        from .scan_parser import parse_scan
        elements, extra = parse_scan(img, ocr_items, gray=gray, hsv=hsv)
        page_dict.update(extra)
        page_dict["path"] = "wechat/scan"
        actions = [
            {"action": "back", "description": "关闭扫一扫"},
            {"action": "tap_flashlight", "description": "切换手电筒"},
            {"action": "show_my_qr", "description": "显示我的二维码"},
        ]
    elif page.type == "wechat_dialog":
        from .dialog_parser import parse_dialog
        elements, extra = parse_dialog(img, ocr_items=ocr_items, gray=gray, hsv=hsv)
        page_dict.update(extra)
        page_dict["path"] = "wechat/dialog"
        actions = [
            {"action": "tap", "target": btn.get("label") or btn.get("name"),
             "description": f"点击按钮：{btn.get('label') or btn.get('name')}"}
            for btn in elements if btn["type"] == "dialog_button"
        ]
        actions.append({"action": "back", "description": "返回/取消弹窗"})
    else:
        # contacts/discover/me/unknown：原始 OCR 文本兜底
        elements = [{
            "id": "raw_1", "type": "raw_ocr_text",
            "content": "\n".join(it["text"] for it in ocr_items
                                 if it["cy"] > LC.STATUS_BAR_BOTTOM),
            "position": {"x": 0, "y": LC.STATUS_BAR_BOTTOM, "w": w,
                         "h": h - LC.STATUS_BAR_BOTTOM},
        }]
        if page.type in ("wechat_contacts", "wechat_discover", "wechat_me"):
            elements.extend(_tab_elements(page.current_tab))
            page_dict["current_tab"] = page.current_tab
        actions = [{"action": "back", "description": "返回上一页"}]

    t_parse = time.perf_counter()
    if timing is not None:
        timing.update({
            "detect_ms": round((t_detect - t_ocr) * 1000, 1),
            "ocr_ms": round((t_ocr - t0) * 1000, 1),   # 含 gray/hsv 全图转换
            "parse_ms": round((t_parse - t_detect) * 1000, 1),
            "total_ms": round((t_parse - t0) * 1000, 1),
        })

    conf = round(float(np.mean([it["conf"] for it in ocr_items])), 3) \
        if ocr_items else 0.0
    state = {
        "meta": {
            "timestamp": datetime.datetime.now().astimezone().isoformat(),
            "device": "oneplus_6t",
            "resolution": f"{w}x{h}",
            "app": "com.tencent.mm",
            "source": source,
            "confidence": conf,
        },
        "page": page_dict,
        "elements": elements,
        "available_actions": actions,
    }
    if input_area is not None:
        state["input_area"] = input_area
    return state


def parse_screenshot(path, timing=None):
    img = cv2.imread(path)
    if img is None:
        raise FileNotFoundError(path)
    return build_state(img, source=path, timing=timing)


def main():
    if len(sys.argv) < 2:
        print("用法: python -m src.interaction.ports.android.perception.state_builder <截图路径> [--bench]",
              file=sys.stderr)
        sys.exit(1)
    timing = {} if "--bench" in sys.argv else None
    state = parse_screenshot(sys.argv[1], timing=timing)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if timing:
        print("--- benchmark ---", file=sys.stderr)
        for k, v in timing.items():
            print(f"  {k}: {v} ms", file=sys.stderr)


if __name__ == "__main__":
    main()
