#!/usr/bin/env python3
"""chat_slicer.py - 聊天页头像顶切段解析器（WP2）。

需求原文：docs/PERCEPTION_DISCUSSION_20260805.md §2.8（头像死命令）、
§2.9（头像顶切段法）、§2.10（消息内容判定规则）。要点：

1. **头像切段**：左列 x20~150 / 右列 x935~1070 区域验证式头像检测
   （非背景掩膜连通域 + 局部对比度暗头像补救 + 屏幕边缘残缺头像放宽），
   合并两列按顶边 y 排序，相邻头像顶之间为一个消息段；
   段高 >600px 触发段内放宽阈值重扫（漏检保险丝）；
   屏幕顶到第一个头像顶 = 残缺段 partial_top=True。
2. **段内归类**：
   - 居中（x 400~680）+ 无气泡 + 灰小字 → time_divider（TIME_RE）/ system；
   - 群聊 other 段：裁昵称条带 [头像顶-10,+50]×[头像右缘+30,+460]，
     验证灰色文字存在（灰度115±20、饱和度0）后放大 3x recognize_line 读昵称，
     读不到 nickname=None + low_confidence；私聊跳过；
   - **文字泡优先**：颜色窗口（对方灰 30~56 / 自己绿 #07C160）
     + 朝向头像侧的尾巴凸角（行投影凸出 >=10px 连续 >=8 行）；
     找到 → content_type=text（fill_missing_lines 补读漏行）；
     找不到 → content_type=multimedia（OCR 提取段内可读文字，显式标注）。

与 chat_parser.py 平行实现（气泡驱动 vs 头像驱动），不改动 chat_parser。
"""

import cv2
import numpy as np

from . import layout_consts as LC
from . import bubble_model
from .chat_parser import normalize_text
from .img_utils import comps_from_mask, estimate_bg, rect_contains
from .ocr_engine import recognize_line, ocr_region, fill_missing_lines

# ---------------------------------------------------------------- 标定常量
SEG_RESCAN_MIN_H = 600          # 段高超过此值触发段内头像重扫（漏检保险丝）

TAIL_MIN_PROTRUDE = 10          # 尾巴凸角：头像侧凸出 >=10px（实测文字泡 13，胶囊 7）
TAIL_MIN_ROWS = 8               # 且连续 >=8 行

# 昵称条带（§2.10 实测规格：x 起点=头像右缘+36，y 中心=头像顶+18，字高 ~30）
# 2026-08-08：右界不再限宽（原 ax_r+460 会把长昵称截断）——昵称行独占该高度，
# 条带只限纵向（y 带），横向一直延伸到屏幕右缘
NICK_DY0, NICK_DY1 = -10, 50
NICK_DX0 = 30
NICK_GRAY_LO, NICK_GRAY_HI = 95, 135    # 灰度 115±20
NICK_SAT_MAX = 40                       # 饱和度 0（纯中性灰，留噪声裕量）
NICK_MIN_PIX = 40                       # 灰色文字像素数下限（实测昵称 175~459）

# 文字泡 x 窗口（实测：灰泡左缘 x≈156 含尾巴；绿泡右缘 ≈924 含尾巴）
BUBBLE_L_X0_MIN = 140
BUBBLE_R_X1_MIN = 700
BUBBLE_MAX_W = 900              # 全宽细条（置顶条/引用预览条）不是消息泡

# 头像检测
AVATAR_EDGE_MIN_H = 55          # 屏幕边缘残缺头像放宽的最小高度（实测残缺头像 h=71）
AVATAR_EDGE_MARGIN = 15         # 只放宽贴近内容区顶/底的候选（防中部图标误检）
AVATAR_TEX_STD = 5.0            # 暗头像补救：局部对比度（标准差）阈值
AVATAR_TEX_STD_RELAXED = 4.0    # 保险丝重扫时的放宽阈值
AVATAR_CARD_GRAY_MAX = 0.5      # 候选区中性灰（30~56）占比 >50% → 面板卡片，非头像

# 居中灰小字（time_divider/system）：x0>=300 排除左侧长文，灰字校验排除白字
CENTER_X0_MIN = 300
CENTER_GRAY_MEAN_MAX = 180


