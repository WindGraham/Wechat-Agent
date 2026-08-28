# -*- coding: utf-8 -*-
"""media_classifier.py — 消息段的多媒体分类器（文本 vs 图片/表情包/卡片）。

v3 调色板法（2026-08-25，猫猫群 473 组件样本回归定稿）：

背景不可信——聊天可设自定义背景图，段裁切还可能被宽气泡占满导致背景
色估计失败。但【气泡颜色是微信主题级常量】（深色模式）：
  - 自己气泡绿 SELF_GREEN = BGR(116,180,60)
  - 他人气泡灰 BUBBLE_DARK = BGR(44,44,44)（引用卡/文件卡同色！）
  - 背景（本群自定义灰）≈ BGR(92,92,92)，与气泡色差 48，tol=24 可分

形状锚定（用户实测三特征）：
  1. 尖角使文本气泡 comp 左缘前移：他人文本 x≤162（≈156），
     图片/卡片/表情包 x≈169-171（≥168）；自己文本右缘 x1≥916（≈924），
     自己图片/卡片 x1≤912 —— 14px 的尖角位移即判据；
  2. 气泡颜色像素占 comp 内部比例高（文本中位 0.83/绿 0.66）；
  3. 文本气泡宽度 ≤790（多行顶到固定最大宽 689~756）。

分类输出：text / quote / image / card(sub=chat_record|file|link) /
system / unknown。
"""
import re

import cv2
import numpy as np

# 卡片标题硬标记：聊天记录卡的标题是独立一行「xxx的聊天记录」；
# 文本消息里提到「的聊天记录」不会整行结尾于此（实测误伤案例：
# 「各群的聊天记录是分开的小格子」）
_CHAT_RECORD_RE = re.compile(r"^.+的聊天记录$", re.M)
# 文件卡大小是独立一行「11.9 KB」
_FILE_SIZE_RE = re.compile(r"^\d+(?:\.\d+)?\s?(?:KB|MB|GB)$", re.M)

# ---- 调色板（深色模式主题常量，BGR）
SELF_GREEN = (116, 180, 60)
BUBBLE_DARK = (44, 44, 44)
COLOR_TOL = 24

# ---- 锚定常量（1080x2340 OnePlus 6T 实测）
L_TEXT_X_MAX = 162         # 他人文本气泡左缘 ≤162（尖角前移）
L_MEDIA_X_MIN = 168        # 他人媒体/卡片左缘 ≥168
R_TEXT_X1_MIN = 916        # 自己文本气泡右缘 ≥916
R_MEDIA_X1_MAX = 912       # 自己媒体/卡片右缘 ≤912

# ---- 几何常量
TEXT_MAX_W = 790           # 文本气泡最大固定宽
BUBBLE_MIN_AREA = 3000
BUBBLE_MIN_H = 40
CARD_MIN_W = 400
CARD_FILL = 0.90           # 卡片矩形填充率下限

# ---- 尖角（相对气泡顶的固定高度带，palette 掩膜上测量）
TAIL_BAND = (8, 34)
TAIL_MIN_PX = 8            # 实测文本=14px，卡片/图片=0
TAIL_SCAN = 30


def _near(img, color, tol=COLOR_TOL):
    diff = np.abs(img.astype(np.int16) - np.array(color, np.int16)).max(axis=2)
    return (diff <= tol).astype(np.uint8) * 255


def _comps_of(mask, min_area=BUBBLE_MIN_AREA, min_h=BUBBLE_MIN_H):
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area or h < min_h or w < 20:
            continue
        out.append({"box": (int(x), int(y), int(w), int(h)),
                    "area": int(area), "fill": float(area / (w * h))})
    out.sort(key=lambda c: c["box"][1])
    return out


def _tail_score(img, box, side, green):
    """尖角得分：气泡顶角带 [顶+8, 顶+34) × 尖端列区 内【精确气泡色】
    (tol=10) 像素占比。

    几何（本机深色模式实测）：L 身体缘 169、尖楔 129~169（尖端 129）；
    R 身体缘 ~905、尖楔 ~920。头像列（L≤139 / R≥945）与尖端有 10px
    交叠，用 tol=10 的精确气泡色排除头像杂色（动漫头像极少恰好
    (44,44,44)±10）。卡片直边在尖端区得分≈0，文本尖楔得分≈0.2~0.6。
    返回 None = 气泡顶被裁切（段顶截断），走兜底。
    """
    x, y, w, h = box
    if y <= 2:
        return None
    band = img[y + 8:min(y + 34, y + h)]
    if band.size == 0:
        return 0.0
    if side == "L":
        zone = band[:, 125:150]
    else:
        zone = band[:, 918:942]
    if zone.size == 0:
        return 0.0
    color = SELF_GREEN if green else BUBBLE_DARK
    m = _near(zone, color, tol=10)
    return float(np.count_nonzero(m)) / (zone.shape[0] * zone.shape[1])


