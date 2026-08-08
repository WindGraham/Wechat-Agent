#!/usr/bin/env python3
"""moments_parser.py - v2 朋友圈时间线解析。

输入 BGR 截图 + OCR items，输出符合 V2_PERCEPTION_ARCH.md 的元素列表。
适配 OnePlus 6T (1080x2340, 深色模式) + 微信 8.0.76。

检测目标：
- 顶部封面区（含 "轻触更换封面" 提示）
- 个人头像/昵称（封面右下角）
- 返回按钮、相机按钮
- 时间线条目：头像、昵称、文字、图片/视频缩略图、时间、点赞/评论图标、详情按钮
"""

import json
import os
import re

import cv2
import numpy as np

from . import layout_consts as LC
from .img_utils import comps_from_mask, estimate_bg, rect_contains, x_overlap


# ---------------------------------------------------------------- 常量（朋友圈专用）
_MOMENTS_TITLE_Y1 = 200           # 标题栏下沿
_COVER_Y1 = 650                   # 封面区下沿（封面照片占标题栏下大片区域）
_PROFILE_ZONE_Y0 = 700            # 个人资料区起始
_PROFILE_ZONE_Y1 = 1000           # 个人资料区结束
_PROFILE_AVATAR_X0 = 850          # 个人头像左缘大致范围
_PROFILE_AVATAR_MIN = 110         # 个人头像边长
_PROFILE_AVATAR_MAX = 190

_TIMELINE_START_Y = 1000          # 时间线内容起始（个人资料之下）
_TIMELINE_AVATAR_COL = (15, 150)  # 时间线头像列
_TIMELINE_AVATAR_MIN = 70
_TIMELINE_AVATAR_MAX = 150

_LIKE_COMMENT_X0 = 920            # 点赞/评论图标在屏幕右侧
_DETAIL_TEXT = "详情"

_TIME_WORDS = {"昨天", "今天", "刚刚", "小时前", "分钟前", "天前", "周前", "月前"}
_TIME_RE = re.compile(r"^(\d+\s*(分钟|小时|天|周|月)前|刚刚)$")


# ---------------------------------------------------------------- OCR 归一化
def _normalize_ocr_items(ocr_items):
    """兼容内部 OCR dict（box 为元组）与样本 JSON dict（box 为四边形）。"""
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
                x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(xs)
                # y1 应该取 ys 最大
                y1 = max(ys)
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


def _icon_button(id_, name, cx, cy, r=40, verified=False):
    return _element(id_, "icon_button", name,
                    cx - r, cy - r, 2 * r, 2 * r,
                    confidence=0.7, verified=verified, actions=["tap"],
                    source="layout")


def _back_button(cx=50, cy=150, r=45):
    return _icon_button("btn_back", "返回", cx, cy, r, verified=True)


