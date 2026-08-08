#!/usr/bin/env python3
"""list_parser.py - v2 通用列表项解析器。

把 OCR 文字片段按 y 坐标聚类成行，每行扩展为全宽可点击列表项，
供 contacts / discover / me / settings 等通用列表页复用。
"""

import re
from collections import defaultdict

from . import layout_consts as LC


# ---------------------------------------------------------------- 常量
_RIGHT_STATUS_RE = re.compile(
    r"(已设置|暂未开启|未开启|已开启|已关闭|未关闭|进行中|已完成|\d+%)"
)
# 快速滚动索引导航条：右侧单列单个大写/特殊字母（☆A/B/C...）
_INDEX_LETTER_RE = re.compile(r"^[A-Z]$")


def _normalize_ocr_items(ocr_items):
    """兼容内部 OCR dict（box/cx/cy/h/conf）和样本 JSON dict（bbox/center/score）。"""
    out = []
    for it in ocr_items:
        if "box" in it:
            box = it["box"]
            # 样本 JSON 中 box 是 [[x0,y0],[x1,y1],...] 四边形；内部是 (x0,y0,x1,y1)
            if isinstance(box[0], (list, tuple)):
                pts = box
                xs = [float(p[0]) for p in pts]
                ys = [float(p[1]) for p in pts]
                x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            else:
                x0, y0, x1, y1 = (float(v) for v in box)
            cx = it.get("cx", (x0 + x1) / 2)
            cy = it.get("cy", (y0 + y1) / 2)
            h = it.get("h", y1 - y0)
            conf = it.get("conf", it.get("score", 0.0))
            text = it.get("text", "")
        elif "bbox" in it:
            b = it["bbox"]
            x0, y0, w, h_ = float(b["x"]), float(b["y"]), float(b["w"]), float(b["h"])
            x1, y1 = x0 + w, y0 + h_
            c = it.get("center", {})
            cx = float(c.get("x", (x0 + x1) / 2))
            cy = float(c.get("y", (y0 + y1) / 2))
            h = h_
            conf = float(it.get("score", 0.0))
            text = it.get("text", "")
        else:
            continue
        out.append({
            "box": (x0, y0, x1, y1),
            "cx": cx, "cy": cy, "h": h,
            "text": text.strip(),
            "conf": conf,
        })
    return out


def _in_roi(it, roi):
    x0, y0, x1, y1 = roi
    return (x0 <= it["box"][0] and it["box"][2] <= x1
            and y0 <= it["cy"] <= y1)


def _is_noise_item(it):
    """过滤通讯录等页面的分段字母 / 右侧快速索引，避免与真实列表项合并。"""
    text = it["text"]
    # 左侧分段头（紧贴左边缘的单个大写字母，如 A/B/C）
    if it["box"][0] < 80 and it["box"][2] < 150 \
            and re.match(r"^[A-Z]$", text):
        return True
    # 右侧快速索引（x>1000 的单个大写/星标字母，如 A / ☆A）
    if it["box"][0] >= LC.SCREEN_W - 120 \
            and re.match(r"^([A-Z]|☆[A-Z])$", text):
        return True
    return False


# ---------------------------------------------------------------- 公开 API
def merge_ocr_fragments(ocr_items, content_roi=(0, LC.CONTENT_Y0,
                                                 LC.SCREEN_W, LC.TAB_BAR_Y0),
                        row_merge_threshold=12,
                        filter_noise=True):
    """对 ROI 内 OCR items 按 y 坐标聚类成行。

    返回 dict: {row_index: [items_sorted_by_x], ...}。
    """
    items = [it for it in _normalize_ocr_items(ocr_items)
             if _in_roi(it, content_roi)]
    if filter_noise:
        items = [it for it in items if not _is_noise_item(it)]
    if not items:
        return {}

    items.sort(key=lambda it: it["cy"])
    rows = defaultdict(list)
    row_idx = 0
    last_cy = None
    for it in items:
        if last_cy is None or abs(it["cy"] - last_cy) <= row_merge_threshold:
            pass
        else:
            row_idx += 1
        rows[row_idx].append(it)
        # 用当前行的平均 cy 作为后续比较基准，避免长行内漂移
        last_cy = sum(i["cy"] for i in rows[row_idx]) / len(rows[row_idx])

    for k in rows:
        rows[k].sort(key=lambda it: it["box"][0])
    return dict(rows)


def expand_to_full_row(bbox, margin_y=4):
    """把单行 bbox 扩展为全宽可点击行。

    bbox 格式固定为 (x, y, w, h)。返回 (0, y', SCREEN_W, h') 并裁剪到屏幕。
    """
    x, y, w, h = bbox
    y0 = max(0, int(y) - margin_y)
    y1 = min(LC.SCREEN_H, int(y) + int(h) + margin_y)
    return (0, y0, LC.SCREEN_W, y1 - y0)


