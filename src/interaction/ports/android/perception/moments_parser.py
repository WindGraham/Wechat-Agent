#!/usr/bin/env python3
"""moments_parser.py — v3 朋友圈时间线解析（头像锚定 + 颜色分割重写）。

真机标定环境：OnePlus 6T (1080x2340, 深色模式) + 微信 8.0.76。
样本：assets/samples/moments/（2026-08-12 真机实采 18 张）。

核心策略（对齐 chat_parser v2 的"头像列先行"骨架）：
1. **头像锚定条目**：左列饱和度掩膜检测头像（~105x92），按 y 排序，
   条目范围 = [avatar.y - NICK_DY, next_avatar.y - NICK_DY)；
2. **蓝/灰颜色分割**：朋友圈的语义信号在颜色里——
   昵称/回复对象/点赞人名/全文按钮 = 蓝色（HSV S>50），
   正文/评论内容/时间 = 灰白色（S<40）。行内列向分割可精确拆分
   "Leisure 回复Doo: 不可以" → 蓝段 Leisure / 蓝段 回复Doo / 灰段 不可以；
3. **两个点按钮几何指纹**：恰好 2 个 ~11x11 连通域、水平排列、间距 ~21px、
   x>900、与时间行同 cy。OCR "●" 只做粗定位候选，几何验证做最终判决
   （防评论内容里的 "●"/"。" 误判）；
4. **评论行解析**：文本模式（A回复B:内容 / A:内容）+ 颜色分割定 click_x
   （点灰色内容区回复，点蓝色昵称会跳个人资料页——实测事故）；
5. **跨屏标记**：partial_top / partial_bottom 供 moments_reader 拼接；
6. **页面状态**：评论输入框（"发表评论:"/"回复XXX:"）、赞/评论弹出菜单
   的识别供 moments_interactor 动作层使用。

产出两个 API：
  parse_moments()         → (elements, page_extra)   兼容旧契约
  parse_moments_entries() → (entries, page_extra)    结构化条目（拼接层用）
"""

import re

import cv2
import numpy as np

from . import layout_consts as LC
from .img_utils import comps_from_mask, rect_contains

# ---------------------------------------------------------------- 常量（2026-08-12 真机标定）
SCREEN_W, SCREEN_H = LC.SCREEN_W, LC.SCREEN_H

# 头像列（左列，饱和度掩膜）
AVATAR_COL_X0, AVATAR_COL_X1 = 30, 180
AVATAR_MIN_W, AVATAR_MAX_W = 60, 130
AVATAR_MIN_H, AVATAR_MAX_H = 60, 130
AVATAR_MIN_AREA = 4000
AVATAR_SAT_MIN = 50               # 头像饱和度阈值（彩色头像 vs 深色背景）
AVATAR_SCAN_Y0 = 200              # 标题栏之下开始扫
NICK_DY = 20                      # 昵称顶部 ≈ avatar.y + NICK_DY（切条目用）

# 正文/评论左缘
TEXT_LEFT = 185                   # 正文、昵称、时间、灰块的左缘 ≈ x190
TEXT_RIGHT = 1045                 # 正文右缘

# 蓝色文字（昵称/回复对象/点赞人名/全文按钮）
BLUE_S_MIN = 50                   # HSV 饱和度：蓝 S>50，灰 S<40（实测 Leisure 蓝 S=86）
BLUE_H_LO, BLUE_H_HI = 90, 130    # 微信蓝 H≈100-115（OpenCV 刻度）
FG_DELTA = 30                     # 文字前景 = 比局部背景亮 30+

# 时间行
TIME_RE = re.compile(
    r"^(\d+\s*(分钟|小时|天|周|月)前|刚刚|昨天|前天|\d+月\d+日|\d{4}年.*)$")

# 两个点按钮几何指纹（实测：2×(11x11) area≈95 间距21px @x983-1015）
DOTS_SCAN_X0, DOTS_SCAN_X1 = 930, 1050
DOT_MIN_W, DOT_MAX_W = 6, 18
DOT_MIN_H, DOT_MAX_H = 6, 18
DOT_MIN_AREA = 40
DOT_GAP_LO, DOT_GAP_HI = 10, 40   # 两点水平间距
DOT_DY_MAX = 8                    # 两点垂直差
DOTS_TIME_DY = 45                 # 与时间行 cy 差上限

