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
from .img_utils import comps_from_mask, estimate_bg, rect_contains, x_overlap
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
AVATAR_PARTIAL_H = 100          # 头像高 < 此值视为被上一条消息遮挡的残缺头像（完整检测框 108，残缺 73~94）

# 居中灰小字（time_divider/system）：x0>=300 排除左侧长文，灰字校验排除白字
CENTER_X0_MIN = 300
CENTER_GRAY_MEAN_MAX = 180


# ---------------------------------------------------------------- 掩膜
def _build_masks(img, gray, hsv, bg, cy0=LC.CONTENT_Y0, cy1=LC.INPUT_BAR_Y0):
    """内容区行带内的三类掩膜（与 chat_parser 同定义）：灰泡/绿泡/非背景。

    cy0/cy1：识别区域上下界（整屏=内容区 [CONTENT_Y0, 输入栏顶]；
    union 拼接图=整图 [0, union高]，见 history_collect.stitch_union）。"""
    Y0, Y1 = cy0, cy1

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


def _capsule_rects(ocr_items, cy0=LC.CONTENT_Y0, cy1=LC.INPUT_BAR_Y0):
    """悬浮胶囊（"有人@我"/"N条新消息"）外扩区域：头像检测前挖掉防误判。"""
    rects = []
    cx0_, cy0_, cx1_, cy1_ = LC.CAPSULE_SCAN
    for it in ocr_items:
        text_n = it["text"].replace("＠", "@")
        if it["cx"] > cx0_ and cy0_ < it["cy"] < cy1_ and (
                "有人@我" in text_n or LC.NEW_MSG_RE.search(text_n)):
            x0, y0, x1, y1 = (int(v) for v in it["box"])
            rects.append((max(0, x0 - LC.CAPSULE_PAD_L),
                          max(cy0, y0 - LC.CAPSULE_PAD_T),
                          min(LC.SCREEN_W, x1 + LC.CAPSULE_PAD_R),
                          min(cy1, y1 + LC.CAPSULE_PAD_B)))
    return rects


# ---------------------------------------------------------------- 头像检测（§2.8 区域验证）
def _avatar_ok(w, h):
    return (LC.AVATAR_MIN_W <= w <= LC.AVATAR_MAX_W
            and LC.AVATAR_MIN_H <= h <= LC.AVATAR_MAX_H
            and LC.AVATAR_ASPECT_LO < w / h < LC.AVATAR_ASPECT_HI)


def _avatar_edge_ok(y, h, cy0=LC.CONTENT_Y0, cy1=LC.INPUT_BAR_Y0):
    """残缺头像（只露半个）只接受贴识别区域顶/底的候选"""
    return AVATAR_EDGE_MIN_H <= h < LC.AVATAR_MIN_H \
        and (y <= cy0 + AVATAR_EDGE_MARGIN
             or y + h >= cy1 - AVATAR_EDGE_MARGIN)


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
                      bubble_mask, green_mask, quote_rects=None):
    """段内文字泡检索（§2.10 强制顺序：先做文字泡检测）。
    other 段找左缘 x>=140 的灰泡，self 段找右缘 x1>=700 的绿泡，
    均需朝向头像侧的尾巴凸角。返回 (comp, kind, mask) 或 None。
    quote_rects: 引用块矩形列表——引用块灰度落在灰泡区间内，
    必须排除，否则会被误当成文字泡（自己配自己）。"""
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
        if quote_rects and any(
                rect_contains((q[0] - 2, q[1] - 2, q[2] + 4, q[3] + 4),
                              c[0] + c[2] / 2, c[1] + c[3] / 2)
                for q in quote_rects):
            continue            # 引用块不是文字泡
        if _has_tail(mask, c, tail_side):
            hits.append((c, kind, mask))
    if not hits:
        return None
    hits.sort(key=lambda t: t[0][1])    # 段内多个泡取最上方（头像顶=消息开始）
    return hits[0]


