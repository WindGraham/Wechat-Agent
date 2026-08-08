#!/usr/bin/env python3
"""chat_parser.py - v2 聊天页解析（结构性重写）。

骨架（相对 v1 的变化）：
1. **头像列先行**：左 x20-150 / 右 x935-1070 非背景连通域，~20ms，作为消息骨架；
2. 气泡配对：绿泡必自己；私聊同行 |cy差|<70；群聊差一行昵称 (avatar.top+NICK_ROW±30)；
   配对规则 = "气泡配最近的上方同侧头像"（同人连发只有首条有头像）；
3. 非文字泡推断：同侧相邻头像间距异常 + 中间无文字泡 -> unknown_nontext；
4. 气泡输出 outline 多边形（findContours+approxPolyDP，凸角/尾巴包住）；
5. bubble_model：漏行检测、宽度合理性校验（low_confidence 触发区域重识别）、
   OCR 失败反推 `[无法识别文本: 约N字M行]` 占位；
6. 按键全部元素化带 bbox：返回/更多/语音/表情/加号/发送（绿掩膜+OCR 双判）。

掩膜定义、类型判定分支、系统消息归并、胶囊处理自 v1 整块保留。
"""

import re
import unicodedata

import cv2
import numpy as np

from . import layout_consts as LC
from . import bubble_model
from .img_utils import (comps_from_mask, rect_contains, inside_comps, x_overlap,
                        estimate_bg, merge_vertical, comp_outline)
from .ocr_engine import (ocr_region, fill_missing_lines)


# ---------------------------------------------------------------- 规范化
_FOLD = str.maketrans({
    "0": "o", "O": "o", "o": "o",
    "1": "l", "I": "l", "l": "l", "|": "l",
    "5": "s", "S": "s", "s": "s",
    "8": "b", "B": "b",
    "，": ".", "、": ".", "。": ".", "．": ".", "．": ".",
    "：": ":", "＠": "@",
})


def normalize_text(t):
    """内容规范化（对齐/去重键用）：NFKC + 去空白 + 小写 + 易混字符 fold"""
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"\s+", "", t).lower()
    return t.translate(_FOLD)


# ---------------------------------------------------------------- 头像列
def find_avatars(nonbg_side):
    """头像列连通域 -> [{'side':'L'/'R','x','y','w','h'}] 按 y 排序。
    nonbg_side: 已挖掉胶囊区域的全图非背景掩膜（uint8 0/1）。"""
    avatars = []
    for side, (x0, x1) in (("L", LC.AVATAR_COL_L), ("R", LC.AVATAR_COL_R)):
        col = np.zeros_like(nonbg_side)
        col[LC.CONTENT_Y0:LC.INPUT_BAR_Y0, x0:x1] = \
            nonbg_side[LC.CONTENT_Y0:LC.INPUT_BAR_Y0, x0:x1]
        for x, y, w, h, area in comps_from_mask(
                col, min_area=LC.AVATAR_MIN_AREA, close_ksize=5,
                min_w=LC.AVATAR_MIN_W, min_h=LC.AVATAR_MIN_H):
            if LC.AVATAR_MIN_W <= w <= LC.AVATAR_MAX_W \
                    and LC.AVATAR_MIN_H <= h <= LC.AVATAR_MAX_H \
                    and LC.AVATAR_ASPECT_LO < w / h < LC.AVATAR_ASPECT_HI:
                avatars.append({"side": side, "x": x0 + x, "y": y,
                                "w": w, "h": h})
    avatars.sort(key=lambda a: a["y"])
    return avatars


def pair_avatar(bubble_rect, avatars, is_group):
    """气泡 -> 最近的上方同侧头像。side 由调用者给定（绿泡 R，其他看 x 位置）。
    返回 avatar dict 或 None。"""
    x, y, w, h = bubble_rect[:4]
    cy = y + h / 2
    side = "R" if x + w / 2 > LC.SCREEN_W / 2 else "L"
    best = None
    for a in avatars:
        if a["side"] != side:
            continue
        if is_group:
            # 群聊：bubble.top ≈ avatar.top + NICK_ROW（±PAIR_GROUP_TOL）
            if not (a["y"] + LC.NICK_ROW - LC.PAIR_GROUP_TOL
                    <= y <= a["y"] + LC.NICK_ROW + LC.PAIR_GROUP_TOL + max(0, h - 108)):
                continue
        else:
            # 私聊：气泡与头像同行
            if abs(cy - (a["y"] + a["h"] / 2)) > LC.PAIR_PRIVATE_DY + max(0, (h - 108) / 2):
                continue
        if a["y"] > cy + 30:      # 只要上方（或同条）头像
            continue
        if best is None or a["y"] > best["y"]:
            best = a
    return best