# ---------------------------------------------------------------- 掩膜
def _build_masks(img, gray, hsv, bg):
    """内容区行带内的三类掩膜（与 chat_parser 同定义）：灰泡/绿泡/非背景。"""
    Y0, Y1 = LC.CONTENT_Y0, LC.INPUT_BAR_Y0

    def _banded(band_mask):
        full = np.zeros(gray.shape, np.uint8)
        full[Y0:Y1] = band_mask
        return full

    band = img[Y0:Y1]
    intense_b = band.max(axis=2)
    neutral_b = (intense_b.astype(np.int16) - band.min(axis=2)) < 12
    bubble_mask = _banded(neutral_b & (intense_b >= LC.BUBBLE_GRAY_LO)
                          & (intense_b <= LC.BUBBLE_GRAY_HI))
    ss_b = hsv[Y0:Y1, :, 1]
    hh_b, vv_b = hsv[Y0:Y1, :, 0], hsv[Y0:Y1, :, 2]
    green_mask = _banded((hh_b >= LC.GREEN_H_LO) & (hh_b <= LC.GREEN_H_HI)
                         & (ss_b > LC.GREEN_S_MIN) & (vv_b > LC.GREEN_V_MIN))
    nonbg = _banded((intense_b > bg + 11) | (ss_b > 60))
    return bubble_mask, green_mask, nonbg


def _carve(mask, rect):
    x0, y0, x1, y1 = (int(v) for v in rect)
    mask[y0:y1, x0:x1] = 0


def _capsule_rects(ocr_items):
    """悬浮胶囊（"有人@我"/"N条新消息"）外扩区域：头像检测前挖掉防误判。"""
    rects = []
    cx0_, cy0_, cx1_, cy1_ = LC.CAPSULE_SCAN
    for it in ocr_items:
        text_n = it["text"].replace("＠", "@")
        if it["cx"] > cx0_ and cy0_ < it["cy"] < cy1_ and (
                "有人@我" in text_n or LC.NEW_MSG_RE.search(text_n)):
            x0, y0, x1, y1 = (int(v) for v in it["box"])
            rects.append((max(0, x0 - LC.CAPSULE_PAD_L),
                          max(LC.CONTENT_Y0, y0 - LC.CAPSULE_PAD_T),
                          min(LC.SCREEN_W, x1 + LC.CAPSULE_PAD_R),
                          min(LC.INPUT_BAR_Y0, y1 + LC.CAPSULE_PAD_B)))
    return rects


# ---------------------------------------------------------------- 头像检测（§2.8 区域验证）
def _avatar_ok(w, h):
    return (LC.AVATAR_MIN_W <= w <= LC.AVATAR_MAX_W
            and LC.AVATAR_MIN_H <= h <= LC.AVATAR_MAX_H
            and LC.AVATAR_ASPECT_LO < w / h < LC.AVATAR_ASPECT_HI)


def _avatar_edge_ok(y, h):
    """残缺头像（只露半个）只接受贴屏幕内容区顶/底的候选"""
    return AVATAR_EDGE_MIN_H <= h < LC.AVATAR_MIN_H \
        and (y <= LC.CONTENT_Y0 + AVATAR_EDGE_MARGIN
             or y + h >= LC.INPUT_BAR_Y0 - AVATAR_EDGE_MARGIN)


def _card_gray_frac(gray, hsv, x, y, w, h):
    """候选区中性灰（气泡/面板卡片色 30~56）占比"""
    sub_g = gray[y:y + h, x:x + w]
    sub_s = hsv[y:y + h, x:x + w, 1]
    if sub_g.size == 0:
        return 1.0
    card = (sub_g >= LC.BUBBLE_GRAY_LO) & (sub_g <= LC.BUBBLE_GRAY_HI) & (sub_s < 60)
    return float(card.mean())


