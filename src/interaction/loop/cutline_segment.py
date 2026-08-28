# -*- coding: utf-8 -*-
"""cutline_segment.py — 统一裁切线分段（自动化消息级切分，无需人工校准）。

2026-08-15 从 scripts/backfill_replay_msgs.py 提炼为正式模块。核心思想
（用户定稿）：消息级裁切标准非常单一，只有两类线——
  ① 头像上边缘（完整头像顶，贴裁切顶的残缺头像跳过）
  ② 时间戳上下边沿（水平中间段的连通圆角矩形，OCR 时间格式定位 + 扩展）

相邻裁切线之间 = 一段消息。每段可输出：y 范围、内容、识别方式
（双因子/单因子/未知/时间/自己）、头像匹配度、昵称匹配度、候选成员。

与 slice_chat 的区别：不依赖气泡完整性（缝合 union 的接缝处气泡可能
被切开），只认头像上边缘和时间戳——跨接缝消息天然正确处理。

纯函数，无真机依赖，可离线测试。
"""

import re

import cv2
import numpy as np

from ..ports.android.perception.ocr_engine import run_ocr
from ..ports.android.perception.chat_slicer import (
    _build_masks, _detect_avatars, _merge_avatars, AVATAR_EDGE_MARGIN)
from ..ports.android.perception.img_utils import estimate_bg

AVATAR_MIN_H = 85       # 与 layout_consts.AVATAR_MIN_H 一致（完整头像下界）
AVATAR_STD_H = 105      # 标准完整头像高下限（实测 108~112）
TS_X0, TS_X1 = 250, 830  # 时间戳水平中间段（避开左右头像列）
TS_CX_LO, TS_CX_HI = 330, 750  # 时间戳矩形中心 x 范围