def infer_nontext_gaps(avatars, bubbles, img_comps):
    """骨架推断：同侧相邻头像间距异常大且中间无任何泡 -> 藏有未识别消息。
    返回 [{'rect':(x,y,w,h), 'content_type':'unknown_nontext'}]"""
    out = []
    for side in ("L", "R"):
        col = sorted((a for a in avatars if a["side"] == side),
                     key=lambda a: a["y"])
        for prev, nxt in zip(col, col[1:]):
            span_y0 = prev["y"] + prev["h"]
            span_y1 = nxt["y"]
            if span_y1 - span_y0 < LC.SKELETON_GAP_MIN:
                continue
            hit = False
            for rect, _kind in bubbles:
                # 与间隔区有垂直交叠即视为有内容（边界 2px 误差会漏判贴边泡）
                if min(rect[1] + rect[3], span_y1) - max(rect[1], span_y0) > 0:
                    hit = True
                    break
            if not hit:
                out.append({
                    "rect": (0, span_y0, LC.SCREEN_W, span_y1 - span_y0),
                    "content_type": "unknown_nontext",
                    "side": side,
                })
    return out


# ---------------------------------------------------------------- 类型判定辅助（自 v1 平移）
def _has_play_button(img, rect):
    """图片连通域中央是否有白色播放按钮（圆环+三角）-> 视频消息"""
    x, y, w, h = rect[:4]
    crop = img[y:y + h, x:x + w]
    if crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 1] < 90) & (hsv[:, :, 2] > 200)).astype(np.uint8)
    for cx, cy, cw, ch, _ in comps_from_mask(white, min_area=800, close_ksize=9,
                                             min_w=30, min_h=30):
        ccx, ccy = cx + cw / 2, cy + ch / 2
        if 0.25 < ccx / w < 0.75 and 0.25 < ccy / h < 0.75 \
                and 0.6 < cw / max(ch, 1) < 1.6 and 45 <= cw <= 170 \
                and 45 <= ch <= 170:
            return True
    return False


def _embedded_thumbnail(img, rect):
    """灰色卡片内部中下部是否有亮/彩色缩略图块 -> 返回缩略图 rect 或 None"""
    x, y, w, h = rect[:4]
    sub = img[y:y + h, x:x + w]
    if sub.size == 0:
        return None
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 1] > 70) | (hsv[:, :, 2] > 170)).astype(np.uint8)
    for cx, cy, cw, ch, _ in comps_from_mask(mask, min_area=6000, close_ksize=15,
                                             min_w=60, min_h=60):
        if cy > h * 0.35:
            return (x + cx, y + cy, cw, ch)
    return None


# ---------------------------------------------------------------- 按键元素
def _icon_ok(hsv, cx, cy, r=None):
    r = r or LC.CHAT_BTN_R
    s = hsv[cy - r:cy + r, cx - r:cx + r, 1]
    v = hsv[cy - r:cy + r, cx - r:cx + r, 2]
    if s.size == 0:
        return False
    return float(((s < LC.ICON_S_MAX) & (v > LC.ICON_V_MIN)).mean()) \
        > LC.CHAT_BTN_LIKE_MIN


def _btn_element(id_, name, cx, cy, hsv, actions=("tap",)):
    r = LC.CHAT_BTN_R
    return {
        "id": id_, "type": "button", "name": name,
        "verified": _icon_ok(hsv, cx, cy),
        "position": {"x": cx - r, "y": cy - r, "w": 2 * r, "h": 2 * r},
        "actions": list(actions),
    }


