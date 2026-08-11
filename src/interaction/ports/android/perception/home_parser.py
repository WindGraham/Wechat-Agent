#!/usr/bin/env python3
"""home_parser.py - v2 首页会话列表解析（重写）。

核心改动（相对 v1）：
- 会话条目切分：头像定位 -> **分割线行投影**（"略亮像素行"判据：
  分割线是覆盖 x195-1050 全宽的 1-2px 细线，一行内略亮像素 ≈845；
  文字行略亮像素 <70 且含大量极亮像素，区分度极大）；
- 残缺条目（< 0.6 条目高）标记 partial，不入选候选；
- 置顶判定（条目底色 vs 背景，常量 TODO 缺样本）与第一页判定（置顶信号 +
  过滑探针接口预留）；
- 未读标记：小红点（免打扰，unread=-1）vs 数字红圈（v1 角标 OCR 复用）；
- 置顶块修复（2026-08-06）：置顶条目整块底色通过"略亮像素"判据会被
  误认为巨型分割线，高组（>8 行）改取上下缘作为条目边界；
- L1 头像输出（2026-08-06，§2.7/§2.8 死命令）：每个行段经
  avatar_detector.verify_avatars_in_rows 行锚定验证必出一个头像元素，
  附着在会话条目的 avatar 字段，同时作为独立元素追加到 elements；
- L2 纪律：名称只取自"行内上部55% + x185~700"名称区；时间列先于
  预览判定（防时间串进预览）。
"""

import cv2
import numpy as np

from . import layout_consts as LC
from .img_utils import (comps_from_mask, rect_contains, x_overlap, estimate_bg)
from .ocr_engine import ocr_badge_digit


# ---------------------------------------------------------------- 分割线
def find_dividers(gray, bg=None):
    """行投影"略亮像素"计数找分割线，返回全局 y 列表。

    置顶条目整块底色（bg+11）也会通过"略亮像素"判据：连续 ~194 行
    合并成一个高组，组均值落在条目中间（曾把置顶行劈成两半、并吞掉
    它与下一条目之间的真分割线，导致名称/预览错位）。高组（>8 行）
    不是细线而是置顶块，改取组的上下缘作为边界。"""
    if bg is None:
        bg = estimate_bg(gray, LC.CONTENT_Y0, 2000)
    strip = gray[LC.CONTENT_Y0:LC.TAB_BAR_Y0, LC.DIV_X0:LC.DIV_X1].astype(int)
    band = ((strip > bg + 2) & (strip < bg + 40)).sum(axis=1)
    rows = np.where(band >= LC.DIV_BAND_MIN)[0]
    groups = []
    for r in rows:
        if groups and r - groups[-1][-1] <= LC.DIV_MERGE_GAP:
            groups[-1].append(r)
        else:
            groups.append([r])
    dividers = []
    for g in groups:
        if len(g) > 8:      # 置顶块：上下缘都是条目边界
            dividers.append(LC.CONTENT_Y0 + int(g[0]))
            dividers.append(LC.CONTENT_Y0 + int(g[-1]))
        else:
            dividers.append(LC.CONTENT_Y0 + int(np.mean(g)))
    return dividers


def split_items(dividers):
    """分割线 -> 条目边界列表 [(y0, y1, partial)]，残缺条目高度不足标记"""
    bounds = [LC.CONTENT_Y0] + list(dividers) + [LC.TAB_BAR_Y0]
    items = []
    for i in range(len(bounds) - 1):
        y0, y1 = bounds[i], bounds[i + 1]
        h = y1 - y0
        if h < 40:
            continue            # 边界细缝（标题栏/Tab 栏与分割线之间），非条目
        partial = h < LC.ITEM_H * LC.ITEM_PARTIAL_RATIO
        items.append((y0, y1, partial))
    return items


# ---------------------------------------------------------------- 条目内字段
def _has_mute_bell(gray_img, ocr_items, ay, ah):
    """预览行右端的免打扰铃铛（灰色小图标）。自 v1 平移。
    判别要点：铃铛不会被 OCR 成文字，而长预览的尾部文字一定有 OCR 框覆盖。"""
    row_y0, row_y1 = int(ay + ah * LC.HOME_NAME_SPLIT), int(ay + ah + 34)
    zone = gray_img[row_y0:row_y1, LC.MUTE_BELL_X0:LC.MUTE_BELL_X1]
    if zone.size == 0:
        return False
    mask = ((zone > 60) & (zone < 210)).astype(np.uint8)
    import re as _re
    for cx, cy, cw, ch, _ in comps_from_mask(mask, min_area=150, close_ksize=5,
                                             min_w=14, min_h=14):
        if cw > 70 or ch > 70:
            continue
        abs_box = (LC.MUTE_BELL_X0 + cx, row_y0 + cy,
                   LC.MUTE_BELL_X0 + cx + cw, row_y0 + cy + ch)
        # "被 OCR 文本覆盖"才算预览文字：v6 会把铃铛误读成 '△' 之类的符号，
        # 只有含文字字符（CJK/字母/数字）的 OCR 框才能充当覆盖证据。
        # 分块 OCR 会把长预览读成整行大框（右缘盖住铃铛）：覆盖证据必须
        # 是"起点就在铃铛处"的文本框（左缘容差 12px），整行大框不算；
        # 单字符误读（铃铛/角标/杂点）也不算
        covered = any(
            not (it["box"][2] < abs_box[0] or it["box"][0] > abs_box[2]
                 or it["box"][3] < abs_box[1] or it["box"][1] > abs_box[3])
            and it["box"][0] >= abs_box[0] - 12
            and len(_re.findall(r"[0-9A-Za-z一-鿿]", it["text"])) >= 2
            for it in ocr_items)
        if not covered:
            return True
    return False


