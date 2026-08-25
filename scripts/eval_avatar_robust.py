# -*- coding: utf-8 -*-
"""eval_avatar_robust.py — 头像模糊匹配鲁棒性离线评测（三策略对比）。

策略：
  old      原版等尺寸匹配（无修边，复刻集成前的 _match_image_score）
  trim     修边后的快速通道（现状 _match_image_score）
  twostage 快速通道 + 灰区 [0.45, 0.70) 鲁棒复核（集成后 match_dual_factor 实际行为）

指标：rank-1 命中本人且分≥0.70（正确识别）；误收=rank-1 非本人但分≥0.70（危险）。
"""
import os
import sys
import random

import cv2
import numpy as np

sys.path.insert(0, ".")
from src.interaction.ports.android.perception.roster_matcher import (
    RosterMatcher, GRAY_ZONE_LO, ROBUST_TOPN)

GROUP = "交流一下？"
CHAT_SIDE = 132
THRESH = 0.70
rng = random.Random(42)


def deg_none(img): return img


def deg_night(img):
    return np.clip(img.astype(np.float32) * 0.55, 0, 255).astype(np.uint8)


def deg_warm(img):
    out = img.astype(np.float32)
    out[:, :, 0] *= 0.72
    out[:, :, 2] = np.clip(out[:, :, 2] * 1.12, 0, 255)
    return out.astype(np.uint8)


def deg_blur(img): return cv2.GaussianBlur(img, (5, 5), 0.9)


def deg_jpeg(img):
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 68])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def deg_gap_top(img):
    g = rng.randint(4, 8)
    out = np.full((img.shape[0] + g, img.shape[1], 3), int(rng.choice([20, 245])), np.uint8)
    out[g:] = img
    return out


def deg_gap_side(img):
    g = rng.randint(3, 7)
    out = np.full((img.shape[0], img.shape[1] + g, 3), int(rng.choice([20, 245])), np.uint8)
    out[:, g:] = img
    return out


def deg_trunc(img):
    cut = int(img.shape[0] * rng.uniform(0.10, 0.18))
    return img[: img.shape[0] - cut]


def deg_offcenter(img):
    s = rng.randint(6, 10)
    out = np.full_like(img, int(rng.choice([18, 250])))
    out[:, : img.shape[1] - s] = img[:, s:]
    return out


DEGRADERS = [deg_none, deg_night, deg_warm, deg_blur, deg_jpeg,
             deg_gap_top, deg_gap_side, deg_trunc, deg_offcenter]


def old_score(crop, tmpl):
    """复刻集成前逻辑：无修边，等尺寸 BGR + Hue 提权。"""
    h, w = crop.shape[:2]
    mask, bgr, hist_tmpl = tmpl["mask"], tmpl["bgr"], tmpl["hist_hue"]
    side = max(w, h)
    crop_sq = cv2.resize(crop, (side, side))
    bgr_r = cv2.resize(bgr, (side, side), interpolation=cv2.INTER_AREA)
    m_r = cv2.resize(mask, (side, side), interpolation=cv2.INTER_NEAREST) if mask is not None else None
    s_bgr = float(cv2.matchTemplate(crop_sq, bgr_r, cv2.TM_CCOEFF_NORMED, mask=m_r)[0][0]) if m_r is not None \
        else float(cv2.matchTemplate(crop_sq, bgr_r, cv2.TM_CCOEFF_NORMED)[0][0])
    hsv_crop = cv2.cvtColor(crop_sq, cv2.COLOR_BGR2HSV)
    hist_crop = cv2.calcHist([hsv_crop], [0], m_r, [180], [0, 180])
    cv2.normalize(hist_crop, hist_crop, 0, 1, cv2.NORM_MINMAX)
    s_hue = float(cv2.compareHist(hist_crop, hist_tmpl, cv2.HISTCMP_CORREL))
    if s_hue >= 0.90 and s_bgr >= 0.50:
        return max(s_bgr, 0.5 * s_bgr + 0.5 * s_hue)
    return s_bgr


def best_of(rm, chat_av, score_fn):
    best, name = -1.0, ""
    for t in rm._candidates_for(chat_av):
        s = score_fn(chat_av, t)
        if s > best:
            best, name = s, t["key"]
    return best, name


def twostage(rm, chat_av):
    scored = [(rm._match_image_score(chat_av, t), t) for t in rm._candidates_for(chat_av)]
    best, name = max(scored, key=lambda x: x[0])[0], max(scored, key=lambda x: x[0])[1]["key"]
    if GRAY_ZONE_LO <= best < THRESH:
        for _, t in sorted(scored, key=lambda x: -x[0])[:ROBUST_TOPN]:
            rs = rm._robust_image_score(chat_av, t)
            if rs > best:
                best, name = rs, t["key"]
    return best, name


def main():
    rm = RosterMatcher(GROUP)
    print(f"模板数: {len(rm.avatar_templates)}")
    stats = {d.__name__: {"old": [0, 0], "trim": [0, 0], "two": [0, 0], "n": 0}
             for d in DEGRADERS}  # [命中, 误收]

    for m in rm.member_profiles[:60]:
        rel = m.get("avatar_image_path")
        if not rel:
            continue
        img = cv2.imread(os.path.join(rm.group_dir, rel), cv2.IMREAD_UNCHANGED)
        if img is None:
            continue
        bgr, true_name = img[:, :, :3], m.get("main_nickname", "")
        for deg in DEGRADERS:
            chat_av = cv2.resize(bgr, (CHAT_SIDE, CHAT_SIDE), interpolation=cv2.INTER_AREA)
            chat_av = deg(chat_av)
            if min(chat_av.shape[:2]) < 24:
                continue
            st = stats[deg.__name__]
            st["n"] += 1
            s_old, n_old = best_of(rm, chat_av, old_score)
            s_trim, n_trim = best_of(rm, chat_av, rm._match_image_score)
            s_two, n_two = twostage(rm, chat_av)
            for key, s, n in (("old", s_old, n_old), ("trim", s_trim, n_trim),
                              ("two", s_two, n_two)):
                if s >= THRESH:
                    if n == true_name:
                        st[key][0] += 1
                    else:
                        st[key][1] += 1

    print(f"\n{'退化类型':<14}{'N':>4} │ {'old命中':>6}{'old误收':>6} │ "
          f"{'trim命中':>7}{'trim误收':>7} │ {'两阶段命中':>8}{'两阶段误收':>8}")
    for d in DEGRADERS:
        st = stats[d.__name__]
        print(f"{d.__name__:<14}{st['n']:>4} │ {st['old'][0]:>6}{st['old'][1]:>6} │ "
              f"{st['trim'][0]:>7}{st['trim'][1]:>7} │ {st['two'][0]:>8}{st['two'][1]:>8}")


if __name__ == "__main__":
    main()