# 评论灰块（点赞+评论区背景，实测 gray≈32 vs 背景 25）
BLOCK_LO, BLOCK_HI = 4, 16        # bg+4 ~ bg+16
BLOCK_MIN_W, BLOCK_MIN_H = 500, 60
BLOCK_MIN_AREA = 30000

# 全文按钮
FULLTEXT_WORDS = ("全文", "展开", "收起")
FULLTEXT_X_MAX = 420              # 在正文区左缘附近

# 评论文本模式
COMMENT_REPLY_RE = re.compile(r"^(?P<from>.+?)\s*回复\s*(?P<to>.+?)\s*[:：]\s*(?P<content>.*)$")
COMMENT_RE = re.compile(r"^(?P<from>.+?)\s*[:：]\s*(?P<content>.*)$")

# 心形点赞行（OCR 可能把心形识别成各种字符或漏掉）
LIKE_ROW_MARKS = ("♡", "❤", "♥")

# 底部输入区（评论输入框）
INPUT_HINT_RE = re.compile(r"^(发表评论|回复.+?)[:：]?\s*$")
SEND_BTN_WORD = "发送"

# 赞/评论弹出菜单
MENU_WORDS = ("赞", "取消", "评论")


# ================================================================ 工具
def _element(id_, type_, name, x, y, w, h,
             label=None, content=None, confidence=0.6, verified=False,
             actions=None, source="layout"):
    return {
        "id": id_, "type": type_, "name": name,
        "label": label, "content": content,
        "position": {"x": int(x), "y": int(y), "w": int(w), "h": int(h)},
        "outline": None,
        "confidence": float(confidence), "verified": bool(verified),
        "actions": list(actions or []), "source": source,
    }


def _normalize_ocr(ocr_items):
    """统一 OCR item 结构：box=(x0,y0,x1,y1) + cx/cy/text/conf/h。"""
    out = []
    for it in ocr_items or []:
        box = it.get("box")
        if box is None:
            continue
        if isinstance(box[0], (list, tuple)):
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        else:
            x0, y0, x1, y1 = (float(v) for v in box)
        cx = float(it.get("cx", (x0 + x1) / 2))
        cy = float(it.get("cy", (y0 + y1) / 2))
        out.append({
            "box": (x0, y0, x1, y1), "cx": cx, "cy": cy,
            "h": float(y1 - y0),
            "text": str(it.get("text", "")).strip(),
            "conf": float(it.get("conf", it.get("score", 0.0))),
        })
    return out


def _estimate_bg(gray):
    """直方图主峰估计背景灰度（深色模式主峰≈25）。
    用左缘窄条（x 2~26）而非全图：条目带大图/键盘弹起时，
    全图主峰会被照片灰度（~87）抢走，左缘永远纯背景。"""
    strip = gray[150:, 2:26]
    hist = np.bincount(strip.ravel(), minlength=256)
    return int(np.argmax(hist))


# ================================================================ 头像锚定
def find_timeline_avatars(hsv, img):
    """左列头像检测（饱和度掩膜 + 方差验证）。返回按 y 排序的头像列表。"""
    sat = (hsv[:, :, 1] > AVATAR_SAT_MIN).astype(np.uint8)
    col = np.zeros_like(sat)
    col[AVATAR_SCAN_Y0:SCREEN_H, AVATAR_COL_X0:AVATAR_COL_X1] = \
        sat[AVATAR_SCAN_Y0:SCREEN_H, AVATAR_COL_X0:AVATAR_COL_X1]
    out = []
    for x, y, w, h, area in comps_from_mask(
            col, min_area=AVATAR_MIN_AREA, close_ksize=5,
            min_w=AVATAR_MIN_W, min_h=AVATAR_MIN_H):
        if not (AVATAR_MIN_W <= w <= AVATAR_MAX_W
                and AVATAR_MIN_H <= h <= AVATAR_MAX_H
                and 0.75 < w / max(h, 1) < 1.35):
            continue
        # 头像不是纯色块
        crop = img[y:y + h, x + AVATAR_COL_X0:x + AVATAR_COL_X0 + w]
        if crop.size:
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            if g.var() < 80:
                continue
        out.append({"x": x + AVATAR_COL_X0, "y": y, "w": w, "h": h,
                    "area": area})
    out.sort(key=lambda a: a["y"])
    return out