# ---------------------------------------------------------------- 头像检测
def _detect_avatars_in_zone(mask, img, x0, x1, y0, y1,
                            min_size, max_size, id_prefix,
                            use_saturation=False):
    """在指定列/行区域内检测方形头像。"""
    col = np.zeros_like(mask)
    col[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
    out = []
    for x, y, w, h, area in comps_from_mask(
            col, min_area=min_size * min_size // 2, close_ksize=5,
            min_w=min_size, min_h=min_size):
        if not (min_size <= w <= max_size and min_size <= h <= max_size
                and 0.7 < w / max(h, 1) < 1.4):
            continue
        # 头像通常不是纯色块
        crop = img[y:y + h, x:x + w]
        if crop.size > 0:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            if gray.var() < 80:
                continue
        out.append({
            "id": f"{id_prefix}_{len(out) + 1}",
            "x": x, "y": y, "w": w, "h": h,
            "area": area,
        })
    out.sort(key=lambda a: a["y"])
    return out


def detect_timeline_avatars(nonbg, img):
    """检测时间线左侧头像列。"""
    x0, x1 = _TIMELINE_AVATAR_COL
    return _detect_avatars_in_zone(
        nonbg, img, x0, x1, _TIMELINE_START_Y, LC.SCREEN_H,
        _TIMELINE_AVATAR_MIN, _TIMELINE_AVATAR_MAX, "avatar")


def detect_profile_avatar(hsv, img):
    """检测封面右下角的个人头像。

    个人头像常与昵称文字粘连成同一个非背景连通域，因此使用饱和度掩膜
    （彩色头像 vs 白色文字）更容易分离出独立头像区域。
    """
    sat = (hsv[:, :, 1] > 50).astype(np.uint8)
    return _detect_avatars_in_zone(
        sat, img, _PROFILE_AVATAR_X0, LC.SCREEN_W - 20,
        _PROFILE_ZONE_Y0, _PROFILE_ZONE_Y1,
        _PROFILE_AVATAR_MIN, _PROFILE_AVATAR_MAX, "profile_avatar")


# ---------------------------------------------------------------- 时间线条目切分
def _find_cover_text(ocr_items):
    for it in ocr_items:
        if "更换封面" in it["text"] or "轻触" in it["text"]:
            return it
    return None


def _looks_like_time(text):
    t = text.strip()
    if t in _TIME_WORDS:
        return True
    if _TIME_RE.match(t):
        return True
    return False


def _detect_image_blocks(img, nonbg, roi_y0, roi_y1, exclude_rects):
    """在内容区检测图片/视频缩略图块（排除头像和文字区域）。"""
    mask = np.zeros_like(nonbg)
    mask[roi_y0:roi_y1, 160:LC.SCREEN_W - 20] = \
        nonbg[roi_y0:roi_y1, 160:LC.SCREEN_W - 20]
    # 排除已知区域
    for x, y, w, h in exclude_rects:
        mask[y:y + h, x:x + w] = 0
    blocks = []
    for x, y, w, h, area in comps_from_mask(
            mask, min_area=8000, close_ksize=11,
            min_w=80, min_h=80):
        if w > 900 or h > 600:
            continue
        blocks.append((x, y, w, h, area))
    blocks.sort(key=lambda b: b[1])
    return blocks


def _bbox_from_items(items, pad=0):
    if not items:
        return None
    x0 = min(it["box"][0] for it in items) - pad
    y0 = min(it["box"][1] for it in items) - pad
    x1 = max(it["box"][2] for it in items) + pad
    y1 = max(it["box"][3] for it in items) + pad
    return (max(0, int(x0)), max(0, int(y0)),
            min(LC.SCREEN_W, int(x1 - x0)), min(LC.SCREEN_H, int(y1 - y0)))


def _inside_rects(cx, cy, rects):
    """点是否落在任一矩形内（含边）。"""
    for x, y, w, h in rects:
        if x <= cx <= x + w and y <= cy <= y + h:
            return True
    return False


# ---------------------------------------------------------------- 主入口
def parse_moments(img, ocr_items, gray=None, hsv=None):
    """返回 (elements, page_extra)。

    elements 符合 V2_PERCEPTION_ARCH.md 元素 schema，包含：
    - cover_image / cover_hint
    - profile_avatar / profile_nickname
    - btn_back / btn_camera
    - timeline 条目：avatar、nickname、text、image、time、like/comment icons
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    items = _normalize_ocr_items(ocr_items)
    bg = estimate_bg(gray, 0, LC.SCREEN_H)
    intense = img.max(axis=2)
    nonbg = ((intense > bg + 11) | (hsv[:, :, 1] > 60)).astype(np.uint8)

    elements = []
    page_extra = {"cover_visible": False, "timeline_item_count": 0}

    # ---- 顶部固定按钮
    elements.append(_back_button())
    elements.append(_icon_button("btn_camera", "相机", 1015, 150, r=45,
                                 verified=True))

    # ---- 封面区与提示
    cover_hint = _find_cover_text(items)
    if cover_hint is not None:
        page_extra["cover_visible"] = True
        bx0, by0, bx1, by1 = cover_hint["box"]
        # 封面提示文字
        elements.append(_element(
            "cover_hint", "text", "轻触更换封面",
            bx0, by0, bx1 - bx0, by1 - by0,
            label=cover_hint["text"],
            confidence=cover_hint["conf"], verified=True,
            source="ocr"))
        # 封面图片区域：文字上方到标题栏下方
        cover_y1 = int(by0)
        elements.append(_element(
            "cover_image", "image", "封面",
            0, _MOMENTS_TITLE_Y1, LC.SCREEN_W,
            max(0, cover_y1 - _MOMENTS_TITLE_Y1),
            confidence=0.7, verified=True, actions=["tap"],
            source="layout"))

    # ---- 个人资料区
    profile_avatars = detect_profile_avatar(hsv, img)
    if profile_avatars:
        pa = profile_avatars[0]
        elements.append(_element(
            pa["id"], "avatar", "我的头像",
            pa["x"], pa["y"], pa["w"], pa["h"],
            confidence=0.8, verified=True, actions=["tap"],
            source="geometry"))
        # 昵称在头像左侧或下方
        best_name = None
        best_score = 1e9
        for it in items:
            if not (_PROFILE_ZONE_Y0 <= it["cy"] <= _PROFILE_ZONE_Y1):
                continue
            left_of_avatar = it["cx"] < pa["x"]
            below_avatar = abs(it["cx"] - (pa["x"] + pa["w"] / 2)) < 120 \
                and it["cy"] > pa["y"] + pa["h"] / 2
            if not (left_of_avatar or below_avatar):
                continue
            score = abs(it["cy"] - pa["y"]) + abs(it["cx"] - pa["x"]) / 3
            if score < best_score:
                best_score = score
                best_name = it
        if best_name:
            bx0, by0, bx1, by1 = best_name["box"]
            elements.append(_element(
                "profile_nickname", "text", "昵称",
                bx0, by0, bx1 - bx0, by1 - by0,
                label=best_name["text"],
                confidence=best_name["conf"], verified=True,
                source="ocr"))

    # ---- 时间线条目
    timeline_avatars = detect_timeline_avatars(nonbg, img)
    timeline_avatars.sort(key=lambda a: a["y"])
    # 每个条目的垂直范围：当前头像顶部 -> 下一个头像顶部（或屏幕底）
    item_spans = []
    for i, a in enumerate(timeline_avatars):
        y0 = a["y"]
        y1 = timeline_avatars[i + 1]["y"] if i + 1 < len(timeline_avatars) \
            else LC.SCREEN_H - 80
        item_spans.append((y0, y1))

    # 详情按钮单独提取
    detail_items = [it for it in items if _DETAIL_TEXT in it["text"]]
    non_detail_items = [it for it in items if _DETAIL_TEXT not in it["text"]]

    # 个人资料区文本：避免被误分到时间线
    profile_zone_items = [
        it for it in non_detail_items
        if _PROFILE_ZONE_Y0 <= it["cy"] <= _PROFILE_ZONE_Y1
        and it["cx"] > 600
    ]
    cand_items = [
        it for it in non_detail_items
        if it["cy"] > _MOMENTS_TITLE_Y1
        and it not in profile_zone_items
    ]

    # 按垂直范围分组 OCR items
    groups = [[] for _ in timeline_avatars]
    for it in cand_items:
        cy = it["cy"]
        best_i = None
        best_dy = 1e9
        for i, (y0, y1) in enumerate(item_spans):
            if y0 - 40 <= cy <= y1 + 40:
                dy = abs(cy - (y0 + y1) / 2)
                if dy < best_dy:
                    best_dy = dy
                    best_i = i
        if best_i is not None:
            groups[best_i].append(it)

    # 为每个头像分组图片块
    exclude_rects = [(a["x"], a["y"], a["w"], a["h"]) for a in timeline_avatars]
    if profile_avatars:
        pa = profile_avatars[0]
        exclude_rects.append((pa["x"], pa["y"], pa["w"], pa["h"]))
    image_blocks = _detect_image_blocks(
        img, nonbg, _TIMELINE_START_Y, LC.SCREEN_H - 200, exclude_rects)
    avatar_image_groups = [[] for _ in timeline_avatars]
    for blk in image_blocks:
        cy = blk[1] + blk[3] / 2
        best_i = None
        best_dy = 1e9
        for i, (y0, y1) in enumerate(item_spans):
            if y0 - 40 <= cy <= y1 + 40:
                dy = abs(cy - (y0 + y1) / 2)
                if dy < best_dy:
                    best_dy = dy
                    best_i = i
        if best_i is not None:
            avatar_image_groups[best_i].append(blk)

    for idx, (avatar, (span_y0, span_y1)) in enumerate(
            zip(timeline_avatars, item_spans), 1):
        group = groups[idx - 1]

        # 头像
        elements.append(_element(
            f"t{idx}_avatar", "avatar", "头像",
            avatar["x"], avatar["y"], avatar["w"], avatar["h"],
            confidence=0.8, verified=True, actions=["tap", "long_press"],
            source="geometry"))

        # 昵称：头像右侧、y 接近头像顶部
        nick_cands = [
            it for it in group
            if it["cx"] > avatar["x"] + avatar["w"]
            and abs(it["cy"] - (avatar["y"] + avatar["h"] * 0.35)) < 80
            and not _looks_like_time(it["text"])
        ]
        nick = None
        if nick_cands:
            nick = min(nick_cands, key=lambda it: it["cx"])
            bx0, by0, bx1, by1 = nick["box"]
            elements.append(_element(
                f"t{idx}_nickname", "text", "昵称",
                bx0, by0, bx1 - bx0, by1 - by0,
                label=nick["text"],
                confidence=nick["conf"], verified=True, source="ocr"))

        # 时间
        time_items = [it for it in group if _looks_like_time(it["text"])]
        if time_items:
            t = min(time_items, key=lambda it: it["cy"])
            bx0, by0, bx1, by1 = t["box"]
            elements.append(_element(
                f"t{idx}_time", "text", "时间",
                bx0, by0, bx1 - bx0, by1 - by0,
                label=t["text"],
                confidence=t["conf"], verified=True, source="ocr"))
        else:
            t = None

        # 图片/视频块（先收集，用于过滤落在图上的 OCR 噪声）
        item_image_blocks = avatar_image_groups[idx - 1]
        image_rects = [(b[0], b[1], b[2], b[3]) for b in item_image_blocks]

        # 点赞/评论图标：右侧 "●" 或 "..."
        icon_items = [
            it for it in group
            if it["cx"] > _LIKE_COMMENT_X0
            and ("●" in it["text"] or "..." in it["text"]
                 or it["text"] in ("赞", "评论"))
        ]
        if icon_items:
            bbox = _bbox_from_items(icon_items, pad=20)
            elements.append(_element(
                f"t{idx}_like_comment", "icon_button", "点赞/评论",
                *bbox, label="赞/评论",
                confidence=0.65, verified=True, actions=["tap"],
                source="ocr"))
        else:
            time_y = t["cy"] if t else avatar["y"] + 120
            elements.append(_element(
                f"t{idx}_like_comment", "icon_button", "点赞/评论",
                940, int(time_y) - 30, 100, 60,
                label="赞/评论", confidence=0.5, verified=False,
                actions=["tap"], source="layout"))

        # 正文：昵称下方、排除时间/图标/图片上的文字
        text_items = [
            it for it in group
            if it["cx"] > avatar["x"] + avatar["w"]
            and not _looks_like_time(it["text"])
            and it not in icon_items
            and (nick is None or it is not nick)
            and not _inside_rects(it["cx"], it["cy"], image_rects)
        ]
        if text_items:
            text_items.sort(key=lambda it: (it["cy"], it["cx"]))
            bbox = _bbox_from_items(text_items, pad=8)
            text = "\n".join(it["text"] for it in text_items)
            elements.append(_element(
                f"t{idx}_text", "text", "正文",
                *bbox, label=text,
                confidence=round(sum(it["conf"] for it in text_items) / len(text_items), 3),
                verified=True, source="ocr"))

        # 图片/视频块元素化
        for bidx, blk in enumerate(item_image_blocks, 1):
            x, y, w, h, area = blk
            elements.append(_element(
                f"t{idx}_image_{bidx}", "image", "图片",
                x, y, w, h,
                confidence=0.75, verified=True, actions=["tap", "long_press"],
                source="geometry"))

    # 详情按钮
    for dit in detail_items:
        bx0, by0, bx1, by1 = dit["box"]
        elements.append(_element(
            "btn_detail", "icon_button", "详情",
            bx0, by0, bx1 - bx0, by1 - by0,
            label="详情", confidence=dit["conf"], verified=True,
            actions=["tap"], source="ocr"))

    page_extra["timeline_item_count"] = len(timeline_avatars)
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
    path = "samples/ui_inventory/08_moments/moments_main_p1.png"
    if len(sys.argv) > 1:
        path = sys.argv[1]
    img = cv2.imread(path)
    if img is None:
        print(f"cannot read {path}", file=sys.stderr)
        sys.exit(1)
    ocr_items = _load_sample_ocr(path)
    elements, extra = parse_moments(img, ocr_items)
    print(json.dumps({
        "sample": path,
        "page_extra": extra,
        "elements": elements,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