def _find_send_button(img, ocr_items, hsv, assigned):
    """发送按钮（输入有字时动态出现）：绿色色块 + OCR "发送" 双判，不用固定坐标。
    返回 element 或 None。"""
    x0, y0, x1, y1 = LC.SEND_SCAN_ZONE
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    green = ((h >= LC.GREEN_H_LO) & (h <= LC.GREEN_H_HI)
             & (s > LC.GREEN_S_MIN) & (v > LC.GREEN_V_MIN)).astype(np.uint8)
    zone = np.zeros_like(green)
    zone[y0:y1, x0:x1] = green[y0:y1, x0:x1]
    green_comps = comps_from_mask(zone, min_area=1500, close_ksize=7,
                                  min_w=40, min_h=40)
    ocr_hit = None
    for i, it in enumerate(ocr_items):
        if it["text"] == "发送" and x0 <= it["cx"] <= x1 and y0 <= it["cy"] <= y1:
            ocr_hit = it
            assigned[i] = True
            break
    if green_comps:
        x, y, w, h_, _ = green_comps[0]
        # 绿色色块与 OCR "发送" 重合 -> 双判成立；只有绿色色块也接受（绿掩膜为主判）
        return {
            "id": "btn_send", "type": "button", "name": "发送",
            "state": "enabled", "verified": ocr_hit is not None,
            "position": {"x": x, "y": y, "w": w, "h": h_},
            "actions": ["tap"],
        }
    if ocr_hit is not None:
        bx0, by0, bx1, by1 = (int(v_) for v_ in ocr_hit["box"])
        return {
            "id": "btn_send", "type": "button", "name": "发送",
            "state": "enabled", "verified": True,
            "position": {"x": bx0 - 20, "y": by0 - 20,
                         "w": bx1 - bx0 + 40, "h": by1 - by0 + 40},
            "actions": ["tap"],
        }
    return None