def _detect_avatars(gray, hsv, nonbg, y0, y1, relaxed=False):
    """两列头像检测：非背景掩膜连通域 + 局部对比度暗头像补救。
    relaxed=True 为段高>600 的保险丝重扫（降面积/对比度阈值，尺寸窗口不变）。
    返回 [{'side','x','y','w','h','low_confidence','rescued'}]。"""
    min_area = 2500 if relaxed else LC.AVATAR_MIN_AREA
    tex_std = AVATAR_TEX_STD_RELAXED if relaxed else AVATAR_TEX_STD
    cands = []
    for side, (x0, x1) in (("L", LC.AVATAR_COL_L), ("R", LC.AVATAR_COL_R)):
        # ---- 主通道：非背景掩膜连通域
        col = np.zeros_like(nonbg)
        col[y0:y1, x0:x1] = nonbg[y0:y1, x0:x1]
        for x, y, w, h, area in comps_from_mask(
                col, min_area=min_area, close_ksize=5,
                min_w=LC.AVATAR_MIN_W, min_h=AVATAR_EDGE_MIN_H):
            if _avatar_ok(w, h):
                cands.append({"side": side, "x": x, "y": y, "w": w, "h": h,
                              "low_confidence": False, "rescued": False})
            elif _avatar_edge_ok(y, h) and w >= LC.AVATAR_MIN_W:
                cands.append({"side": side, "x": x, "y": y, "w": w, "h": h,
                              "low_confidence": True, "rescued": False})
        # ---- 暗头像补救：局部对比度（§2.8：宁可误检不可漏检，过滤交给窗口）
        strip = gray[y0:y1, x0:x1].astype(np.float32)
        m1 = cv2.blur(strip, (21, 21))
        m2 = cv2.blur(strip * strip, (21, 21))
        std = np.sqrt(np.maximum(m2 - m1 * m1, 0))
        tex = (std > tex_std).astype(np.uint8)
        min_h = 60 if relaxed else AVATAR_EDGE_MIN_H
        for x, y, w, h, area in comps_from_mask(
                tex, min_area=1500, close_ksize=9, min_w=40, min_h=min_h):
            gy, gx = y0 + y, x0 + x
            if not (_avatar_ok(w, h) or (_avatar_edge_ok(gy, h)
                                         and w >= LC.AVATAR_MIN_W)):
                continue
            # 纹理覆盖率（卡片只剩边缘/图标两行稀疏纹理）
            if float(tex[y:y + h, x:x + w].mean()) < 0.25:
                continue
            # 面板卡片/输入栏是中性灰色块，不是头像
            if _card_gray_frac(gray, hsv, gx, gy, w, h) > AVATAR_CARD_GRAY_MAX:
                continue
            cands.append({"side": side, "x": gx, "y": gy, "w": w, "h": h,
                          "low_confidence": True, "rescued": True})
    return cands


def _merge_avatars(cands):
    """同侧重叠候选去重（主通道优先于纹理补救），按顶边 y 排序"""
    cands.sort(key=lambda a: (a["rescued"], -(a["w"] * a["h"])))
    kept = []
    for a in cands:
        dup = False
        for k in kept:
            if k["side"] != a["side"]:
                continue
            ov = min(a["y"] + a["h"], k["y"] + k["h"]) - max(a["y"], k["y"])
            if ov > 30:
                dup = True
                break
        if not dup:
            kept.append(a)
    kept.sort(key=lambda a: a["y"])
    return kept


# ---------------------------------------------------------------- 尾巴凸角（§2.10 硬特征）
def _has_tail(mask, comp, side):
    """文字泡尾巴：头像侧行投影相对主体边缘凸出 >=10px 且连续 >=8 行。
    圆角（半径 ~20）只影响顶部/底部 12% 行带，中部带内凸出即尾巴；
    胶囊（全圆头）中部凸出仅 ~7px，被 10px 阈值排除。"""
    x, y, w, h = comp[:4]
    sub = mask[y:y + h, x:x + w]
    rows = []
    for r in range(h):
        cols = np.nonzero(sub[r])[0]
        rows.append((cols[0], cols[-1]) if len(cols) else None)
    valid = [v for v in rows if v]
    if not valid:
        return False
    idx = 0 if side == "L" else 1
    body = float(np.median([v[idx] for v in valid]))
    y0m, y1m = int(h * 0.12), max(int(h * 0.88), int(h * 0.12) + 1)
    best = cur = 0
    for r in range(y0m, y1m):
        prot = (body - rows[r][0]) if (side == "L" and rows[r]) else \
               ((rows[r][1] - body) if rows[r] else 0)
        if prot >= TAIL_MIN_PROTRUDE - 4:   # 连续行计数用略低阈值保持连贯
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    maxprot = 0.0
    for r in range(y0m, y1m):
        if rows[r]:
            prot = (body - rows[r][0]) if side == "L" else (rows[r][1] - body)
            maxprot = max(maxprot, prot)
    return maxprot >= TAIL_MIN_PROTRUDE and best >= TAIL_MIN_ROWS