# ================================================================ 两个点按钮
def _verify_dots_geometry(gray, cx, cy):
    """几何指纹验证：恰好 2 个 ~11x11 亮点，水平排列，间距 10~40。"""
    x0, y0 = max(0, cx - 45), max(0, cy - 30)
    x1, y1 = min(SCREEN_W, cx + 45), min(SCREEN_H, cy + 30)
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return False
    bg = np.median(crop)
    mask = (crop > bg + 40).astype(np.uint8)
    dots = []
    for x, y, w, h, area in comps_from_mask(
            mask, min_area=DOT_MIN_AREA, close_ksize=1,
            min_w=DOT_MIN_W, min_h=DOT_MIN_H):
        if DOT_MIN_W <= w <= DOT_MAX_W and DOT_MIN_H <= h <= DOT_MAX_H:
            dots.append((x + x0, y + y0, w, h))
    if len(dots) != 2:
        return False
    dots.sort()
    (ax, ay, _, _), (bx, by, _, _) = dots
    return (DOT_GAP_LO <= bx - ax <= DOT_GAP_HI
            and abs(ay - by) <= DOT_DY_MAX)


def find_dots_button(gray, ocr_items, time_cy, span=None):
    """两个点按钮：OCR '●' 粗定位 + 几何指纹验证；OCR 漏检则在
    时间行右侧固定扫描区直接几何检测。返回 {'cx','cy','verified'} 或 None。

    span=(y0,y1) 为条目垂直范围：候选必须落在条目内，防止没有
    时间行的 partial 条目抢到其他条目的按钮。"""
    def _in_span(cy):
        return span is None or (span[0] - 20 <= cy <= span[1] + 20)

    # 路径 1：OCR 候选
    cands = [it for it in ocr_items
             if "●" in it["text"] and it["cx"] > 900 and _in_span(it["cy"])]
    for c in cands:
        if time_cy is not None and abs(c["cy"] - time_cy) > DOTS_TIME_DY:
            continue
        if _verify_dots_geometry(gray, int(c["cx"]), int(c["cy"])):
            return {"cx": int(c["cx"]), "cy": int(c["cy"]), "verified": True}
    # 路径 2：固定扫描区（时间行右侧）
    if time_cy is not None:
        y0 = max(0, int(time_cy) - 40)
        y1 = min(SCREEN_H, int(time_cy) + 40)
        zone = gray[y0:y1, DOTS_SCAN_X0:DOTS_SCAN_X1]
        if zone.size:
            bg = np.median(zone)
            mask = (zone > bg + 40).astype(np.uint8)
            dots = []
            for x, y, w, h, area in comps_from_mask(
                    mask, min_area=DOT_MIN_AREA, close_ksize=1,
                    min_w=DOT_MIN_W, min_h=DOT_MIN_H):
                if DOT_MIN_W <= w <= DOT_MAX_W and DOT_MIN_H <= h <= DOT_MAX_H:
                    dots.append((x + DOTS_SCAN_X0, y + y0, w, h))
            if len(dots) == 2:
                dots.sort()
                (ax, ay, _, _), (bx, by, _, _) = dots
                if (DOT_GAP_LO <= bx - ax <= DOT_GAP_HI
                        and abs(ay - by) <= DOT_DY_MAX):
                    return {"cx": (ax + bx) // 2, "cy": (ay + by) // 2,
                            "verified": True}
    return None


# ================================================================ 颜色分割
def _blue_mask(hsv_crop, fg):
    """蓝色文字像素：高饱和 + 微信蓝色相（H 90~130）。
    emoji（黄/红，H≈10~30）也是高饱和，必须靠色相排除，
    否则评论尾的 😊 会被误判成蓝段。"""
    h = hsv_crop[:, :, 0]
    s = hsv_crop[:, :, 1]
    return (s > BLUE_S_MIN) & (h >= BLUE_H_LO) & (h <= BLUE_H_HI) & fg