# ---------------------------------------------------------------- 主入口
def parse_chat(img, ocr_items, raw_title, gray_img=None, hsv=None):
    """返回 (title, elements, input_area, actions, page_extra)。
    gray_img/hsv 可由 build_state 预先算好传入，避免重复 cvtColor。"""
    if gray_img is None:
        gray_img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    bg = estimate_bg(gray_img, 400, LC.CONTENT_Y1)

    m = LC.MEMBER_RE.match(raw_title)
    if m:
        title, member_count = m.group(1).strip(), int(m.group(2))
    else:
        title, member_count = raw_title, None

    # ---- 掩膜（自 v1）
    # 性能收口（2026-08-04）：所有掩膜本来就把内容区外置 0，直接在内容区
    # 行带 [CONTENT_Y0:INPUT_BAR_Y0) 内计算（~70% 像素），再贴回全尺寸零阵，
    # 全局坐标索引不变；intense/min 每带各只算一次（原实现 max 算了两遍）。
    Y0, Y1 = LC.CONTENT_Y0, LC.INPUT_BAR_Y0

    def _banded(band_mask):
        full = np.zeros(gray_img.shape, np.uint8)
        full[Y0:Y1] = band_mask
        return full

    band = img[Y0:Y1]
    intense_b = band.max(axis=2)
    neutral_b = (intense_b.astype(np.int16) - band.min(axis=2)) < 12
    bubble_mask = _banded(neutral_b & (intense_b >= LC.BUBBLE_GRAY_LO)
                          & (intense_b <= LC.BUBBLE_GRAY_HI))
    hh, ss, vv = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    ss_b, vv_b = ss[Y0:Y1], vv[Y0:Y1]
    hh_b = hh[Y0:Y1]
    green_mask = _banded((hh_b >= LC.GREEN_H_LO) & (hh_b <= LC.GREEN_H_HI)
                         & (ss_b > LC.GREEN_S_MIN) & (vv_b > LC.GREEN_V_MIN))
    orange_mask = _banded((hh_b >= LC.ORANGE_H_LO) & (hh_b <= LC.ORANGE_H_HI)
                          & (ss_b > LC.ORANGE_S_MIN) & (vv_b > LC.ORANGE_V_MIN))

    # ---- 悬浮胶囊（"有人@我" / "N条新消息"）：挖掉防粘连，同时产出元素
    assigned = [False] * len(ocr_items)
    mention_button = False
    new_messages_count = 0
    capsule_rects = []
    capsule_elements = []
    cx0_, cy0_, cx1_, cy1_ = LC.CAPSULE_SCAN
    for i, it in enumerate(ocr_items):
        text_n = it["text"].replace("＠", "@")
        is_capsule = it["cx"] > cx0_ and cy0_ < it["cy"] < cy1_ and (
            "有人@我" in text_n or LC.NEW_MSG_RE.search(text_n))
        if not is_capsule:
            continue
        kind = "someone_at_me" if "有人@我" in text_n else "new_messages"
        if kind == "someone_at_me":
            mention_button = True
        mm = LC.NEW_MSG_RE.search(text_n)
        if mm:
            new_messages_count = int(mm.group(1))
        assigned[i] = True
        x0, y0, x1, y1 = (int(v) for v in it["box"])
        x0, y0 = max(0, x0 - LC.CAPSULE_PAD_L), max(LC.CONTENT_Y0, y0 - LC.CAPSULE_PAD_T)
        x1, y1 = min(LC.SCREEN_W, x1 + LC.CAPSULE_PAD_R), min(LC.INPUT_BAR_Y0, y1 + LC.CAPSULE_PAD_B)
        bubble_mask[y0:y1, x0:x1] = 0
        green_mask[y0:y1, x0:x1] = 0
        capsule_rects.append((x0, y0, x1, y1))
        capsule_elements.append({
            "id": f"cap_{kind}", "type": "capsule", "kind": kind,
            "position": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
            "actions": [],      # V2 三.3：当前阶段只检测不暴露跳转动作
        })

    # ---- 标题栏返回键旁的未读数胶囊
    other_unread = None
    for i, it in enumerate(ocr_items):
        if not assigned[i] and 90 <= it["cy"] <= 190 and it["cx"] < 200 \
                and it["text"].isdigit():
            other_unread = int(it["text"])
            assigned[i] = True
            break
    if other_unread is None:
        pill = ocr_region(img, (55, 100, 110, 70), pad=4, scale=2.0)
        mm = re.search(r"\d+", pill)
        if mm:
            other_unread = int(mm.group())

    # ---- 连通域
    gray_comps = comps_from_mask(bubble_mask, min_area=LC.BUBBLE_MIN_AREA,
                                 close_ksize=9,
                                 min_w=LC.BUBBLE_MIN_W, min_h=LC.BUBBLE_MIN_H)
    green_comps = comps_from_mask(green_mask, min_area=LC.GREEN_MIN_AREA,
                                  close_ksize=9,
                                  min_w=LC.BUBBLE_MIN_W, min_h=LC.BUBBLE_MIN_H)
    orange_comps = []
    for c in comps_from_mask(orange_mask, min_area=30000, close_ksize=11,
                             min_w=300, min_h=110):
        x, y, w, h, _ = c
        if not (300 <= w <= 900 and 110 <= h <= 320 and 1.6 < w / h < 4.5):
            continue
        if orange_mask[y:y + h, x:x + w].mean() > 0.55:
            orange_comps.append(c)

    # 图片/表情：非背景区域（去掉气泡/绿色块后），大块连通域
    nonbg = _banded((intense_b > bg + 11) | (ss_b > 60))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21))
    remove = cv2.dilate(bubble_mask | green_mask, k)
    nonbg_for_img = nonbg.copy()
    nonbg_for_img[remove > 0] = 0
    img_comps_all = comps_from_mask(nonbg_for_img, min_area=8000, close_ksize=17,
                                    min_w=120, min_h=80)
    img_comps_all = merge_vertical(img_comps_all)
    img_comps_all = [c for c in img_comps_all
                     if c[4] >= LC.IMG_COMP_MIN_AREA
                     and c[2] >= LC.IMG_COMP_MIN_W and c[3] >= LC.IMG_COMP_MIN_H]
    img_comps = []
    for c in img_comps_all:
        if c[2] > 900:
            continue
        ccx, ccy = c[0] + c[2] / 2, c[1] + c[3] / 2
        inside_card = any(
            rect_contains((g[0] - 5, g[1] - 5, g[2] + 10, g[3] + 10), ccx, ccy)
            for g in gray_comps if g[4] > 60000)
        on_orange = any(x_overlap(c, o) > c[2] * 0.5 for o in orange_comps)
        if not inside_card and not on_orange:
            img_comps.append(c)

    # ---- 头像列骨架（先挖掉胶囊，防止胶囊被当成右侧头像）
    # nonbg_side 与上面 nonbg 同表达式：直接复制，省一遍全图运算
    nonbg_side = nonbg.copy()
    for crx0, cry0, crx1, cry1 in capsule_rects:
        nonbg_side[cry0:cry1, crx0:crx1] = 0
    avatars = find_avatars(nonbg_side)

    # ---- OCR 文本分类
    time_dividers = []
    for i, it in enumerate(ocr_items):
        if LC.CONTENT_Y0 < it["cy"] < LC.CONTENT_Y1 and 400 <= it["cx"] <= 680 \
                and it["h"] < 48 and LC.TIME_RE.match(it["text"].replace(" ", "")) \
                and not inside_comps(img_comps, it["cx"], it["cy"], 6):
            time_dividers.append((it["cy"], it["text"]))
            assigned[i] = True

    item_confs = {}     # msg id 占位：消费文本时记录 conf

    def texts_in(rect, extra=0):
        """取中心点落在 rect 内的未分配 OCR 文本，按阅读顺序拼接，并标记已分配"""
        r = (rect[0] - extra, rect[1] - extra, rect[2] + 2 * extra,
             rect[3] + 2 * extra)
        hits = [(it["box"][1], it["box"][0], i, it["text"])
                for i, it in enumerate(ocr_items)
                if not assigned[i] and rect_contains(r, it["cx"], it["cy"])]
        hits.sort()
        for _, _, i, _ in hits:
            assigned[i] = True
        return "\n".join(t for _, _, _, t in hits)

    def nickname_near(rect):
        """气泡的灰色小字昵称（自 v1 平移）。必须在 texts_in 之前调用。"""
        x, y, w = rect[0], rect[1], rect[2]
        best = None
        for i, it in enumerate(ocr_items):
            # 昵称框高阈值 52：v6(PP-OCRv6) 检测框比 v3 略高（45~48），v3 时代是 46
            if assigned[i] or it["cy"] < LC.CONTENT_Y0 or it["h"] > 52:
                continue
            if y - 130 <= it["cy"] <= y + 40 and x - 90 <= it["box"][0] <= x + 120 \
                    and it["cx"] < x + max(w, 260):
                bx0, by0, bx1, by1 = (int(v) for v in it["box"])
                patch = gray_img[by0:by1, bx0:bx1]
                pix = patch[patch > 90]
                if pix.size and pix.mean() > 185:
                    continue
                if best is None or it["cy"] > best[1]["cy"]:
                    best = (i, it)
        if best is not None:
            assigned[best[0]] = True
            return best[1]["text"]
        return None

    # ---- 构建消息元素
    raw_msgs = []
    bars = []
    quotes = []
    bubbles = []

    for c in gray_comps:
        x, y, w, h, area = c
        sub = bubble_mask[y:y + h, x:x + w]
        mean_val = float(gray_img[y:y + h, x:x + w][sub > 0].mean()) \
            if area else LC.BUBBLE_GRAY
        if w > 900 and h < 170 and x < 30:      # 全宽细条：置顶条/引用预览条
            bars.append((c, mean_val))
        elif mean_val < 40 and y + h / 2 > 1980:
            bars.append((c, mean_val))
        elif mean_val < 40 and h < 150:         # 引用块
            quotes.append(c)
        else:
            bubbles.append((c, "gray"))
    for c in green_comps:
        if any(g[4] > 60000 and
               rect_contains(g[:4], c[0] + c[2] / 2, c[1] + c[3] / 2)
               for g in gray_comps):
            continue
        bubbles.append((c, "green"))
    for c in orange_comps:
        bubbles.append((c, "orange"))
    for c in img_comps:
        bubbles.append((c, "image"))
    bubbles.sort(key=lambda b: b[0][1])

    used_quotes = set()
    bar_texts = [texts_in(c) for c, _ in bars]

    # ---- 群聊/私聊判定：标题 (N) 或 检测到昵称行
    nicknames = {}
    for rect, kind in bubbles:
        if kind != "green":
            nick = nickname_near(rect)
            if nick:
                nicknames[id(rect)] = nick
    is_group = member_count is not None or bool(nicknames)

    # ---- 非文字泡骨架推断
    nontext_inferred = infer_nontext_gaps(avatars, bubbles, img_comps)

    for rect, kind in bubbles:
        x, y, w, h, area = rect
        is_green = kind == "green"
        avatar = pair_avatar(rect, avatars, is_group)
        side = "self" if is_green else (
            "self" if avatar and avatar["side"] == "R" else "other")
        if not is_green and avatar is None:
            # 无头像配对时回退：看左右列密度（v1 探测法）
            probe_y0 = max(LC.CONTENT_Y0, y - 25)
            probe_y1 = min(LC.INPUT_BAR_Y0, y + max(h, 90) + 25)
            lx0, lx1 = LC.AVATAR_COL_L
            rx0, rx1 = LC.AVATAR_COL_R
            l = nonbg_side[probe_y0:probe_y1, lx0:lx1].mean()
            r = nonbg_side[probe_y0:probe_y1, rx0:rx1].mean()
            side = "self" if r > l and r > 0.03 else "other"

        nickname = None
        if side != "self" and is_group:
            nickname = nicknames.get(id(rect))

        # 引用块配对（自 v1）
        quote_text = None
        for qi, q in enumerate(quotes):
            if qi in used_quotes:
                continue
            gap_above = y - (q[1] + q[3])
            gap_below = q[1] - (y + h)
            if (-8 <= gap_above <= 115 or -8 <= gap_below <= 60) \
                    and x_overlap(q, rect) >= q[2] * 0.35:
                quote_text = texts_in(q)
                used_quotes.add(qi)
                break

        if kind == "image":
            text = ""
            dur = None
            for i, it in enumerate(ocr_items):
                if not assigned[i] and rect_contains((x, y, w, h), it["cx"], it["cy"]) \
                        and LC.VIDEO_DUR_RE.match(it["text"].replace(" ", "")):
                    dur = it["text"].replace(" ", "")
                    assigned[i] = True
                    break
        else:
            text = texts_in(rect)
            dur = None

        # ---- 类型判定（自 v1 分支整块保留）
        if kind == "image":
            if dur or _has_play_button(img, rect):
                content_type = "video"
                content = f"[视频] {dur}".rstrip()
            else:
                content_type = "sticker" if (w <= 420 and h <= 420) else "image"
                content = "[表情]" if content_type == "sticker" else "[图片]"
        elif kind == "orange":  # 无样本未验证：红包/转账卡片
            if re.search(r"转账|¥|￥", text):
                content_type = "transfer"
                content = text or "[转账]"
            else:
                content_type = "redpacket"
                content = text or "[红包]"
        elif h > 180 and area > 60000 and not text:
            content_type = "image"
            content = "[图片]"
        elif h > 180 and area > 60000 and text:
            lines = text.split("\n")
            head = "".join(lines[:2])
            if len(lines) <= 4 and ("地图" in text or re.search(
                    r"(省|市|区|县|街道|镇|路|街|巷|号|大学|大厦|中心|广场|酒店|机场|站)", head)):
                content_type = "location"
                content = "\n".join(lines[:2])
            elif "http" in head and len(lines) <= 4:
                content_type = "link"
                content = text
            else:
                thumb = _embedded_thumbnail(img, rect)
                if thumb:
                    content_type = "link"
                    content = "\n".join(
                        it["text"] for it in sorted(
                            (it for it in ocr_items
                             if rect_contains((x, y, w, h), it["cx"], it["cy"])
                             and not rect_contains(thumb, it["cx"], it["cy"])),
                            key=lambda it: (it["box"][1], it["box"][0]))) or text
                else:
                    content_type = "text"
                    content = text
        else:
            vm = LC.VOICE_RE.match(text.strip()) if text else None
            if kind == "gray" and vm and h < 130:
                content_type = "voice"      # 无样本未验证
                content = f"[语音] {vm.group(1)}秒"
            else:
                content_type = "text"
                content = text

        if quote_text:
            content_type = "quote"
            content = (quote_text + "\n" + content).strip()

        low_confidence = False
        # OCR 漏检兜底：文字气泡内容为空时，裁剪气泡区域单独再识别一次
        if content_type == "text" and not content.strip():
            if h >= 60:
                content = ocr_region(img, rect)
                if nickname and content.strip() == nickname:
                    content = ""
            if not content.strip() and w <= 300 and h <= 300:
                content_type = "sticker"
                content = "[表情]"
        # 漏行补读（模型驱动，替代 v1 固定常数）
        if content_type == "text" and content.strip():
            content = fill_missing_lines(img, rect, content,
                                         dark_text=(kind == "green"))
        # 模型校验：宽度/行数与文本矛盾 -> low_confidence，触发区域重识别一次
        # （大泡/卡片跳过重试：整区重跑检测太贵，且多为卡片类而非宽度异常）
        if content_type == "text" and content.strip() \
                and not bubble_model.width_ok(w, content):
            retry = "" if (w > 800 or h > 400) else ocr_region(img, rect)
            if retry.strip() and bubble_model.width_ok(w, retry, tol=90):
                content = retry
            else:
                low_confidence = True
        # OCR 彻底失败的文字泡：模型反推占位
        if content_type == "text" and not content.strip():
            content, _chars, _lines = bubble_model.infer_unknown(w, h)
            low_confidence = True
        # 小灰泡里的彩色内容：微信自带 emoji 是灰底圆角泡（被灰度掩膜捕获），
        # v6 会把表情五官误 OCR 成短文本（如 "6"）。泡内彩色像素 >2% → 表情。
        # 真实短文本（"?"）是白字灰底，饱和度低，不受影响。
        if kind == "gray" and content_type == "text" and content.strip() \
                and w <= 300 and h <= 300 and len(content.strip()) <= 4:
            sub_hsv = hsv[y:y + h, x:x + w]
            if float((sub_hsv[:, :, 1] > 100).mean()) > 0.02:
                content_type, content = "sticker", "[表情]"

        # 发送者
        if side == "self":
            sender, nickname = "我", None
        else:
            sender = nickname or (title if not is_group else "unknown(left)")

        # outline 多边形（凸角/尾巴包住，供可视化）
        if kind == "green":
            outline = comp_outline(green_mask, rect, epsilon=LC.OUTLINE_EPSILON)
        elif kind in ("gray", "orange"):
            src = bubble_mask if kind == "gray" else orange_mask
            outline = comp_outline(src, rect, epsilon=LC.OUTLINE_EPSILON)
        else:
            outline = comp_outline(nonbg_for_img, rect, epsilon=LC.OUTLINE_EPSILON)

        raw_msgs.append({
            "rect": rect, "sender": sender, "sender_nickname": nickname,
            "content": content, "content_type": content_type,
            "is_mine": side == "self", "mentions": LC.MENTION_RE.findall(content),
            "low_confidence": low_confidence,
            "outline": outline,
            "avatar": ({"x": avatar["x"], "y": avatar["y"],
                        "w": avatar["w"], "h": avatar["h"]} if avatar else None),
        })

    # 骨架推断出的非文字消息（无掩膜痕迹）
    for inf in nontext_inferred:
        raw_msgs.append({
            "rect": inf["rect"], "sender": "unknown(left)",
            "sender_nickname": None,
            "content": "[未知非文字消息]", "content_type": "unknown_nontext",
            "is_mine": inf["side"] == "R", "mentions": [],
            "low_confidence": True, "outline": None, "avatar": None,
        })

    # 孤儿引用块（正文滚出屏幕）
    for qi, q in enumerate(quotes):
        if qi in used_quotes:
            continue
        text = texts_in(q)
        raw_msgs.append({
            "rect": q, "sender": "unknown(left)", "sender_nickname": None,
            "content": text, "content_type": "quote",
            "is_mine": False, "mentions": LC.MENTION_RE.findall(text),
            "low_confidence": False,
            "outline": comp_outline(bubble_mask, q, epsilon=LC.OUTLINE_EPSILON),
            "avatar": None,
        })

    # ---- 居中灰色系统提示（自 v1）
    occupied = [b[0] for b in bubbles] + list(quotes) + [c for c, _ in bars]
    sys_lines = []
    for i, it in enumerate(ocr_items):
        if assigned[i]:
            continue
        if not (LC.CONTENT_Y0 < it["cy"] < LC.CONTENT_Y1):
            continue
        if it["h"] > 50 or not (340 <= it["cx"] <= 740):
            continue
        if inside_comps(occupied, it["cx"], it["cy"], 14):
            continue
        sys_lines.append((it["cy"], i, it["text"], it["box"]))
    sys_lines.sort()
    sys_groups = []
    for cy, i, t, box in sys_lines:
        if sys_groups and cy - sys_groups[-1][0] < 90:
            sys_groups[-1][1].append(i)
            sys_groups[-1][2].append(t)
        else:
            sys_groups.append([cy, [i], [t]])
    for cy, idxs, texts in sys_groups:
        for i in idxs:
            assigned[i] = True
        content = "\n".join(texts)
        content_type = "recall" if LC.RECALL_RE.search(content) else "system"
        raw_msgs.append({
            "rect": (0, int(cy) - 30, LC.SCREEN_W, 60 * len(texts)),
            "sender": "system", "sender_nickname": None,
            "content": content, "content_type": content_type,
            "is_mine": False, "mentions": [],
            "low_confidence": False, "outline": None, "avatar": None,
        })

    # ---- 系统条（置顶 / 引用预览）
    bar_elements = []
    quote_preview = None
    for (c, mean_val), text in zip(bars, bar_texts):
        if c[1] < 500:
            bar_elements.append({
                "rect": c, "type": "pinned_message", "content": text})
        else:
            quote_preview = text
            bar_elements.append({
                "rect": c, "type": "quote_preview_bar", "content": text})

    # ---- 汇总：时间分割线 + 消息按 y 排序，挂 time_hint；seq 帧内严格递增
    entries = []
    for cy, t in time_dividers:
        entries.append((cy, {"type": "time_divider", "content": t,
                             "position": {"x": 0, "y": int(cy) - 20,
                                          "w": LC.SCREEN_W, "h": 40}}))
    for b in bar_elements:
        x, y, w, h, _ = b["rect"]
        entries.append((y, {"type": b["type"], "content": b["content"],
                            "position": {"x": x, "y": y, "w": w, "h": h}}))
    for mmsg in raw_msgs:
        x, y, w, h = mmsg["rect"][:4]
        entries.append((y, {"type": "message_bubble", **mmsg,
                            "position": {"x": x, "y": y, "w": w, "h": h}}))
    entries.sort(key=lambda e: e[0])

    elements = []
    last_time = None
    msg_n = 0
    seq = 0
    for _, e in entries:
        e.pop("rect", None)
        seq += 1
        e["seq"] = seq
        if e["type"] == "time_divider":
            last_time = e["content"]
            e["id"] = f"time_{seq}"
            elements.append(e)
            continue
        if e["type"] == "message_bubble":
            msg_n += 1
            x, y, w, h = e["position"]["x"], e["position"]["y"], \
                e["position"]["w"], e["position"]["h"]
            acts = ["long_press_message"]
            if e["content_type"] in ("text", "quote"):
                acts.insert(0, "copy_text")
            if e["content_type"] in ("image", "sticker", "video"):
                acts.insert(0, "view_image")
            e = {
                "id": f"m{msg_n}",
                "seq": seq,
                "type": "message_bubble",
                "sender": e["sender"],
                "sender_nickname": e["sender_nickname"],
                "content": e["content"],
                "content_norm": normalize_text(e["content"]),
                "content_type": e["content_type"],
                "is_mine": e["is_mine"],
                "mentions": e["mentions"],
                "low_confidence": e["low_confidence"],
                "partial_top": y <= LC.CONTENT_Y0 + 2,
                "partial_bottom": y + h >= LC.INPUT_BAR_Y0 - 2,
                "position": e["position"],
                "outline": e["outline"],
                "avatar": e["avatar"],
                "time_hint": last_time,
                "actions": acts,
            }
        else:
            e["id"] = f"{e['type']}_{seq}"
        elements.append(e)

    # ---- 按键元素化（全部暴露，含暂不用的）
    btn_elements = [
        _btn_element("btn_back", "返回", *LC.CHAT_BACK_CENTER, hsv),
        _btn_element("btn_more", "更多", *LC.CHAT_MORE_CENTER, hsv),
        _btn_element("btn_voice", "语音切换", *LC.CHAT_VOICE_CENTER, hsv),
        _btn_element("btn_emoji", "表情", *LC.CHAT_EMOJI_CENTER, hsv),
        _btn_element("btn_plus", "扩展加号", *LC.CHAT_PLUS_CENTER, hsv),
    ]
    send_btn = _find_send_button(img, ocr_items, hsv, assigned)
    if send_btn:
        btn_elements.append(send_btn)
    elements.extend(btn_elements)
    elements.extend(capsule_elements)

    input_area = {
        "id": "input_bar",
        "type": "input_bar",
        "placeholder": "发送消息",
        "empty": send_btn is None,
        "position": {"x": LC.CHAT_INPUT_BOX[0], "y": LC.CHAT_INPUT_BOX[1],
                     "w": LC.CHAT_INPUT_BOX[2] - LC.CHAT_INPUT_BOX[0],
                     "h": LC.CHAT_INPUT_BOX[3] - LC.CHAT_INPUT_BOX[1]},
        "quote_preview": quote_preview,
        "actions": ["focus_input", "type_text", "send_message",
                    "send_image", "send_voice"],
    }
    actions = [
        {"action": "send_text", "text": "<文本>", "description": "发送文本消息"},
        {"action": "scroll_up", "description": "查看更早的消息"},
        {"action": "scroll_down", "description": "查看更新的消息"},
        {"action": "back_to_home", "description": "返回会话列表"},
        {"action": "send_image", "path": "<图片路径>", "description": "发送图片"},
    ]
    page_extra = {"member_count": member_count, "is_group": is_group,
                  "mention_me_button": mention_button,
                  "new_messages_button": new_messages_count > 0,
                  "new_messages_count": new_messages_count,
                  "other_unread_count": other_unread,
                  "avatar_count": len(avatars)}
    return title, elements, input_area, actions, page_extra