# ---------------------------------------------------------------- 昵称（§2.10 实测规格）
def _read_nickname(img, gray, hsv, avatar,
                   cy0=LC.CONTENT_Y0, cy1=LC.INPUT_BAR_Y0):
    """裁昵称条带验证灰色文字存在 + 放大 3x 只走识别器。
    返回 (nickname, ok)；灰色文字不存在或读不出 → (None, False)。"""
    ax_r = avatar["x"] + avatar["w"]
    y0 = max(cy0, avatar["y"] + NICK_DY0)
    y1 = min(cy1, avatar["y"] + NICK_DY1)
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
    """OCR 行是否就是昵称行（归一化后）。

    只接受：精确相等 / 行是昵称的前缀（OCR 截断昵称）/ 昵称 + 纯标点后缀。
    绝不判定"昵称是行的子串"——引用块首行"风图：xxx"含昵称"风图"会被
    误当昵称行剥离，破坏引用块完整性（2026-08-13 修复）。"""
    a, b = normalize_text(line), normalize_text(nickname)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 2 and a in b:
        return True
    if a.startswith(b) and len(b) >= 2:
        tail = a[len(b):]
        if tail.strip("：:，,。. 　·-—") == "":
            return True
    return False


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


def _match_quote(quotes, used_quotes, ocr_items, consumed,
                 ref_rect, ref_y0, ref_y1, seg_y0=None, seg_y1=None):
    """引用块配对（自 chat_parser L459-470 平移）：在 quotes 中找与参考
    矩形（文字泡 rect 或段条带）垂直相邻 + 水平重叠的引用块。

    返回 (quote_text, matched_qi)；无配对返回 (None, None)。
    ref_rect: 参考矩形（用于水平重叠判定）；ref_y0/ref_y1: 参考物垂直
    范围（引用块在上方 gap_above，或罕见在下方 gap_below）。
    seg_y0/seg_y1: 当前消息段边界——引用块必须落在段内，否则是相邻
    消息的引用块（头像驱动下正文气泡与下一条消息的引用块 gap 常 <60px，
    会跨消息误配，2026-08-13 修复）。"""
    for qi, (qc, _qv) in enumerate(quotes):
        if qi in used_quotes:
            continue
        qx, qy, qw, qh = qc[:4]
        # 引用块必须在当前消息段内（头像顶切段边界），否则跳过
        if seg_y0 is not None and qy < seg_y0 - 2:
            continue
        if seg_y1 is not None and qy + qh > seg_y1 + 2:
            continue
        gap_above = ref_y0 - (qy + qh)
        gap_below = qy - ref_y1
        # 水平重叠按较窄者宽度的 35% 判（引用块通常比简短正文宽很多，
        # 如引用一整句话 + 正文只回一个"棒"字，此时 qw*0.35 会误拒——
        # 已有段边界约束兜底，这里只做弱对齐校验）
        ref_w = ref_rect[2] if len(ref_rect) >= 3 else qw
        if not ((-8 <= gap_above <= 115 or -8 <= gap_below <= 60)
                and x_overlap(qc, ref_rect) >= min(qw, ref_w) * 0.35):
            continue
        q_hits = sorted(
            (i for i, it in enumerate(ocr_items)
             if not consumed[i]
             and rect_contains((qx - 2, qy - 2, qw + 4, qh + 4),
                               it["cx"], it["cy"])),
            key=lambda i: (ocr_items[i]["box"][1],
                           ocr_items[i]["box"][0]))
        for i in q_hits:
            consumed[i] = True
        quote_text = "\n".join(ocr_items[i]["text"] for i in q_hits)
        used_quotes.add(qi)
        return quote_text, qi
    return None, None


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
def slice_chat(img, ocr_items, is_group, title, roster_matcher=None,
               content_y0=None, content_y1=None):
    """头像顶切段解析聊天页。
    img: BGR 截图（全屏 或 两屏缝合 union，见 history_collect.stitch_union）；
    ocr_items: run_ocr 格式列表（调用方传入，不被修改）；
    is_group: 群聊才读昵称；title: 会话名（保留入参，当前仅透传）。
    roster_matcher: 可选的花名册双因子匹配器 (RosterMatcher 实例)。
    content_y0/content_y1: 识别区域上下界。默认内容区 [LC.CONTENT_Y0,
    LC.INPUT_BAR_Y0]（整屏）；union 拼接图传 (0, union高)——内容区是整图，
    不再有固定标题栏/输入栏。所有纵向边界判定（段切分/头像残缺/完整性）
    都按此区域，不再硬编码全屏常量（设计文档 §9 #2）。
    返回 {"messages":[msg...], "is_group":bool, "avatar_count":int}。"""
    cy0 = LC.CONTENT_Y0 if content_y0 is None else int(content_y0)
    cy1 = LC.INPUT_BAR_Y0 if content_y1 is None else int(content_y1)
    cmid = cy0 + (cy1 - cy0) // 2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    bg = estimate_bg(gray, 400, cy1)
    bubble_mask, green_mask, nonbg = _build_masks(img, gray, hsv, bg, cy0, cy1)

    # ---- 挖掉悬浮胶囊与全宽细条（置顶条），防止被当成头像/气泡
    capsule_rects = _capsule_rects(ocr_items, cy0, cy1)
    nonbg_side = nonbg.copy()
    for r in capsule_rects:
        _carve(bubble_mask, r)
        _carve(green_mask, r)
        _carve(nonbg_side, r)
    gray_comps = comps_from_mask(bubble_mask, min_area=LC.BUBBLE_MIN_AREA,
                                 close_ksize=9, min_w=LC.BUBBLE_MIN_W,
                                 min_h=LC.BUBBLE_MIN_H)
    # ---- 置顶消息条（chat_parser bars 同判据）：顶部全宽细条（w>900、h<170、
    # x<30、y<600），是固定 UI 不是消息。记录后把条内 OCR 文字标记 consumed，
    # 防止漏进任何消息文本（2026-08-14 用户反馈：置顶信息被识别进文本区）。
    pinned_rects = [(c[0], c[1], c[0] + c[2], c[1] + c[3])
                    for c in gray_comps
                    if c[2] > BUBBLE_MAX_W and c[3] < 170
                    and c[0] < 30 and c[1] < 600]
    for c in gray_comps:
        if c[2] > BUBBLE_MAX_W and c[3] < 200:      # 全宽细条（置顶/引用预览）
            _carve(nonbg_side, (c[0], c[1], c[0] + c[2], c[1] + c[3]))
    green_comps = comps_from_mask(green_mask, min_area=LC.GREEN_MIN_AREA,
                                  close_ksize=9, min_w=LC.BUBBLE_MIN_W,
                                  min_h=LC.BUBBLE_MIN_H)

    # ---- 引用块检测（自 chat_parser L396-410 平移）：深灰小块（引用预览）
    quotes = []                 # (rect, mean_val)
    for c in gray_comps:
        x, y, w, h, area = c
        if w > BUBBLE_MAX_W and h < 170:
            continue            # 全宽细条（置顶/引用预览条），非消息引用块
        if y + h / 2 > cy1 - 60:
            continue            # 输入栏附近的深色块（麦克风/加号面板）不是引用
        # 排除表情/小图标/普通文本泡（单行短词如 "G"）
        if w < 100 or h < 35:
            continue
        sub = bubble_mask[y:y + h, x:x + w]
        mean_val = float(gray[y:y + h, x:x + w][sub > 0].mean()) \
            if area else LC.BUBBLE_GRAY
        if mean_val < 40 and h < 150:
            quotes.append((c, mean_val))
    used_quotes = set()

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
        gray, hsv, nonbg_side, cy0, cy1))
    # 保险丝：段高 >600 → 段内放宽阈值重扫（§2.9 细则 2）
    tops = [a["y"] for a in avatars]
    rescan_spans = []
    bounds = [cy0] + tops + [cy1]
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
    # 置顶消息条文字不参与消息识别：条内 OCR 文字直接 consumed（防止被多媒体
    # 段/居中灰小字等路径收进某条消息的 content）。
    for _i, _it in enumerate(ocr_items):
        if any(rect_contains((r[0] - 8, r[1] - 8, r[2] + 16, r[3] + 16),
                             _it["cx"], _it["cy"]) for r in pinned_rects):
            consumed[_i] = True
    messages = []           # (sort_y, msg)

    # ---- 段定义：屏幕顶→首头像顶为残缺段；末头像顶→内容区底为末段
    # 一条消息 = 头像+昵称+正文+引用，四部分都在【本条头像上边界】与
    # 【下一条头像上边界】之间。引用块在正文下方（不在头像上方），故段
    # 边界直接用头像顶，不向上扩展到引用块——引用块经 _match_quote 的
    # gap_below 归入它上方那条消息（2026-08-13 修复：旧逻辑把头像上方的
    # 引用块扩展进段顶，导致引用块被误归入下一条消息）。
    seg_bounds = [(a["y"], a) for a in avatars]
    segments = []           # (y0, y1, avatar|None)
    if seg_bounds:
        if seg_bounds[0][0] > cy0 + 2:
            segments.append((cy0, seg_bounds[0][0], None))
        for i, (top, av) in enumerate(seg_bounds):
            y1 = seg_bounds[i + 1][0] if i + 1 < len(seg_bounds) \
                else cy1
            segments.append((av["y"], y1, av))
    else:
        segments.append((cy0, cy1, None))

    for seg_y0, seg_y1, avatar in segments:
        partial_top_seg = avatar is None
        is_last = avatar is not None and avatar is avatars[-1]
        occupied = []

        if avatar is not None:
            side = "other" if avatar["side"] == "L" else "self"
            low_confidence = avatar["low_confidence"]
            media_present = False       # quote 消息正文是否含媒体（需归档）
            body_rect = None            # quote 消息：正文气泡 rect（裁切重排：正文在上）
            quote_rect = None           # quote 消息：引用块 rect（裁切重排：引用在下）

            # ---- 文字泡优先（§2.10 强制顺序）
            found = _find_text_bubble(side, seg_y0, seg_y1, gray_comps,
                                      green_comps, bubble_mask, green_mask,
                                      quote_rects=[qc[:4] for qc, _qv in
                                                   quotes])
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
                    # 大泡 + 极短文本 = 图片/视频泡上的 OCR 噪声（视频时长
                    # "00:00"、图中角标文字等），不是真文字泡——真文字泡的
                    # 尺寸随内容收紧，不会又大又只有两三个字
                    # （2026-08-09 风图照片被识别成文字"00"实测）
                    _tight = "".join(str(content).split())
                    if w >= 300 and h >= 150 and len(_tight) <= 6:
                        content_type = "multimedia"
                        content, lines = "", []
                    else:
                        content_type = "text"
                elif w <= 300 and h <= 300:
                    content_type = "multimedia"         # 小泡无字 → 表情类
                else:
                    content_type = "text"               # OCR 彻底失败：占位
                    content, _c, _l = bubble_model.infer_unknown(w, h)
                    lines, low_confidence = [], True

                # ---- 引用块配对（自 chat_parser L459-470 平移）
                # 引用块通常在气泡上方（gap_above）或罕见在下方（gap_below），
                # 水平重叠 >= 引用块宽的 35% 即视为同一消息的引用预览。
                quote_text = None
                if content_type == "text" and content.strip():
                    quote_text, qi_matched = _match_quote(
                        quotes, used_quotes, ocr_items, consumed,
                        comp, y, y + h, seg_y0=seg_y0, seg_y1=seg_y1)
                    if quote_text and qi_matched is not None:
                        content_type = "quote"
                        # 正文在前，引用块内容在后（用户要求：引用框内部的消息
                        # 排在具体实际信息下面，2026-08-13 调整）
                        content = (content + "\n" + quote_text).strip()
                        lines = content.split("\n")
                        # 记录正文气泡 rect（融合前）与引用块 rect，供裁切
                        # 重排（正文在上、引用在下）
                        body_rect = list(bubble_rect)
                        qc_rect = quotes[qi_matched][0]
                        qx, qy, qw, qh = qc_rect[:4]
                        quote_rect = [int(qx), int(qy), int(qw), int(qh)]
                        # 将引用块的物理矩形融合进 bubble_rect，确保 y_top 完整覆盖顶部的引用小框！
                        new_bx0 = min(bubble_rect[0], int(qx))
                        new_by0 = min(bubble_rect[1], int(qy))
                        new_bx1 = max(bubble_rect[0] + bubble_rect[2], int(qx + qw))
                        new_by1 = max(bubble_rect[1] + bubble_rect[3], int(qy + qh))
                        bubble_rect = [new_bx0, new_by0, new_bx1 - new_bx0, new_by1 - new_by0]
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
                     and it["cy"] < cy1 - 95
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
                # 群聊段：昵称行在头像右侧条带内，rest 会把它并进内容
                # （修复前靠 strip_nickname_line 剥首行；引用块配对后昵称
                # 行变成第二行剥不掉）——直接从 rest 排除昵称条带
                # （NICK_DY0..DY1 × 头像右缘起，cx 判定留 OCR 框抖动容差）
                if is_group and side == "other" and avatar is not None:
                    nick_x0 = avatar["x"] + avatar["w"] + NICK_DX0 - 15
                    rest_items = [
                        it for it in rest_items
                        if not (it["cy"] >= avatar["y"] + NICK_DY0
                                and it["cy"] <= avatar["y"] + NICK_DY1
                                and it["cx"] >= nick_x0)]
                lines = [it["text"] for it in rest_items]
                content = "\n".join(lines)
                confs = [it.get("conf") for it in rest_items]
                if confs and all(c is not None for c in confs):
                    ocr_conf = round(sum(confs) / len(confs), 4)

                # ---- 引用块配对（引用+图片/视频：正文无文字泡）
                # 引用块在段上方（gap_above）或罕见在下方；水平重叠用段条带
                # 与引用块 x 范围判定（段条带 x=0..SCREEN_W 全宽，等价于
                # 引用块中心在内容区且不与头像列重叠）。
                quote_text = None
                seg_rect = (0, seg_y0, LC.SCREEN_W, seg_y1 - seg_y0)
                quote_text, _qi = _match_quote(
                    quotes, used_quotes, ocr_items, consumed,
                    seg_rect, seg_y0, seg_y1)
                if quote_text:
                    content_type = "quote"
                    content = (content + "\n" + quote_text).strip()
                    lines = content.split("\n")
                    media_present = True    # 引用+图片/视频：正文段需归档

            # ---- 昵称（仅群聊 other 段）
            nickname = None
            if is_group and side == "other":
                nickname, nick_ok = _read_nickname(
                    img, gray, hsv, avatar, cy0, cy1)
                if not nick_ok:
                    low_confidence = True
                # 昵称行偶发被 OCR 行分配粘进内容首行：剥离，sender/content 分离
                new_lines = strip_nickname_line(lines, nickname)
                if new_lines != lines:
                    lines = new_lines
                    content = "\n".join(lines)

            # 如果是 multimedia 类型且未匹配到普通气泡，寻找段内非背景图片/视频区域作为 bubble_rect
            if bubble_rect is None and avatar is not None and seg_y1 > seg_y0:
                # 寻找段内位于头像下方或同高度的非背景色块
                sub_nonbg = nonbg_side[seg_y0:seg_y1, :]
                if sub_nonbg.size > 0:
                    comps = comps_from_mask(sub_nonbg, min_area=400, close_ksize=5, min_w=15, min_h=15)
                    av_x1 = avatar["x"] + avatar["w"]
                    av_y1_off = avatar["y"] + avatar["h"] - seg_y0
                    valid_comps = []
                    for c in comps:
                        x, y, w, h, area = c
                        # 排除头像列（x 在头像右缘内，y 落在头像范围）
                        if x < av_x1 and y < av_y1_off:
                            continue
                        # 排除输入栏右侧的 UI 元素（加号/语音按钮等，贴输入栏）
                        if x > 700 and seg_y0 + y > cy1 - 150:
                            continue
                        valid_comps.append(c)
                    if valid_comps:
                        # 合并所有图片/视频碎块，取整体外接边界（图片贴输入栏时常被切成碎块）
                        xs0 = min(c[0] for c in valid_comps)
                        ys0 = min(c[1] for c in valid_comps)
                        xs1 = max(c[0] + c[2] for c in valid_comps)
                        ys1 = max(c[1] + c[3] for c in valid_comps)
                        bubble_rect = [int(xs0), int(seg_y0 + ys0), int(xs1 - xs0), int(ys1 - ys0)]
            partial_bottom = bool(is_last and (
                bubble_rect is None
                or bubble_rect[1] + bubble_rect[3] >= cy1 - 60
                or avatar["y"] + avatar["h"] >= cy1 - 60))
            msg = {
                "side": side, "nickname": nickname,
                "content_type": content_type, "lines": lines,
                "content": content, "content_norm": normalize_text(content),
                "partial_top": bool(
                    avatar["h"] < AVATAR_PARTIAL_H
                    and avatar["y"] < cmid),
                "partial_bottom": partial_bottom,
                "avatar": {"x": avatar["x"], "y": avatar["y"],
                           "w": avatar["w"], "h": avatar["h"],
                           "side": avatar["side"]},
                "bubble_rect": bubble_rect,
                "seg_y0": int(seg_y0),
                "body_rect": body_rect,
                "quote_rect": quote_rect,
                "outline": bubble_outline,
                "low_confidence": low_confidence,
                "ocr_conf": ocr_conf,
                "y": int(seg_y0),
            }

            # 双因子匹配与资料页 Hook 标记（若提供了花名册匹配器）
            if roster_matcher and side == "other":
                ax, ay, aw, ah = avatar["x"], avatar["y"], avatar["w"], avatar["h"]
                # 微信头像在手机上是 1:1 正方形，切框补全至正方形 (以高度为准)
                sq_dim = max(aw, ah)
                crop = img[ay:min(img.shape[0], ay+sq_dim), ax:min(img.shape[1], ax+sq_dim)]
                matched, matched_name, _info = roster_matcher.match_dual_factor(crop, nickname)
                # 匹配度数值（2026-08-14：头像分/昵称分/候选昵称，供可视化）
                msg["avatar_score"] = _info.get("avatar_score")
                msg["nick_score"] = _info.get("nick_score")
                msg["avatar_cand"] = _info.get("avatar_cand")
                if matched:
                    msg["matched_user_name"] = matched_name
                    msg["uncertain_entity"] = False
                else:
                    msg["uncertain_entity"] = True
                    msg["uncertain_avatar_pos"] = (int(ax + aw / 2), int(ay + ah / 2))
            if content_type == "quote" and media_present:
                msg["media_present"] = True     # 引用+图片/视频：正文需归档
            messages.append((seg_y0, msg))
            if avatar["rescued"]:
                msg["low_confidence"] = True

        # ---- 段内居中灰小字摘出（time_divider / system）
        messages.extend(_extract_center_msgs(
            ocr_items, consumed, gray, seg_y0, seg_y1, occupied,
            capsule_rects, furniture_spans, partial_top_seg))

    # ---- 孤儿引用块（正文滚出屏幕，无气泡可配对）：单独输出（自 chat_parser）
    for qi, (qc, _qv) in enumerate(quotes):
        if qi in used_quotes:
            continue
        qx, qy, qw, qh = qc[:4]
        q_hits = sorted(
            (i for i, it in enumerate(ocr_items)
             if not consumed[i]
             and rect_contains((qx - 2, qy - 2, qw + 4, qh + 4),
                               it["cx"], it["cy"])),
            key=lambda i: (ocr_items[i]["box"][1],
                           ocr_items[i]["box"][0]))
        for i in q_hits:
            consumed[i] = True
        text = "\n".join(ocr_items[i]["text"] for i in q_hits)
        if not text.strip():
            text = ocr_region(img, qc, pad=4, scale=1.5)   # 引用块小字兜底
        if text.strip():
            messages.append((qy, {
                "side": None, "nickname": None,
                "content_type": "quote", "lines": text.split("\n"),
                "content": text, "content_norm": normalize_text(text),
                "partial_top": qy <= cy0 + 2,
                "partial_bottom": False,
                "avatar": None, "bubble_rect": list(qc[:4]),
                "outline": None, "low_confidence": False,
                "ocr_conf": None, "y": int(qy),
            }))

    messages.sort(key=lambda t: t[0])
    
    # 填充每一条消息的 y_top 和 y_bottom（准确界定范围，精细包络时间戳/气泡，排除输入栏）
    msg_list = [m for _, m in messages]
    input_bar_y = cy1
    for idx, msg in enumerate(msg_list):
        msg_y = msg.get("y", 0)
        ctype = msg.get("content_type")
        # 记录识别区域边界，供 classify_message 用区域而非全屏常量夹取坐标
        msg["content_y0"] = cy0
        msg["content_y1"] = cy1
        
        # 1. 顶边界 y_top 计算
        if ctype == "time_divider":
            # 时间戳精细包络：单行字高约 30px，居中对齐，y_top 精确置于字顶上方 10px
            y_top = max(0, msg_y - 12)
        else:
            # 普通消息：优先使用 segment 的起点 seg_y0 包络与该消息配对的引用块
            seg_y0_val = msg.get("seg_y0")
            if seg_y0_val is not None:
                y_top = max(0, seg_y0_val - 12)
            else:
                y_top = max(0, msg_y - 12)
                av = msg.get("avatar")
                if av and av["y"] >= cy0 + 30:
                    y_top = min(y_top, max(0, av["y"] - 12))
                b_rect = msg.get("bubble_rect")
                if b_rect:
                    y_top = min(y_top, max(0, b_rect[1] - 12))
        
        # 2. 底边界 y_bottom 初步计算
        if idx + 1 < len(msg_list):
            next_msg = msg_list[idx + 1]
            next_y = next_msg.get("y", input_bar_y)
            next_ctype = next_msg.get("content_type")
            next_av = next_msg.get("avatar")
            
            # 下一条消息的真实起始顶界（头像顶/消息顶/引用顶/时间戳顶）
            next_top = next_y
            if next_av and next_av["y"] >= cy0 + 30:
                next_top = min(next_top, next_av["y"])
            next_b_rect = next_msg.get("bubble_rect")
            if next_b_rect:
                next_top = min(next_top, next_b_rect[1])
                
            if next_ctype == "time_divider":
                # 下一条是时间戳：严格切在时间戳文字上方（留 12px 空隙，绝对不切到时间戳）
                y_bottom = next_y - 12
            else:
                # 下一条是普通消息：切在下一条头像/消息顶界上方 8px
                y_bottom = next_top - 8
        else:
            b_rect = msg.get("bubble_rect")
            if b_rect:
                y_bottom = min(input_bar_y, b_rect[1] + b_rect[3] + 25)
            else:
                y_bottom = min(input_bar_y, msg_y + 350)
                
        # 3. 气泡/多媒体底边完整性保护 (确保包络完整卡片/文字泡底边)
        b_rect = msg.get("bubble_rect")
        if b_rect and ctype != "time_divider":
            b_bottom = b_rect[1] + b_rect[3] + 25  # 给文字泡/大卡片底部保留 25px 视感 padding 缓冲
            if idx + 1 < len(msg_list):
                next_msg = msg_list[idx + 1]
                next_y = next_msg.get("y", input_bar_y)
                next_av = next_msg.get("avatar")
                next_top = min(next_y, next_av["y"]) if next_av else next_y
                # 若气泡底边大于初步 y_bottom 且未遮挡下一条消息主体，以气泡底边为准
                y_bottom = min(input_bar_y, max(y_bottom, min(next_top - 5, b_bottom)))
            else:
                y_bottom = min(input_bar_y, max(y_bottom, b_bottom))

        # 特殊保护：时间戳本身的 bottom 只需要精确覆盖其文字高度 (+42px)
        if ctype == "time_divider":
            y_bottom = min(y_bottom, msg_y + 42)
            
        msg["y_top"] = int(y_top)
        msg["y_bottom"] = int(y_bottom)

    return {"messages": msg_list,
            "is_group": bool(is_group),
            "avatar_count": len(avatars)}