def _text_fg_mask(gray_crop, hsv_crop):
    """文字前景掩膜 + 蓝色像素掩膜。"""
    bg = np.median(gray_crop)
    fg = gray_crop > bg + FG_DELTA
    blue = _blue_mask(hsv_crop, fg)
    return fg, blue


def split_comment_row(img, gray, hsv, box):
    """评论行内列向蓝/灰分割。

    返回 {'blue_ranges': [(x0,x1)...], 'gray_ranges': [...],
          'content_x0': int|None}（绝对坐标）。
    content_x0 = 第一个灰色段左缘（回复点击安全位）。"""
    x0, y0, x1, y1 = (int(v) for v in box)
    pad = 6
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(SCREEN_W, x1 + pad), min(SCREEN_H, y1 + pad)
    g = gray[y0:y1, x0:x1]
    hv = hsv[y0:y1, x0:x1]
    if g.size == 0:
        return {"blue_ranges": [], "gray_ranges": [], "content_x0": None}
    fg, blue = _text_fg_mask(g, hv)
    # 列向投影
    col_fg = fg.sum(axis=0)
    col_blue = blue.sum(axis=0)
    W = g.shape[1]
    # 逐列标记：F=无文字 B=蓝 G=灰；2px 容差桥接
    tags = []
    for x in range(W):
        if col_fg[x] < 3:
            tags.append("F")
        elif col_blue[x] > max(2, col_fg[x] * 0.5):
            tags.append("B")
        else:
            tags.append("G")
    # 桥接短空隙（<=3px 的 F 在同类包围中并入）
    for i in range(2, len(tags) - 2):
        if tags[i] == "F" and tags[i - 1] == tags[i + 1] != "F":
            tags[i] = tags[i - 1]
    # 收敛为段
    ranges = []
    cur, start = None, 0
    for i, t in enumerate(tags + ["F"]):
        if t != cur:
            if cur in ("B", "G") and i - start >= 6:
                ranges.append((start + x0, i + x0, cur))
            cur, start = t, i
    blue_ranges = [(a, b) for a, b, t in ranges if t == "B"]
    gray_ranges = [(a, b) for a, b, t in ranges if t == "G"]
    # 点击位 = 最后一个蓝段之后的第一个灰段左缘。
    # 蓝段 = 昵称/回复对象（点了跳资料页），内容区一定在最后蓝段右侧；
    # 取"第一个灰段"会把昵称字符间隙当成内容区，点到昵称尾巴上。
    if blue_ranges:
        last_blue_end = max(b for _, b in blue_ranges)
        after = [r for r in gray_ranges if r[0] >= last_blue_end - 2]
        content_x0 = after[0][0] if after else None
    else:
        content_x0 = gray_ranges[0][0] if gray_ranges else None
    return {"blue_ranges": blue_ranges, "gray_ranges": gray_ranges,
            "content_x0": content_x0}


# ================================================================ 评论解析
def _has_emoji_pixels(gray, hsv, box, after_x):
    """检测行内 after_x 之后是否有彩色 emoji 像素。
    emoji 高饱和但色相不在蓝色范围（黄/红脸 H≈10~30），
    蓝昵称/灰文字都不会命中。"""
    x0, y0, x1, y1 = (int(v) for v in box)
    x0 = max(int(after_x), x0)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return False
    hv = hsv[y0:y1, x0:x1]
    g = gray[y0:y1, x0:x1]
    if g.size == 0:
        return False
    bg = np.median(g)
    fg = g > bg + FG_DELTA
    colorful = (hv[:, :, 1] > BLUE_S_MIN) & fg & ~_blue_mask(hv, fg)
    return int(colorful.sum()) >= 30