def _find_text_bubble(side, seg_y0, seg_y1, gray_comps, green_comps,
                      bubble_mask, green_mask):
    """段内文字泡检索（§2.10 强制顺序：先做文字泡检测）。
    other 段找左缘 x>=140 的灰泡，self 段找右缘 x1>=700 的绿泡，
    均需朝向头像侧的尾巴凸角。返回 (comp, kind, mask) 或 None。"""
    if side == "other":
        cands = [(c, "gray", bubble_mask) for c in gray_comps
                 if BUBBLE_L_X0_MIN <= c[0] <= 400 and c[2] <= BUBBLE_MAX_W]
    else:
        cands = [(c, "green", green_mask) for c in green_comps
                 if c[0] + c[2] >= BUBBLE_R_X1_MIN and c[2] <= BUBBLE_MAX_W]
    tail_side = "L" if side == "other" else "R"
    hits = []
    for c, kind, mask in cands:
        cy = c[1] + c[3] / 2
        if not (seg_y0 - 10 <= cy < seg_y1):
            continue
        if _has_tail(mask, c, tail_side):
            hits.append((c, kind, mask))
    if not hits:
        return None
    hits.sort(key=lambda t: t[0][1])    # 段内多个泡取最上方（头像顶=消息开始）
    return hits[0]