# ================================================================ 完整性四态判定
STATE_COMPLETE = "complete"            # 完整：头像+昵称+正文都在屏内，身份可识别
STATE_TOP_CLIP = "top_clipped"         # 顶部残缺：头像/昵称/正文头在屏幕外（上方）
STATE_BOTTOM_CLIP = "bottom_clipped"   # 底部残缺：正文尾在屏幕外（输入栏下方）
STATE_BOTH_CLIP = "both_clipped"       # 顶+底都残缺：超长消息跨整个屏幕
STATE_UNIDENTIFIABLE = "unidentifiable"  # 真识别不了：头像/身份检测失败，与位置无关
STATE_SYSTEM = "system"                # 时间戳/系统消息（天生完整，不参与四态）


def classify_message(msg):
    """判定一条 slice_chat 消息的完整性四态。

    返回 dict: {state, y_top, y_bottom}。y_top/y_bottom 硬约束在识别区域
    [content_y0, content_y1] 内（整屏=内容区 [LC.CONTENT_Y0, LC.INPUT_BAR_Y0]；
    union 缝合图=整图，坐标由 slice_chat 写入 msg），绝不裁到固定 UI。

    完整性按设计文档 §4：一条消息完整 ⟺ [本条头像顶, 下条头像顶] 两条边界
    都在识别区域内（slice_chat 的段边界 + partial_top/partial_bottom 即此语义）。

    判定顺序（优先级从高到低）：
      1. 系统/时间戳 → system
      2. partial_top 且 partial_bottom → both_clipped（超长消息跨整个区域）
      3. partial_top → top_clipped
      4. partial_bottom → bottom_clipped
      5. 无头像或身份识别失败 → unidentifiable
      6. 否则 → complete
    """
    cy0 = int(msg.get("content_y0", LC.CONTENT_Y0))
    cy1 = int(msg.get("content_y1", LC.INPUT_BAR_Y0))
    ctype = msg.get("content_type")
    if ctype in ("time_divider", "system"):
        return {"state": STATE_SYSTEM,
                "y_top": max(cy0, int(msg.get("y_top", 0))),
                "y_bottom": min(cy1, int(msg.get("y_bottom", 0)))}

    pt = bool(msg.get("partial_top"))
    pb = bool(msg.get("partial_bottom"))

    if pt and pb:
        return {"state": STATE_BOTH_CLIP,
                "y_top": max(cy0, int(msg.get("y_top", 0))),
                "y_bottom": min(cy1, int(msg.get("y_bottom", 0)))}
    if pt:
        return {"state": STATE_TOP_CLIP,
                "y_top": max(cy0, int(msg.get("y_top", 0))),
                "y_bottom": min(cy1, int(msg.get("y_bottom", 0)))}
    if pb:
        return {"state": STATE_BOTTOM_CLIP,
                "y_top": max(cy0, int(msg.get("y_top", 0))),
                "y_bottom": min(cy1, int(msg.get("y_bottom", 0)))}

    # 自己的消息（side=self）：身份就是"自己"，无需昵称行
    if msg.get("side") == "self":
        return {"state": STATE_COMPLETE,
                "y_top": max(cy0, int(msg.get("y_top", 0))),
                "y_bottom": min(cy1, int(msg.get("y_bottom", 0)))}

    # 无头像 → 识别不了（孤儿引用块 / 头像检测失败）
    avatar = msg.get("avatar")
    if avatar is None:
        return {"state": STATE_UNIDENTIFIABLE,
                "y_top": max(cy0, int(msg.get("y_top", 0))),
                "y_bottom": min(cy1, int(msg.get("y_bottom", 0)))}

    # 身份识别失败（昵称 OCR 失败 且 头像双因子匹配失败）
    nickname = msg.get("nickname")
    matched = msg.get("matched_user_name")
    if not nickname and not matched:
        return {"state": STATE_UNIDENTIFIABLE,
                "y_top": max(cy0, int(msg.get("y_top", 0))),
                "y_bottom": min(cy1, int(msg.get("y_bottom", 0)))}

    # 完整：头像 + 昵称 + 正文都在区域内
    return {"state": STATE_COMPLETE,
            "y_top": max(cy0, int(msg.get("y_top", 0))),
            "y_bottom": min(cy1, int(msg.get("y_bottom", 0)))}