def avatar_top_lines(img):
    """① 完整头像上边缘（统一裁切线）。贴裁切顶的残缺头像跳过。

    2026-08-15 修复：rescue 通道（纹理补救）检出的头像尺寸正常
    (h 100~145、宽高比 ~1) 也是真头像——深色背景/stitch 变形时主通道
    （非背景掩膜连通域）可能全部失败，全走 rescue 且都标 low_confidence
    （screen_01 实测 7 个头像 h=128 全被旧判据 `not low_confidence`
    排除 → 蓝虚线 0 条）。改为：low_confidence 的候选只要尺寸
    达标(h 100~150 且宽高比 0.8~1.25) 也接受。
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    H = img.shape[0]
    bg = estimate_bg(gray, 0, H)
    _, _, nonbg = _build_masks(img, gray, hsv, bg, 0, H)
    avs = _merge_avatars(_detect_avatars(gray, hsv, nonbg, 0, H))
    lines = []
    for a in avs:
        h, w = a["h"], a["w"]
        ar = w / max(h, 1)
        if a["low_confidence"]:
            if not (100 <= h <= 150 and 0.8 <= ar <= 1.25):
                continue
        else:
            if h < AVATAR_MIN_H:
                continue
        if a["y"] > AVATAR_EDGE_MARGIN or h >= AVATAR_STD_H:
            lines.append(int(a["y"]))
    return lines


def timestamp_edges(img):
    """② 时间戳圆角矩形上下边沿。OCR 时间格式定位 + 非背景像素扩展。"""
    items = run_ocr(img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    # 矮图（如滑动异常导致的 138px stitch）没有可用的背景采样区 → 直接返回空
    if H < 200:
        return []
    bg = float(np.median(gray[200:min(600, H), 100:980]))
    if not np.isfinite(bg):
        return []
    edges = set()

    def bg_frac(yy):
        if yy < 0 or yy >= H:
            return 0.0
        r = gray[yy, TS_X0:TS_X1]
        return float((np.abs(r.astype(int) - int(bg)) > 25).mean())

    for it in items:
        txt = (it["text"] or "").strip()
        if not re.fullmatch(r"\d{1,2}[:：]\d{2}", txt) and "昨天" not in txt:
            continue
        b = it["box"]
        x0, y0, x1, y1 = (int(v) for v in b)
        cx = (x0 + x1) / 2
        if not (TS_CX_LO <= cx <= TS_CX_HI):
            continue
        row = gray[y0:y1, TS_X0:TS_X1]
        frac = float((np.abs(row.astype(int) - int(bg)) > 25).mean())
        if frac < 0.05:
            continue
        yt = y0
        while yt > 0 and bg_frac(yt - 1) >= 0.05:
            yt -= 1
        yb = y1
        while yb < H and bg_frac(yb) >= 0.05:
            yb += 1
        if 15 <= (yb - yt) <= 170:
            edges.add(yt)
            edges.add(yb)
    return sorted(edges)


def _factor_of(m):
    ctype = m.get("content_type")
    if ctype == "time_divider":
        return "时间"
    side = m.get("side")
    if side == "self":
        return "自己"
    matched = m.get("matched_user_name")
    unc = m.get("uncertain_entity")
    if matched and not unc:
        return "双因子"
    if matched or m.get("nickname"):
        return "单因子"
    return "未知"


def segment_cutlines(img, roster_matcher=None, min_seg=8, title=""):
    """统一裁切线分段（核心）。

    返回 [{'y_top','y_bottom','content','factor','avatar_score',
           'nick_score','avatar_cand','nickname'}, ...]
    段 = 相邻裁切线之间；每段内容/识别方式来自整图一次 slice_chat
    （段内 slice_chat 对裁剪小段识别不可靠，故整图识别后按 y 归属）。
    """
    from ..ports.android.perception.chat_slicer import (
        slice_chat, classify_message)
    if img.shape[0] < 500:
        return []   # 矮图（滑动异常产生的 stitch，H<500）无完整消息可切
                    # （slice_chat 内部 estimate_bg(gray,400,cy1) 要求 H>=400）
    cut = sorted(set(avatar_top_lines(img)) | set(timestamp_edges(img)))
    if len(cut) < 2:
        return []
    ocr_all = run_ocr(img)
    res = slice_chat(img, ocr_all, is_group=True, title=title,
                     roster_matcher=roster_matcher)
    msgs = []
    for msg in res["messages"]:
        ctype = msg.get("content_type")
        y = int(msg.get("y", msg.get("y_top", 0)))
        if ctype == "time_divider":
            factor = "时间"
            content = (msg.get("content") or "").strip()
        else:
            factor = _factor_of(msg)
            content = (msg.get("content") or "").strip()
        msgs.append({
            "y": y, "factor": factor, "content": content, "type": ctype,
            "avatar_score": msg.get("avatar_score"),
            "nick_score": msg.get("nick_score"),
            "avatar_cand": msg.get("avatar_cand"),
            "nickname": msg.get("nickname"),
        })
    segs = []
    for j in range(len(cut) - 1):
        y0, y1 = cut[j], cut[j + 1]
        if y1 - y0 < min_seg:
            continue
        own = [mm for mm in msgs if y0 <= mm["y"] < y1]
        texts = [mm["content"] for mm in own if mm["content"]]
        factor = "未知"
        seg_type = "text"
        av_s = nk_s = None
        cand = nick = ""
        for mm in own:
            if mm["factor"] == "时间":
                factor = "时间"
                break
            if mm["factor"] != "未知":
                factor = mm["factor"]
            # 类型传播：段内第一条非 text/quote 消息的 slice_chat 类型
            # （多媒体段打标用；time_divider 已由 factor=="时间" 表达）
            mt = mm.get("type")
            if seg_type == "text" and mt \
                    and mt not in ("text", "quote", "time_divider"):
                seg_type = mt
            if mm.get("avatar_score") is not None:
                av_s = mm["avatar_score"]
                cand = mm.get("avatar_cand") or cand
            if mm.get("nick_score") is not None:
                nk_s = mm["nick_score"]
            if mm.get("nickname"):
                nick = mm["nickname"]
        segs.append({
            "y_top": int(y0), "y_bottom": int(y1),
            "content": "\n".join(texts)[:200],
            "factor": factor,
            "type": seg_type,
            "avatar_score": av_s, "nick_score": nk_s,
            "avatar_cand": cand, "nickname": nick,
        })
    return segs