def extract_right_status(row_items, img_gray=None):
    """检测行右侧状态文字，如 '已设置' / '暂未开启'。

    先从 row_items 最右文本中按正则匹配；img_gray 暂作接口预留，
    未来可加入区域 OCR 兜底。返回 str 或 None。
    """
    if not row_items:
        return None
    # 最右侧文字项
    rightmost = max(row_items, key=lambda it: it["box"][2])
    text = rightmost["text"]
    m = _RIGHT_STATUS_RE.search(text)
    if m:
        return m.group(1)
    return None


def _looks_like_index_letter(items):
    """右侧快速索引字母（单列单字，x>1000）不是列表项。"""
    if len(items) != 1:
        return False
    it = items[0]
    if it["box"][0] < LC.SCREEN_W - 120:
        return False
    return bool(_INDEX_LETTER_RE.match(it["text"]))


def _looks_like_section_header(items):
    """左侧单字母分段头（x<80，单个大写/特殊字母）不是列表项。"""
    if len(items) != 1:
        return False
    it = items[0]
    if it["box"][2] > 100:
        return False
    return bool(re.match(r"^[A-Z]$", it["text"]))


def _infer_subtype(page_type, row_items):
    """根据 page_type 和行内容推断列表项子类型。"""
    pt = (page_type or "").lower()

    # 菜单/弹窗
    if "menu" in pt or "popup" in pt or "dialog" in pt:
        return "menu_item"

    # 联系人页
    if "contact" in pt:
        return "contact_item"

    # 设置页
    if "setting" in pt or "me" in pt:
        return "setting_item"

    # discover 等保留通用列表项
    return "list_item"


def parse_list_items(ocr_items,
                     content_roi=(0, LC.CONTENT_Y0,
                                  LC.SCREEN_W, LC.TAB_BAR_Y0),
                     row_merge_threshold=12,
                     page_type=None,
                     img_gray=None,
                     filter_noise=True):
    """把 OCR 片段聚类成通用列表项元素。

    返回 list[element dict]，每个元素符合 V2 元素 schema：
    id, type, name, label, content, position, confidence, actions 等。
    """
    rows = merge_ocr_fragments(ocr_items, content_roi, row_merge_threshold,
                               filter_noise=filter_noise)
    elements = []

    for i, row_items in rows.items():
        if _looks_like_index_letter(row_items):
            continue
        if _looks_like_section_header(row_items):
            continue

        # 按 x 拼接为 label
        label = "".join(it["text"] for it in row_items)
        confs = [it["conf"] for it in row_items if it["conf"] > 0]
        confidence = round(sum(confs) / len(confs), 3) if confs else 0.0

        # bbox：取行内文字最小包围，再扩成全宽
        y0 = min(it["box"][1] for it in row_items)
        y1 = max(it["box"][3] for it in row_items)
        x0 = min(it["box"][0] for it in row_items)
        x1 = max(it["box"][2] for it in row_items)
        full = expand_to_full_row((x0, y0, x1 - x0, y1 - y0), margin_y=4)

        subtype = _infer_subtype(page_type, row_items)
        content = extract_right_status(row_items, img_gray)

        el = {
            "id": f"list_{i}",
            "type": subtype,
            "name": label,
            "label": label,
            "content": content,
            "position": {
                "x": full[0],
                "y": full[1],
                "w": full[2],
                "h": full[3],
            },
            "confidence": confidence,
            "verified": False,
            "actions": ["tap"],
            "state": None,
            "source": "ocr",
        }
        elements.append(el)
    return elements


# ---------------------------------------------------------------- 便捷入口（供 generic_parser 复用）
def parse_contacts_list(ocr_items, img_gray=None):
    """通讯录专用封装。"""
    return parse_list_items(ocr_items, page_type="wechat_contacts",
                            img_gray=img_gray)


def parse_settings_list(ocr_items, img_gray=None):
    """设置页专用封装。"""
    return parse_list_items(ocr_items, page_type="wechat_settings",
                            img_gray=img_gray)


# ---------------------------------------------------------------- 独立 sanity check
def _load_sample_ocr(path):
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


def _summarize(elements):
    lines = []
    for e in elements:
        pos = e["position"]
        content = f" content={e['content']}" if e["content"] else ""
        lines.append(
            f"  {e['id']:8s} {e['type']:14s} {e['name']!r} "
            f"bbox=({pos['x']},{pos['y']},{pos['w']},{pos['h']}) "
            f"conf={e['confidence']:.3f}{content}"
        )
    return "\n".join(lines)


def main():
    import sys
    samples = [
        ("samples/ui_inventory/05_contacts/contacts_main_v2.png",
         "wechat_contacts"),
        ("samples/ui_inventory/10_settings/settings_about_v2.png",
         "wechat_settings"),
    ]
    for path, page_type in samples:
        try:
            ocr_items = _load_sample_ocr(path)
        except Exception as e:
            print(f"[SKIP] {path}: {e}")
            continue
        elements = parse_list_items(ocr_items, page_type=page_type)
        print(f"\n=== {path} (page_type={page_type}) ===")
        print(f"OCR items: {len(ocr_items)} -> list elements: {len(elements)}")
        print(_summarize(elements))


if __name__ == "__main__":
    main()