def parse_comment_row(item, img, gray, hsv):
    """解析一条评论 OCR item → 结构化评论 dict（未匹配返回 None）。"""
    text = item["text"]
    m = COMMENT_REPLY_RE.match(text)
    if m:
        from_user = m.group("from").strip()
        reply_to = m.group("to").strip()
        content = m.group("content").strip()
    else:
        m = COMMENT_RE.match(text)
        if not m:
            return None
        from_user = m.group("from").strip()
        reply_to = None
        content = m.group("content").strip()
    if not from_user:
        return None
    seg = split_comment_row(img, gray, hsv, item["box"])
    x0, y0, x1, y1 = item["box"]
    # OCR 不识别 emoji：内容为空但冒号后有彩色像素 → 标注 [表情]
    if not content and seg["blue_ranges"]:
        tail_x = seg["blue_ranges"][-1][1]
        if _has_emoji_pixels(gray, hsv, item["box"], tail_x):
            content = "[表情]"
    if seg["content_x0"]:
        click_x = seg["content_x0"]
    elif seg["blue_ranges"]:
        click_x = seg["blue_ranges"][-1][1] + 15
    else:
        click_x = int((x0 + x1) / 2)
    return {
        "from_user": from_user,
        "reply_to": reply_to,
        "content": content,
        "raw_text": text,
        "y0": int(y0), "y1": int(y1),
        "click_x": int(click_x),
        "click_y": int(item["cy"]),
        "blue_ranges": seg["blue_ranges"],
        "conf": item["conf"],
    }


def is_like_row(text):
    """点赞行判定：不含冒号（名字,逗号 列表），或含心形字符。"""
    if any(m in text for m in LIKE_ROW_MARKS):
        return True
    return (":" not in text) and ("：" not in text)


def parse_like_names(text):
    """点赞行 → 人名列表。去掉心形字符，按中英文逗号/空格切分。"""
    t = text
    for m in LIKE_ROW_MARKS:
        t = t.replace(m, " ")
    t = re.sub(r"[,，、]", ",", t)
    names = [n.strip() for n in t.split(",") if n.strip()]
    return names


# ================================================================ 灰块（点赞+评论区）
def find_gray_blocks(gray, bg):
    """检测点赞/评论区的浅灰背景块。"""
    mask = ((gray >= bg + BLOCK_LO) & (gray <= bg + BLOCK_HI)).astype(np.uint8)
    mask[:300, :] = 0
    mask[:, :170] = 0
    mask[:, 1050:] = 0
    out = []
    for x, y, w, h, area in comps_from_mask(
            mask, min_area=BLOCK_MIN_AREA, close_ksize=15,
            min_w=BLOCK_MIN_W, min_h=BLOCK_MIN_H):
        out.append({"x": x, "y": y, "w": w, "h": h, "area": area})
    out.sort(key=lambda b: b["y"])
    return out


# ================================================================ 页面状态（动作层用）
def detect_comment_input(ocr_items):
    """底部评论输入框：'发表评论:' / '回复XXX:'。返回状态 dict 或 None。"""
    for it in ocr_items:
        if it["cy"] < 1400:
            continue
        m = INPUT_HINT_RE.match(it["text"])
        if m:
            kind = "reply" if it["text"].startswith("回复") else "comment"
            reply_to = None
            if kind == "reply":
                reply_to = re.sub(r"^回复|[:：]\s*$", "", it["text"]).strip()
            send = None
            for jt in ocr_items:
                if jt["text"] == SEND_BTN_WORD and jt["cx"] > 850 \
                        and abs(jt["cy"] - it["cy"]) < 60:
                    send = {"cx": int(jt["cx"]), "cy": int(jt["cy"])}
                    break
            return {
                "active": True, "kind": kind, "reply_to": reply_to,
                "input_cx": int(it["cx"]), "input_cy": int(it["cy"]),
                "send_btn": send,
            }
    return None


def detect_action_menu(ocr_items):
    """赞/评论弹出菜单：同行存在 '评论' + ('赞'|'取消')。
    返回 {'like': (cx,cy), 'comment': (cx,cy), 'liked': bool} 或 None。"""
    like = comment = None
    liked = False
    for it in ocr_items:
        if it["text"] == "评论":
            comment = (int(it["cx"]), int(it["cy"]))
        elif it["text"] == "赞":
            like = (int(it["cx"]), int(it["cy"]))
        elif it["text"] == "取消":
            like = (int(it["cx"]), int(it["cy"]))
            liked = True
    if comment and like and abs(comment[1] - like[1]) < 60:
        return {"like": like, "comment": comment, "liked": liked}
    return None


# ================================================================ 条目解析
def _items_in(items, y0, y1, x0=0, x1=SCREEN_W):
    return [it for it in items
            if y0 <= it["cy"] <= y1 and x0 <= it["cx"] <= x1]