def _preview_type(preview):
    """首页预览 -> last_message_type。自 v1 平移。"""
    import re
    body = re.sub(r"^\[?有人[@＠]我\]?\s*", "", preview)
    body = re.sub(r"^[^:：\[\]]{1,20}[:：]\s*", "", body)
    m = re.match(r"^\[([^\[\]]{1,6})\]", body)
    if m:
        return LC.PREVIEW_TYPE_MAP.get(m.group(1), "text")
    return "text"


def _red_badges(hsv):
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red = (((h < LC.RED_H_LO) | (h > LC.RED_H_HI))
           & (s > LC.RED_S_MIN) & (v > LC.RED_V_MIN)).astype(np.uint8)
    red[:LC.CONTENT_Y0, :] = 0
    red[LC.TAB_BAR_Y0:, :] = 0
    return comps_from_mask(red, min_area=80, close_ksize=5, min_w=8, min_h=8)


def _item_badge(img, ocr_items, badges, y0):
    """条目头像右上角的未读标记 -> (unread, badge_kind)。
    unread: N=数字红圈 / -1=小红点（免打扰）/ 0=无"""
    for bx, by, bw, bh, barea in badges:
        ccx, ccy = bx + bw / 2, by + bh / 2
        if not (LC.BADGE_ZONE_X0 <= ccx <= LC.BADGE_ZONE_X1):
            continue
        if not (y0 - 10 <= ccy <= y0 + 90):
            continue
        is_dot = LC.BADGE_DOT_MIN_SIZE <= bw <= LC.BADGE_DOT_MAX_SIZE \
            and LC.BADGE_DOT_MIN_SIZE <= bh <= LC.BADGE_DOT_MAX_SIZE \
            and LC.BADGE_DOT_MIN_AREA <= barea <= LC.BADGE_DOT_MAX_AREA \
            and 0.7 < bw / max(bh, 1) < 1.4
        if is_dot:
            return -1, "dot"          # 小红点：有未读但未计数（免打扰）
        if barea <= LC.BADGE_DOT_MAX_AREA:
            continue                  # 太小的红色杂点（头像图里的红色），忽略
        # 太大的红色块也不是角标——是头像内容（数字红圈最大也就 "99+" 胶囊）。
        # 摸鱼酱橙红帽头像（130x81）曾在此漏过 → 幻影未读死循环（2026-08-10）
        if bw > LC.BADGE_NUM_MAX_W or bh > LC.BADGE_NUM_MAX_H \
                or barea > LC.BADGE_NUM_MAX_AREA:
            continue
        digit = None
        for it in ocr_items:
            if it["text"].isdigit() and \
                    rect_contains((bx - 6, by - 6, bw + 12, bh + 12),
                                  it["cx"], it["cy"]):
                digit = int(it["text"])
                break
        if digit is None:
            digit = ocr_badge_digit(img, bx, by, bw, bh)
        return (digit if digit is not None else -1), "number"
    return 0, None


def _is_pinned(gray, y0, y1, bg):
    """置顶判定：条目底色（名字左侧文字间列的灰度众数）比列表背景亮。
    TODO(缺样本): BG_PINNED 未标定（样本中无置顶会话），逻辑已就绪，
    补采样本后由 calibrate_layout 写回常量即生效。"""
    if LC.BG_PINNED is None:
        return False
    x0, x1 = LC.PINNED_SAMPLE_COL
    zone = gray[y0 + 20:y1 - 20, x0:x1].ravel()
    if zone.size == 0:
        return False
    vals, counts = np.unique(zone, return_counts=True)
    item_bg = int(vals[np.argmax(counts)])
    return item_bg >= bg + LC.PINNED_DELTA


