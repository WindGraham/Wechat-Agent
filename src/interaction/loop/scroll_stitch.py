# -*- coding: utf-8 -*-
"""scroll_stitch.py — 滚动拼接识别核心（两屏差分拼接）。

落实 SCROLL_STITCH_IMPL.md 的纯函数部分：
  - find_overlap_dy：相邻两屏垂直位移（matchTemplate / phaseCorrelate）
  - slice_new_part：删重叠得新增切片
  - stitch_sequence：把连续截图序列差分拼接成长图（离线验证）
  - scroll_scan：滚动识别主循环骨架（四态分流 + 残片缓冲）

纯函数，无真机依赖，可用离线截图序列单测。
"""

import cv2
import numpy as np

CONTENT_Y0 = 200
INPUT_BAR_Y0 = 2110


# ---------------------------------------------------------------- 找重叠
def find_overlap_dy(prev_img, cur_img, method="template"):
    """相邻两屏的垂直位移 dy。

    dy > 0 表示 cur 相对 prev 内容向下移动 dy px（看更早方向）。
    取 prev 中间横带在 cur 里配准，避开顶部/底部残缺消息的干扰。
    """
    H = prev_img.shape[0]

    if method == "phase":
        pg = cv2.cvtColor(prev_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        cg = cv2.cvtColor(cur_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        shift, resp = cv2.phaseCorrelate(pg, cg)
        return float(shift[1]), float(resp)

    # 双横带：顶部横带（看更早/内容下移，滚动后在 cur 底部）+ 底部横带（看更新/内容上移）。
    # 顶部横带支持滚动到 H-700≈1640px，远大于旧"中间横带"的 870px 上限。
    cg = cv2.cvtColor(cur_img, cv2.COLOR_BGR2GRAY)
    best_dy, best_conf = 0.0, -1.0
    for y0, y1 in ((200, 700), (H - 700, H - 200)):
        band = prev_img[y0:y1, :]
        bg = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(cg, bg, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        dy = maxloc[1] - y0
        if maxv > best_conf:
            best_dy, best_conf = dy, maxv
    return float(best_dy), float(best_conf)


# ---------------------------------------------------------------- 差分切片
def slice_new_part(cur_img, dy, direction="earlier"):
    """cur_img 删掉和 prev 重叠的部分，返回新增切片。

    direction='earlier'（看更早/内容下移 dy>0）: 新增在顶部 [0:dy]
    direction='later'（看更新/内容上移 dy<0）: 新增在底部 [H+dy:]
    返回 (new_part, overlap_part)。
    """
    H = cur_img.shape[0]
    dy = int(round(dy))
    if direction == "earlier":
        if dy <= 0:
            return None, None
        dy = min(dy, H)
        return cur_img[0:dy, :], cur_img[dy:H, :]
    else:
        if dy >= 0:
            return None, None
        dy = max(dy, -H)
        return cur_img[H + dy:H, :], cur_img[0:H + dy, :]


# ---------------------------------------------------------------- 拼接长图（离线验证）
def stitch_sequence(imgs, direction="earlier", min_overlap=50):
    """把连续截图序列差分拼接成一张连续长图（离线验证用）。

    验证「找重叠 + 删重叠 + 累加」能否把相邻屏正确拼起来。
    返回拼接长图；若某相邻屏重叠过小（对不上），返回 None。
    """
    if not imgs:
        return None
    canvas = imgs[0]
    for i in range(1, len(imgs)):
        prev, cur = imgs[i - 1], imgs[i]
        dy, conf = find_overlap_dy(prev, cur)
        if conf < 0.5:
            return None
        new_part, _ = slice_new_part(cur, dy, direction)
        if new_part is None or new_part.shape[0] < min_overlap:
            return None
        if direction == "earlier":
            canvas = np.vstack([new_part, canvas])
        else:
            canvas = np.vstack([canvas, new_part])
    return canvas


# ---------------------------------------------------------------- 主循环骨架
def scroll_scan(cap, classify, slice_chat, find_dy=find_overlap_dy,
                direction="earlier", max_rounds=60, on_complete=None):
    """滚动识别主循环：两屏差分拼接 + 四态输出（依赖注入，真机/离线皆可）。

    返回 (complete_messages, rounds)。
    """
    complete = []
    pending = None
    prev_img = cap()

    for r in range(max_rounds):
        cur_img = cap()
        dy, conf = find_dy(prev_img, cur_img)
        if conf < 0.5:
            break
        new_part, _ = slice_new_part(cur_img, dy, direction)
        if new_part is None:
            break

        if pending is not None:
            stitched = (np.vstack([pending, new_part]) if direction == "earlier"
                        else np.vstack([new_part, pending]))
        else:
            stitched = new_part

        msgs = slice_chat(stitched)
        pending = None
        for m in msgs:
            c = classify(m)
            if c["state"] == "complete":
                complete.append(m)
                if on_complete:
                    on_complete(m)
            elif c["state"] in ("top_clipped", "bottom_clipped", "both_clipped"):
                pending = _crop_msg(stitched, m, c)

        prev_img = cur_img
        if pending is None:
            break

    return complete, r + 1


def _crop_msg(img, m, c):
    y0 = max(0, int(c.get("y_top", 0)))
    y1 = min(img.shape[0], int(c.get("y_bottom", img.shape[0])))
    return img[y0:y1, :]