def _looks_like_time(text):
    return bool(TIME_RE.match(text.strip()))


def _is_blue_at(hsv, gray, item):
    """OCR item 的文字主色是否为蓝。"""
    x0, y0, x1, y1 = (int(v) for v in item["box"])
    g = gray[y0:y1, x0:x1]
    hv = hsv[y0:y1, x0:x1]
    if g.size == 0:
        return False
    fg, blue = _text_fg_mask(g, hv)
    if fg.sum() < 10:
        return False
    return blue.sum() / fg.sum() > 0.5


def parse_entry(avatar, span, items, img, gray, hsv, idx):
    """解析单个条目。span=(y0,y1) 条目垂直范围。"""
    y0, y1 = span
    ax, ay, aw, ah = avatar["x"], avatar["y"], avatar["w"], avatar["h"]
    group = _items_in(items, y0, y1)

    entry = {
        "idx": idx,
        "avatar": dict(avatar),
        "nickname": None,
        "text": "",
        "text_items": [],
        "time": None,
        "time_cy": None,
        "dots": None,
        "fulltext_btn": None,
        "likes": [],
        "comments": [],
        "images": [],
        "partial_top": y0 <= AVATAR_SCAN_Y0 + 2,
        "partial_bottom": y1 >= SCREEN_H - 250,
        "complete": False,
    }

    # ---- 昵称：蓝色、头像右侧、y 在头像上部
    for it in group:
        if it["cx"] <= ax + aw:
            continue
        if not (ay - 30 <= it["cy"] <= ay + ah * 0.6 + 40):
            continue
        if _looks_like_time(it["text"]):
            continue
        if _is_blue_at(hsv, gray, it):
            entry["nickname"] = it["text"]
            entry["nickname_item"] = it
            break

    # ---- 时间行
    time_item = None
    for it in group:
        if it["cx"] < TEXT_LEFT + 100 and _looks_like_time(it["text"]):
            if time_item is None or it["cy"] < time_item["cy"]:
                time_item = it
    if time_item is not None:
        entry["time"] = time_item["text"]
        entry["time_cy"] = int(time_item["cy"])

    # ---- 全文按钮：蓝色 "全文/展开/收起"，正文区左缘
    for it in group:
        if it["text"] in FULLTEXT_WORDS and it["cx"] < FULLTEXT_X_MAX:
            if _is_blue_at(hsv, gray, it):
                entry["fulltext_btn"] = {
                    "text": it["text"],
                    "cx": int(it["cx"]), "cy": int(it["cy"]),
                }
                break

    # ---- 两个点按钮（与时间行对齐，且必须在本条目范围内）
    entry["dots"] = find_dots_button(gray, items, entry["time_cy"],
                                     span=(y0, y1))

    # ---- 正文：昵称下方 → 时间行/灰块/条目底 之间的非蓝文字
    nick_cy = entry["nickname_item"]["cy"] if entry.get("nickname_item") \
        else ay + ah * 0.35
    text_bottom = entry["time_cy"] - 10 if entry["time_cy"] else y1
    text_items = []
    for it in group:
        if it["cx"] < TEXT_LEFT:
            continue
        if not (nick_cy + 30 < it["cy"] < text_bottom):
            continue
        if it["text"] in FULLTEXT_WORDS:
            continue
        text_items.append(it)
    text_items.sort(key=lambda it: (it["cy"], it["cx"]))
    entry["text_items"] = text_items
    entry["text"] = "\n".join(it["text"] for it in text_items)

    return entry


def attach_block(entry, block, items, img, gray, hsv):
    """把点赞/评论灰块挂到条目上，解析点赞行与评论行。"""
    bx0, by0 = block["x"], block["y"]
    bx1, by1 = bx0 + block["w"], by0 + block["h"]
    rows = _items_in(items, by0 - 10, by1 + 10, bx0 - 10, bx1 + 10)
    rows.sort(key=lambda it: (it["cy"], it["cx"]))

    like_names = []
    comments = []
    comment_started = False
    for it in rows:
        t = it["text"]
        if not t:
            continue
        if not comment_started and is_like_row(t):
            like_names.extend(parse_like_names(t))
        else:
            comment_started = True
            c = parse_comment_row(it, img, gray, hsv)
            if c is not None:
                comments.append(c)
    entry["likes"] = like_names
    entry["comments"] = comments
    entry["block"] = block