# ---------------------------------------------------------------- 昵称（§2.10 实测规格）
def _read_nickname(img, gray, hsv, avatar):
    """裁昵称条带验证灰色文字存在 + 放大 3x 只走识别器。
    返回 (nickname, ok)；灰色文字不存在或读不出 → (None, False)。"""
    ax_r = avatar["x"] + avatar["w"]
    y0 = max(LC.CONTENT_Y0, avatar["y"] + NICK_DY0)
    y1 = min(LC.INPUT_BAR_Y0, avatar["y"] + NICK_DY1)
    x0 = min(LC.SCREEN_W - 1, ax_r + NICK_DX0)
    x1 = LC.SCREEN_W              # 不限右界：长昵称不截断（该行高只有昵称）
    if y1 <= y0 or x1 <= x0:
        return None, False
    strip = img[y0:y1, x0:x1]
    g = gray[y0:y1, x0:x1]
    s = hsv[y0:y1, x0:x1, 1]
    gray_pix = (g >= NICK_GRAY_LO) & (g <= NICK_GRAY_HI) & (s < NICK_SAT_MAX)
    if int(gray_pix.sum()) < NICK_MIN_PIX:
        return None, False              # 灰色文字不存在，不允许静默
    big = cv2.resize(strip, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    text, conf = recognize_line(big)
    if text:
        return text, True
    return None, False


def _nick_eq(line, nickname):
    """OCR 行是否就是昵称行：归一化后相等或互为包含（OCR 截断/粘连容错）。"""
    a, b = normalize_text(line), normalize_text(nickname)
    if not a or not b:
        return False
    return a == b or (len(a) >= 2 and a in b) or (len(b) >= 2 and b in a)


def strip_nickname_line(lines, nickname):
    """群聊段内 OCR 行分配偶发把昵称行粘进内容首行（实测 'jywang\\n@陈曦\\n…'）：
    剥离开头与昵称相同的行，保证 sender 与 content 字段干净分离。"""
    if not nickname:
        return lines
    out = list(lines)
    while out and _nick_eq(out[0], nickname):
        out = out[1:]
    return out


# ---------------------------------------------------------------- 居中灰小字
def _is_gray_text(gray, item):
    """time_divider/system 是灰小字（实测时间文字 75~89，昵称 111）；
    更亮的白字不进入本分支（面板标签另有家具区排除兜底）"""
    x0, y0, x1, y1 = (int(v) for v in item["box"])
    patch = gray[max(0, y0):y1, max(0, x0):x1]
    if patch.size == 0:
        return False
    pix = patch[patch > 40]
    return pix.size >= 8 and float(pix.mean()) <= CENTER_GRAY_MEAN_MAX


def _extract_center_msgs(ocr_items, consumed, gray, seg_y0, seg_y1,
                         occupied, capsule_rects, furniture_spans,
                         partial_top):
    """段内居中+无气泡+灰小字 → time_divider / system 消息列表"""
    hits = []
    for i, it in enumerate(ocr_items):
        if consumed[i] or not (seg_y0 <= it["cy"] < seg_y1):
            continue
        if it["h"] > 50 or it["box"][0] < CENTER_X0_MIN \
                or not (400 <= it["cx"] <= 680):
            continue
        if any(fy0 <= it["cy"] <= fy1 for fy0, fy1 in furniture_spans):
            continue
        text_n = it["text"].replace("＠", "@")
        if "有人@我" in text_n or LC.NEW_MSG_RE.search(text_n):
            continue
        if any(rect_contains((r[0] - 8, r[1] - 8, r[2] + 16, r[3] + 16),
                             it["cx"], it["cy"]) for r in occupied):
            continue
        if any(rect_contains((r[0], r[1], r[2] - r[0], r[3] - r[1]),
                             it["cx"], it["cy"]) for r in capsule_rects):
            continue
        if not _is_gray_text(gray, it):
            continue
        hits.append((it["cy"], i, it["text"]))
        consumed[i] = True
    hits.sort()
    msgs = []
    sys_group = []
    for cy, _i, text in hits:
        if LC.TIME_RE.match(text.replace(" ", "")):
            msgs.append((cy, _base_msg("time_divider", [text], partial_top, cy)))
            continue
        if sys_group and cy - sys_group[-1][0] < 90:
            sys_group[-1][1].append(text)
        else:
            sys_group.append((cy, [text]))
    for cy, texts in sys_group:
        msgs.append((cy, _base_msg("system", texts, partial_top, cy)))
    return msgs


def _base_msg(content_type, lines, partial_top, y):
    return {
        "side": None, "nickname": None, "content_type": content_type,
        "lines": list(lines), "content": "\n".join(lines),
        "content_norm": normalize_text("\n".join(lines)),
        "partial_top": partial_top, "partial_bottom": False,
        "avatar": None, "bubble_rect": None, "low_confidence": False,
        "y": int(y),
    }


def _comp_outline(mask, comp, epsilon=None):
    """从连通域掩膜提取多边形轮廓（全局坐标），供 overlay 多边形描边。"""
    if epsilon is None:
        epsilon = LC.OUTLINE_EPSILON
    x, y, w, h = (int(v) for v in comp[:4])
    sub = mask[y:y + h, x:x + w]
    cnts, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    approx = cv2.approxPolyDP(c, epsilon, True)
    return [[int(p[0][0]) + x, int(p[0][1]) + y] for p in approx]


# ---------------------------------------------------------------- 主入口
def slice_chat(img, ocr_items, is_group, title):
    """头像顶切段解析聊天页。
    img: BGR 全屏截图；ocr_items: run_ocr 格式列表（调用方传入，不被修改）；
    is_group: 群聊才读昵称；title: 会话名（保留入参，当前仅透传）。
    返回 {"messages":[msg...], "is_group":bool, "avatar_count":int}。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    bg = estimate_bg(gray, 400, LC.CONTENT_Y1)
    bubble_mask, green_mask, nonbg = _build_masks(img, gray, hsv, bg)

    # ---- 挖掉悬浮胶囊与全宽细条（置顶条），防止被当成头像/气泡
    capsule_rects = _capsule_rects(ocr_items)
    nonbg_side = nonbg.copy()
    for r in capsule_rects:
        _carve(bubble_mask, r)
        _carve(green_mask, r)
        _carve(nonbg_side, r)
    gray_comps = comps_from_mask(bubble_mask, min_area=LC.BUBBLE_MIN_AREA,
                                 close_ksize=9, min_w=LC.BUBBLE_MIN_W,
                                 min_h=LC.BUBBLE_MIN_H)
    for c in gray_comps:
        if c[2] > BUBBLE_MAX_W and c[3] < 200:      # 全宽细条（置顶/引用预览）
            _carve(nonbg_side, (c[0], c[1], c[0] + c[2], c[1] + c[3]))
    green_comps = comps_from_mask(green_mask, min_area=LC.GREEN_MIN_AREA,
                                  close_ksize=9, min_w=LC.BUBBLE_MIN_W,
                                  min_h=LC.BUBBLE_MIN_H)
    # 家具区：无尾巴的大灰块（加号面板会与输入栏并成整宽连通域）及其标签带，
    # 居中灰小字摘出要避开（面板标签"拍摄"等会伪装成居中小字）
    furniture_spans = []
    for c in gray_comps:
        if c[2] >= 120 and c[3] >= 120 \
                and not _has_tail(bubble_mask, c, "L") \
                and not _has_tail(bubble_mask, c, "R"):
            furniture_spans.append((c[1] - 20, c[1] + c[3] + 80))

    # ---- 头像切段
    avatars = _merge_avatars(_detect_avatars(
        gray, hsv, nonbg_side, LC.CONTENT_Y0, LC.INPUT_BAR_Y0))
    # 保险丝：段高 >600 → 段内放宽阈值重扫（§2.9 细则 2）
    tops = [a["y"] for a in avatars]
    rescan_spans = []
    bounds = [LC.CONTENT_Y0] + tops + [LC.INPUT_BAR_Y0]
    for a, b in zip(bounds, bounds[1:]):
        if b - a > SEG_RESCAN_MIN_H:
            rescan_spans.append((a, b))
    for a, b in rescan_spans:
        extra = _detect_avatars(gray, hsv, nonbg_side, a + 10, b - 10,
                                relaxed=True)
        new = [e for e in extra
               if all(not (e["side"] == k["side"]
                           and min(e["y"] + e["h"], k["y"] + k["h"])
                           - max(e["y"], k["y"]) > 30) for k in avatars)]
        if new:
            avatars = _merge_avatars(avatars + new)

    consumed = [False] * len(ocr_items)
    messages = []           # (sort_y, msg)

    # ---- 段定义：屏幕顶→首头像顶为残缺段；末头像顶→内容区底为末段
    seg_bounds = [(a["y"], a) for a in avatars]
    segments = []           # (y0, y1, avatar|None)
    if seg_bounds:
        if seg_bounds[0][0] > LC.CONTENT_Y0 + 2:
            segments.append((LC.CONTENT_Y0, seg_bounds[0][0], None))
        for i, (top, av) in enumerate(seg_bounds):
            y1 = seg_bounds[i + 1][0] if i + 1 < len(seg_bounds) \
                else LC.INPUT_BAR_Y0
            segments.append((top, y1, av))
    else:
        segments.append((LC.CONTENT_Y0, LC.INPUT_BAR_Y0, None))

    for seg_y0, seg_y1, avatar in segments:
        partial_top_seg = avatar is None
        is_last = avatar is not None and avatar is avatars[-1]
        occupied = []

        if avatar is not None:
            side = "other" if avatar["side"] == "L" else "self"
            low_confidence = avatar["low_confidence"]

            # ---- 文字泡优先（§2.10 强制顺序）
            found = _find_text_bubble(side, seg_y0, seg_y1, gray_comps,
                                      green_comps, bubble_mask, green_mask)
            bubble_rect = None
            bubble_outline = None
            lines, content = [], ""
            ocr_conf = None
            content_type = "multimedia"
            if found is not None:
                comp, kind, _mask = found
                x, y, w, h = comp[:4]
                bubble_rect = [int(x), int(y), int(w), int(h)]
                bubble_outline = _comp_outline(_mask, comp)
                occupied.append(comp)
                hits = sorted(
                    (i for i, it in enumerate(ocr_items)
                     if not consumed[i]
                     and rect_contains((x - 2, y - 2, w + 4, h + 4),
                                       it["cx"], it["cy"])),
                    key=lambda i: (ocr_items[i]["box"][1],
                                   ocr_items[i]["box"][0]))
                for i in hits:
                    consumed[i] = True
                lines = [ocr_items[i]["text"] for i in hits]
                content = "\n".join(lines)
                if not content.strip() and h >= 60:
                    content = ocr_region(img, comp)     # 漏检兜底重识别
                    lines = content.split("\n") if content.strip() else []
                if content.strip():
                    filled = fill_missing_lines(
                        img, comp, content, dark_text=(kind == "green"))
                    if len(filled) > len(content):
                        content, lines = filled, filled.split("\n")
                if content.strip():
                    content_type = "text"
                elif w <= 300 and h <= 300:
                    content_type = "multimedia"         # 小泡无字 → 表情类
                else:
                    content_type = "text"               # OCR 彻底失败：占位
                    content, _c, _l = bubble_model.infer_unknown(w, h)
                    lines, low_confidence = [], True
            if content_type == "multimedia":
                # OCR 提取段内一切可读文字，显式标注 multimedia（§2.10）
                # 排除输入栏区域（cy >= 聚焦态栏顶 2015）：输入框里未发送的
                # 文字和"发送"按钮在末段 y 范围内（cy~2067/2076 < 2110），
                # 不剔除会被粘进最后一条 multimedia（实测污染日志导致锚点
                # 永远对不上、反复 gap）。文字泡内容走 bubble_rect 分配，
                # 不受影响。
                rest = sorted(
                    (i for i, it in enumerate(ocr_items)
                     if not consumed[i]
                     and seg_y0 <= it["cy"] < seg_y1
                     and it["cy"] < LC.INPUT_BAR_Y0 - 95
                     and not any(rect_contains(
                         (r[0], r[1], r[2] - r[0], r[3] - r[1]),
                         it["cx"], it["cy"]) for r in capsule_rects)),
                    key=lambda i: (ocr_items[i]["box"][1],
                                   ocr_items[i]["box"][0]))
                # 居中灰小字（时间/系统）不属于多媒体内容，先摘出
                rest_items = [ocr_items[i] for i in rest
                              if not (ocr_items[i]["h"] <= 50
                                      and ocr_items[i]["box"][0] >= CENTER_X0_MIN
                                      and 400 <= ocr_items[i]["cx"] <= 680)]
                lines = [it["text"] for it in rest_items]
                content = "\n".join(lines)
                confs = [it.get("conf") for it in rest_items]
                if confs and all(c is not None for c in confs):
                    ocr_conf = round(sum(confs) / len(confs), 4)

            # ---- 昵称（仅群聊 other 段）
            nickname = None
            if is_group and side == "other":
                nickname, nick_ok = _read_nickname(img, gray, hsv, avatar)
                if not nick_ok:
                    low_confidence = True
                # 昵称行偶发被 OCR 行分配粘进内容首行：剥离，sender/content 分离
                new_lines = strip_nickname_line(lines, nickname)
                if new_lines != lines:
                    lines = new_lines
                    content = "\n".join(lines)

            partial_bottom = bool(is_last and (
                bubble_rect is None
                or bubble_rect[1] + bubble_rect[3] >= LC.INPUT_BAR_Y0 - 10))
            msg = {
                "side": side, "nickname": nickname,
                "content_type": content_type, "lines": lines,
                "content": content, "content_norm": normalize_text(content),
                "partial_top": avatar["y"] <= LC.CONTENT_Y0 + 2,
                "partial_bottom": partial_bottom,
                "avatar": {"x": avatar["x"], "y": avatar["y"],
                           "w": avatar["w"], "h": avatar["h"],
                           "side": avatar["side"]},
                "bubble_rect": bubble_rect,
                "outline": bubble_outline,
                "low_confidence": low_confidence,
                "ocr_conf": ocr_conf,
                "y": int(seg_y0),
            }
            messages.append((seg_y0, msg))
            if avatar["rescued"]:
                msg["low_confidence"] = True

        # ---- 段内居中灰小字摘出（time_divider / system）
        messages.extend(_extract_center_msgs(
            ocr_items, consumed, gray, seg_y0, seg_y1, occupied,
            capsule_rects, furniture_spans, partial_top_seg))

    messages.sort(key=lambda t: t[0])
    return {"messages": [m for _, m in messages],
            "is_group": bool(is_group),
            "avatar_count": len(avatars)}