# ---------------------------------------------------------------- 主入口
def parse_home(img, ocr_items, gray=None, hsv=None):
    """返回 (elements, page_extra)。
    elements: s1..sn 会话条目（含 bbox/字段）；page_extra 含分页信息。
    gray/hsv 可由 build_state 预先算好传入，避免重复 cvtColor。"""
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    bg = estimate_bg(gray, LC.CONTENT_Y0, 2000)

    dividers = find_dividers(gray, bg)
    items = split_items(dividers)
    badges = _red_badges(hsv)

    # L1 头像（§2.7/§2.8 死命令）：行锚定验证，每个行段必出一个头像元素
    # （检不到产出 low_confidence 占位，不允许静默缺失）
    from .avatar_detector import verify_avatars_in_rows, SPEC_HOME_AVATAR
    avatars = verify_avatars_in_rows(
        img, gray, hsv,
        rows=[(y0, y1) for (y0, y1, _p) in items],
        col_roi=(LC.HOME_AVATAR_X0, LC.HOME_AVATAR_X1),
        spec=SPEC_HOME_AVATAR)

    # 全局未读：标题"微信(N)"
    total_unread = None
    for it in ocr_items:
        if LC.TITLE_Y0 <= it["cy"] <= LC.TITLE_Y1:
            m = LC.HOME_TITLE_RE.match(it["text"].replace(" ", ""))
            if m:
                total_unread = int(m.group(1))
                break

    elements = []
    n_full = 0
    for item_i, (y0, y1, partial) in enumerate(items):
        h = y1 - y0
        # 名字 / 预览 / 时间
        time_text = ""
        name_parts, preview_parts = [], []
        for it in ocr_items:
            if not (y0 <= it["cy"] <= y1 + 40):
                continue
            x0, bx0_, x1, y1_ = it["box"]
            # L2 纪律（§2.7）：名称只能取自"行内上部55% + x185~700"名称区；
            # 时间列（右缘）必须先于预览判定，否则时间会串进预览文字
            if LC.HOME_NAME_X0 <= x0 <= LC.HOME_NAME_X1 \
                    and it["cy"] < y0 + h * LC.HOME_NAME_SPLIT:
                name_parts.append((x0, it["text"]))
            elif x1 >= LC.HOME_TIME_X1 - 65 and it["cx"] > 900:
                time_text = it["text"]
            elif LC.HOME_NAME_X0 <= x0 and x0 < LC.HOME_PREVIEW_X1 \
                    and it["cy"] >= y0 + h * LC.HOME_NAME_SPLIT:
                preview_parts.append((x0, it["text"]))
        name = "".join(t for _, t in sorted(name_parts))
        preview = " ".join(t for _, t in sorted(preview_parts))

        unread, badge_kind = _item_badge(img, ocr_items, badges, y0)
        mention = "有人@我" in preview or "有人＠我" in preview
        pinned = _is_pinned(gray, y0, y1, bg)

        n_full += 0 if partial else 1
        idx = n_full if not partial else 0
        avatar = avatars[item_i] if item_i < len(avatars) else None
        el = {
            "id": f"s{idx}" if not partial else f"partial_{len(elements) + 1}",
            "type": "session_item",
            "label": name,
            "last_message": preview,
            "last_message_type": _preview_type(preview),
            "last_message_time": time_text,
            "unread_count": unread,
            "unread_kind": badge_kind,      # "dot"=红点 / "number"=数字圈 / None
            "mention_me": mention,
            "muted": _has_mute_bell(gray, ocr_items, y0, h),
            "pinned": pinned,
            "partial": partial,             # 残缺条目：不进入 enter_session 候选
            "position": {"x": 0, "y": int(y0), "w": LC.SCREEN_W, "h": int(h)},
            "avatar": avatar,               # L1 头像元素（附着于行框内）
            "actions": [] if partial else ["enter_session", "long_press_session"],
        }
        elements.append(el)

    # L1 头像同时作为独立元素输出（与通讯录同纪律，§2.7）。
    # 注意在 full_items/partial_count 统计之后追加，头像元素不属于会话条目。
    full_items = [e for e in elements if not e["partial"]]
    partial_count = len(elements) - len(full_items)
    for el, avatar in zip(elements, avatars):
        if avatar is not None:
            avatar["id"] = f"avatar_{el['id']}"
            elements.append(avatar)

    # 第一页判定：主信号 = 列表顶部完整条目是置顶条目；
    # BG_PINNED 未标定时返回 None（未知），由上层走过滑探针兜底
    first_page = None
    if LC.BG_PINNED is not None:
        first_page = bool(full_items) and full_items[0]["pinned"]

    page_extra = {
        "total_unread": total_unread,
        "divider_count": len(dividers),
        "item_count": len(full_items),
        "partial_count": partial_count,
        "first_page": first_page,           # True/False/None(常量缺失，需过滑探针)
        "page_no": 1 if first_page else None,   # 页码由上层按滚动次数维护
    }
    return elements, page_extra