# ================================================================ 孤儿段（跨屏拼接用）
ORPHAN_Y0 = 210                   # 标题栏之下才算孤儿内容


def parse_top_orphan(img, ocr_items, first_y, gray=None, hsv=None, bg=None):
    """解析第一个头像之上的孤儿内容（属于上屏底部被截断的条目）。

    滚动到条目中段时，当前屏顶部残留的是上屏条目的正文尾巴/时间行/
    两个点/点赞/评论——它们没有头像锚点，parse_moments_entries 会丢弃。
    first_y = 第一条目 span 顶（avatar.y - NICK_DY - 10）；无条目时传 SCREEN_H。

    返回 {'text_items': [...], 'comments': [...], 'likes': [...],
          'time': str|None, 'time_cy': int|None, 'dots': dict|None}"""
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    if bg is None:
        bg = _estimate_bg(gray)
    items = _normalize_ocr(ocr_items)

    result = {"text_items": [], "comments": [], "likes": [],
              "time": None, "time_cy": None, "dots": None}
    zone = [it for it in items
            if ORPHAN_Y0 < it["cy"] < first_y and it["cx"] > TEXT_LEFT - 20]
    if not zone:
        return result

    # 灰块中心在 first_y 之上的 → 孤儿评论/点赞区
    blocks = [b for b in find_gray_blocks(gray, bg)
              if b["y"] + b["h"] / 2 < first_y]

    def _in_block(it):
        return any(b["y"] - 10 <= it["cy"] <= b["y"] + b["h"] + 10
                   for b in blocks)

    comment_started = False
    for it in sorted(zone, key=lambda t: (t["cy"], t["cx"])):
        t = it["text"]
        if not t:
            continue
        if _in_block(it):
            if not comment_started and is_like_row(t):
                result["likes"].extend(parse_like_names(t))
            else:
                comment_started = True
                c = parse_comment_row(it, img, gray, hsv)
                if c is not None:
                    result["comments"].append(c)
        elif _looks_like_time(t):
            result["time"] = t
            result["time_cy"] = int(it["cy"])
        elif t in FULLTEXT_WORDS:
            continue                    # 展开后的"收起"按钮不是正文
        elif t.strip() in ("●", "•", "·") and it["cx"] > 900:
            continue                    # 两个点按钮的 OCR 残片，不是正文
        else:
            result["text_items"].append(it)
    if result["time_cy"] is not None:
        result["dots"] = find_dots_button(
            gray, items, result["time_cy"], span=(ORPHAN_Y0, first_y))
    return result