def _bubble_comps(img):
    """调色板命中的气泡色组件（自己绿 + 他人灰），附带颜色/尖角属性。"""
    mask_g = _near(img, SELF_GREEN)
    mask_d = _near(img, BUBBLE_DARK)
    mask = cv2.bitwise_or(mask_g, mask_d)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    comps = _comps_of(mask)
    for c in comps:
        x, y, w, h = c["box"]
        roi = img[y:y + h, x:x + w]
        mg = _near(roi, SELF_GREEN)
        md = _near(roi, BUBBLE_DARK)
        ng, nd = np.count_nonzero(mg), np.count_nonzero(md)
        c["bubble_frac"] = float(ng + nd) / max(roi.size // 3, 1)
        c["is_green"] = bool(ng > nd)
        c["tail_l"] = _tail_score(img, c["box"], "L", c["is_green"])
        c["tail_r"] = _tail_score(img, c["box"], "R", c["is_green"])
        # 中段行左右缘（2026-08-27 实测：「N条新消息」胶囊是深色药丸，
        # 调色板命中且与气泡顶部粘连，把 box 撑宽到 924 → 误判非文本；
        # 取中段 1/3~2/3 行调色板左右缘的【中位数】——胶囊只占少数行，
        # 中位数落在气泡本体上）
        lefts, rights = [], []
        band = mask[y + h // 3: y + max(h // 3 + 1, 2 * h // 3), x:x + w]
        for row in band:
            nz = np.nonzero(row)[0]
            if nz.size:
                lefts.append(int(nz[0]))
                rights.append(int(nz[-1]))
        c["mid_left"] = x + (int(np.median(lefts)) if lefts else 0)
        c["mid_right"] = x + (int(np.median(rights)) if rights else w - 1) + 1
        c["mid_span"] = c["mid_right"] - c["mid_left"] if lefts else 0
    return comps, mask


TAIL_SCORE_MIN = 0.12        # 尖角区调色板像素占比阈值


def _is_text_bubble(c):
    """文本气泡 = 调色板命中 + 宽度合规 + 侧别颜色 + X 锚定。

    L 侧（他人）：深色 comp、左缘 ≤176（含抗锯齿边，实体缘 ~169）；
    R 侧（自己）：绿色 comp、右缘 ∈[895,935]（自己的深色文件卡靠
    「非绿色」排除）。
    宽度与锚定用中段行缘（mid_*）：「N条新消息」胶囊与气泡顶部
    粘连会把 box 撑宽/撑偏，中段行缘不受影响。
    卡片类（聊天记录/文件/链接）由调用方前置的 OCR 硬标记拦截；
    尖角得分 c["tail_l"]/c["tail_r"] 作为参考信息记录（PNG 原图上
    更可靠，JPEG 留档上因压缩噪声不稳定，不做门槛）。
    """
    x, y, w, h = c["box"]
    if c.get("mid_span", w) > TEXT_MAX_W or c.get("bubble_frac", 0) < 0.45:
        return False
    if not c["is_green"]:
        return c.get("mid_left", x) <= 176
    return 895 <= c.get("mid_right", x + w) <= 935


def _is_card(c):
    x, y, w, h = c["box"]
    return (c["fill"] >= CARD_FILL and w >= CARD_MIN_W
            and L_MEDIA_X_MIN <= x and x + w <= R_MEDIA_X1_MAX)


def _dominant_bg(img):
    """背景色估计（仅用于图片段的内容提取）：边缘众数，失败退全图中位数。"""
    edges = np.concatenate([img[0].reshape(-1, 3), img[-1].reshape(-1, 3),
                            img[:, 0].reshape(-1, 3), img[:, -1].reshape(-1, 3)])
    vals, counts = np.unique(edges.reshape(-1, 3), axis=0, return_counts=True)
    i = counts.argmax()
    if counts[i] / len(edges) >= 0.5:
        return vals[i]
    return np.median(img.reshape(-1, 3), axis=0).astype(np.uint8)


def _nonbg_comps(img):
    bg = _dominant_bg(img)
    diff = np.abs(img.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    mask = (diff > 18).astype(np.uint8) * 255
    return _comps_of(mask, min_area=1500, min_h=20), bg


def _embedded_block(img, box):
    """调色板气泡内部是否嵌着实体图块（公众号链接卡的缩略图等）。

    深色主题下链接卡与文本气泡同色同形、调色板锚定无法区分
    （2026-08-27 交流一下实测：AI寒武纪链接卡被判 text）。
    区分点：文本笔画细、闭运算后填充率低；缩略图是实心大块。
    返回图块 box（相对整图）或 None。
    只扫气泡顶部 30% 以下区域：顶部是「N条新消息」胶囊的挂载位
    （胶囊药丸色与气泡色有偏差，会被误检成嵌入块）。
    """
    x, y, w, h = box
    y0 = y + int(h * 0.3)
    roi = img[y0:y + h, x:x + w]
    m = cv2.bitwise_or(_near(roi, SELF_GREEN), _near(roi, BUBBLE_DARK))
    fg = cv2.bitwise_not(m)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    comps = _comps_of(fg, min_area=8000, min_h=60)
    bg = _dominant_bg(img)
    for c in comps:
        cx, cy, cw, ch = c["box"]
        if cw < 60:
            continue
        fill = c["area"] / (cw * ch)
        if fill < 0.6:
            continue
        # 排除"气泡外的聊天背景"块：胶囊粘连会把 bg 包进组件 box，
        # bg 灰也是"非气泡色"实心块（2026-08-27 实测 (904,84,176,140)
        # fill=0.96 的纯 bg 误检）。真缩略图颜色与 bg 有显著差异。
        block_roi = roi[cy:cy + ch, cx:cx + cw]
        mean_bgr = block_roi.reshape(-1, 3).mean(axis=0)
        if float(np.abs(mean_bgr.astype(int) - bg.astype(int)).max()) <= 25:
            continue
        return (x + cx, y0 + cy, cw, ch)
    return None


def classify_segment(img, ocr_text=""):
    """消息段裁切图 → (label, detail)。

    img: 单条消息裁切（BGR）；ocr_text: 该段已识别的 OCR 文本（可选，
    用于卡片细分）。
    """
    if img is None or img.size == 0:
        return "system", {"reason": "empty"}

    # 卡片 OCR 硬标记（整行匹配，先于形状判定：聊天记录卡也带尖角，
    # 形状与文本气泡不可分，只能靠标题文本）
    t = ocr_text or ""
    if _CHAT_RECORD_RE.search(t):
        return "card", {"reason": "ocr_marker", "sub": "chat_record"}
    if _FILE_SIZE_RE.search(t):
        return "card", {"reason": "ocr_marker", "sub": "file"}

    bubbles, _mask = _bubble_comps(img)
    text_bubbles = [c for c in bubbles if _is_text_bubble(c)]

    # 大图块检测：非调色板的大连通域（图片/表情包/封面图）。
    # 先于文本判定——「图片+引用卡」组合里引用卡会被误判成文本气泡，
    # 必须先看有没有图（引用卡本身是气泡色，不会误判为图块）。
    others, bg = _nonbg_comps(img)
    others = [c for c in others
              if not (c["box"][0] < 150 and c["box"][2] < 130)]  # 排除头像列
    img_blocks = []
    for c in others:
        if c["area"] < 20000:
            continue
        x, y, w, h = c["box"]
        roi = img[y:y + h, x:x + w]
        frac = float(np.count_nonzero(cv2.bitwise_or(
            _near(roi, SELF_GREEN), _near(roi, BUBBLE_DARK)))) / (w * h)
        if frac < 0.45:
            img_blocks.append(c)

    if text_bubbles and not img_blocks:
        main = max(text_bubbles, key=lambda c: c["area"])
        my = main["box"][1]
        # 深色主题链接卡检测：气泡内嵌实心图块（缩略图）→ 卡片不是文本
        # （2026-08-27 交流一下实测：深色链接卡调色板锚定与文本气泡全同）
        if _embedded_block(img, main["box"]) is not None:
            return "card", {"reason": "embedded_thumb", "sub": "link",
                            "main_box": main["box"]}
        # 引用回复：文本气泡下方还有无锚定的卡片色组件（引用卡）
        has_quote_card = any(
            c is not main and c["box"][1] > my and _is_card(c)
            for c in bubbles)
        if not has_quote_card:
            has_quote_card = any(
                c["box"][1] > my and c["fill"] >= CARD_FILL
                and c["box"][2] >= CARD_MIN_W and c["box"][0] >= L_MEDIA_X_MIN
                for c in others)
        label = "quote" if has_quote_card else "text"
        return label, {"reason": "palette_anchored", "main_box": main["box"],
                       "n_bubbles": len(bubbles)}

    # 红包：红橙色卡片不在气泡调色板内，点开是领取页，不能当图片处理。
    # （必须在文本判定之后：文本消息可能提到「微信红包」）
    if "微信红包" in (ocr_text or ""):
        return "red_packet", {"reason": "ocr_marker"}

    # 媒体块（图块优先；无图块时退回面积阈值）
    big = img_blocks or [c for c in others if c["area"] >= 8000]
    if big:
        main = max(big, key=lambda c: c["area"])
        x, y, w, h = main["box"]
        # 卡片提示：宽 ≥400 且内部卡片深色占比高（卡框+标题区）；
        # 仅作 hint——精确类型在点击/长按后的页面签名环节判定
        roi = img[y:y + h, x:x + w]
        dark_frac = float(np.count_nonzero(_near(roi, BUBBLE_DARK))) / (w * h)
        hint = "cardish" if (w >= CARD_MIN_W and dark_frac >= 0.25) else "imageish"
        return "media", {"reason": "nonbg_block", "hint": hint,
                         "main_box": main["box"],
                         "dark_frac": round(dark_frac, 3)}
    if bubbles:
        # 有气泡色组件但锚定失败（疑似气泡截图表情包）
        main = max(bubbles, key=lambda c: c["area"])
        return "media", {"reason": "bubble_color_no_anchor",
                         "hint": "imageish", "main_box": main["box"]}
    if not others:
        return "system", {"reason": "no_component"}
    return "unknown", {"reason": "ambiguous",
                       "main_box": max(others, key=lambda c: c["area"])["box"]}