# ================================================================ 主入口
def parse_moments_entries(img, ocr_items, gray=None, hsv=None):
    """结构化解析朋友圈 feed 当前屏。

    返回 (entries, page_extra)：
      entries: [entry] 条目列表（含 comments/likes/partial 标记）
      page_extra: {'comment_input': ..., 'action_menu': ..., ...}
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    items = _normalize_ocr(ocr_items)
    bg = _estimate_bg(gray)

    # 页面状态（动作层）
    comment_input = detect_comment_input(items)
    action_menu = detect_action_menu(items)

    # 头像锚定 → 条目切分
    avatars = find_timeline_avatars(hsv, img)
    entries = []
    for i, av in enumerate(avatars):
        y0 = av["y"] - NICK_DY - 10
        y1 = (avatars[i + 1]["y"] - NICK_DY - 10) if i + 1 < len(avatars) \
            else SCREEN_H
        entries.append((av, (y0, y1)))

    result = []
    for idx, (av, span) in enumerate(entries, 1):
        e = parse_entry(av, span, items, img, gray, hsv, idx)
        result.append(e)

    # 灰块挂接到条目（按 y 归属）
    blocks = find_gray_blocks(gray, bg)
    for blk in blocks:
        bcx, bcy = blk["x"] + blk["w"] / 2, blk["y"] + blk["h"] / 2
        best = None
        for e in result:
            av = e["avatar"]
            y0 = av["y"] - NICK_DY - 10
            # 条目底 = 下一条目顶 or 屏底
            y1 = y0 + 100000
            for e2 in result:
                if e2["avatar"]["y"] > av["y"]:
                    y1 = min(y1, e2["avatar"]["y"] - NICK_DY - 10)
            if y0 <= bcy <= y1:
                if best is None or av["y"] > best["avatar"]["y"]:
                    best = e
        if best is not None:
            attach_block(best, blk, items, img, gray, hsv)

    # 完整性：见到时间行 + 两个点 = 正文完整；有下一个头像 = 评论完整
    for i, e in enumerate(result):
        e["text_complete"] = e["time"] is not None and e["dots"] is not None
        e["complete"] = e["text_complete"] and (i + 1 < len(result)
                                                or not e["partial_bottom"])

    page_extra = {
        "entry_count": len(result),
        "comment_input": comment_input,
        "action_menu": action_menu,
        "bg": bg,
    }
    return result, page_extra


# ---------------------------------------------------------------- 兼容旧契约
def parse_moments(img, ocr_items, gray=None, hsv=None):
    """兼容旧 API：返回 (elements, page_extra)。"""
    entries, page_extra = parse_moments_entries(img, ocr_items,
                                                gray=gray, hsv=hsv)
    elements = []
    elements.append(_element("btn_back", "icon_button", "返回",
                             5, 105, 90, 90, verified=True, actions=["tap"]))
    elements.append(_element("btn_camera", "icon_button", "相机",
                             970, 105, 90, 90, verified=True, actions=["tap"]))

    for e in entries:
        i = e["idx"]
        av = e["avatar"]
        elements.append(_element(
            f"t{i}_avatar", "avatar", "头像", av["x"], av["y"], av["w"], av["h"],
            confidence=0.8, verified=True, actions=["tap", "long_press"],
            source="geometry"))
        if e.get("nickname_item"):
            it = e["nickname_item"]
            x0, y0, x1, y1 = it["box"]
            elements.append(_element(
                f"t{i}_nickname", "text", "昵称",
                x0, y0, x1 - x0, y1 - y0, label=e["nickname"],
                confidence=it["conf"], verified=True, source="ocr"))
        if e["text_items"]:
            bx0 = min(it["box"][0] for it in e["text_items"])
            by0 = min(it["box"][1] for it in e["text_items"])
            bx1 = max(it["box"][2] for it in e["text_items"])
            by1 = max(it["box"][3] for it in e["text_items"])
            elements.append(_element(
                f"t{i}_text", "text", "正文", bx0, by0, bx1 - bx0, by1 - by0,
                label=e["text"], confidence=0.9, verified=True, source="ocr"))
        if e["time"]:
            elements.append(_element(
                f"t{i}_time", "text", "时间",
                TEXT_LEFT, e["time_cy"] - 25, 300, 50,
                label=e["time"], confidence=0.9, verified=True, source="ocr"))
        if e["dots"]:
            d = e["dots"]
            elements.append(_element(
                f"t{i}_dots", "icon_button", "两个点",
                d["cx"] - 50, d["cy"] - 40, 100, 80, label="赞/评论",
                confidence=0.95, verified=d["verified"], actions=["tap"],
                source="geometry"))
        if e["fulltext_btn"]:
            fb = e["fulltext_btn"]
            elements.append(_element(
                f"t{i}_fulltext", "text", "全文按钮",
                fb["cx"] - 60, fb["cy"] - 30, 120, 60, label=fb["text"],
                confidence=0.9, verified=True, actions=["tap"], source="ocr"))
        for j, c in enumerate(e["comments"], 1):
            elements.append(_element(
                f"t{i}_comment{j}", "text", "评论",
                TEXT_LEFT, c["y0"], SCREEN_W - TEXT_LEFT - 30,
                c["y1"] - c["y0"],
                label=c["raw_text"], content=c,
                confidence=c["conf"], verified=True,
                actions=["tap"], source="ocr"))
        if e["likes"]:
            elements.append(_element(
                f"t{i}_likes", "text", "点赞列表",
                TEXT_LEFT, 0, 800, 40,
                label=",".join(e["likes"]), content=e["likes"],
                confidence=0.8, verified=True, source="ocr"))

    page_extra["timeline_item_count"] = len(entries)
    return elements, page_extra
