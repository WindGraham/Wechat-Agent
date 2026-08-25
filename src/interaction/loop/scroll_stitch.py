# -*- coding: utf-8 -*-
"""scroll_stitch.py — 滚动对齐核心：相邻两屏垂直位移检测 + 两屏缝合。

2026-08-13 收敛：删除「子图差分拼接」相关死代码（slice_new_part / stitch_sequence /
scroll_scan / _crop_msg）。原因是差分拼接方案有根本缺陷——slice_new_part 把重叠区
cur[dy:H] 丢掉，但一条消息会横跨「新内容/重叠区」分界线，它的另一半（在重叠区里）
永远找不回来；且拼接顺序曾写反（earlier 方向 vstack([pending,new_part]) 应为
vstack([new_part,pending])）。生产采集已改用「整屏识别 + 模糊去重」（见
history_collect.py），不再走差分拼接，这里只保留仍然在用的位移检测。

2026-08-14（设计定稿 docs/DESIGN_OVERLAP_STITCH_COLLECTION.md）：
新增 stitch_union——按设计 §5/§6 把相邻两屏的【内容区】按实测位移 dy 拼成
识别 union（cur[0:dy] 在上=更旧，prev 在下=更新，接缝内容连续）。这等价于
A/B 差分缝合（A2 在上 + B1 在下），且不丢任何一行：≤1 屏高的消息必然被
某次 union 完整包含（前提 0 < dy < 消息区高）。

保留：
  - find_overlap_dy：相邻两屏垂直位移（matchTemplate / phaseCorrelate），
    供「滑到没变化」判到底 与 脚本对齐验证使用。
  - stitch_union：两屏内容区缝合 union（识别与书签检测共用）。

纯函数，无真机依赖，可用离线截图序列单测。
"""

import cv2
import numpy as np

CONTENT_Y0 = 200
INPUT_BAR_Y0 = 2110


# ---------------------------------------------------------------- 找重叠
def _band_texture(band):
    """横带纹理度：非背景(>40)像素占比 + 行内方差，纯背景带返回低值。"""
    intense = band.max(axis=2)
    return float((intense > 40).mean())


def _pixdiff_at(prev_img, cur_img, dy):
    """重叠带像素差验证：cur 顶部 [0,H-dy] vs prev 底部 [dy,H] 的 absdiff 均值。
    假匹配（模板 conf 高但位置错）的像素差大；真匹配的像素差小。"""
    H = cur_img.shape[0]
    if not (0 < dy < H):
        return 1e9
    a = cur_img[0:H - dy]
    b = prev_img[dy:H]
    if a.shape != b.shape:
        return 1e9
    return float(cv2.absdiff(a, b).mean())


def _dy_multi_band(prev_img, cur_img, n_bands=6, min_band=90):
    """多带共识 + 像素差验证的位移检测。

    - 取 n_bands 个不同 y0 的横带（均匀分布，避开顶部固定 UI 区），
      每带纹理足够才参与；
    - 每带 matchTemplate 得候选 (dy, conf)，只保留 conf>=0.55 的；
    - 对每个候选 dy 做【像素差验证】，选像素差最小的——模板假匹配
      (conf 高但位置错) 的像素差必然大，被剔除；
    - 若多个带共识指向同一 dy（|Δ|<20），以共识 dy 为准。
    返回 (dy, conf)；conf=像素差验证后的可信度（0~1，越大越可信）。
    """
    H = prev_img.shape[0]
    cg = cv2.cvtColor(cur_img, cv2.COLOR_BGR2GRAY)
    band_h = max(min_band, H // n_bands)
    step = max(band_h, (H - band_h) // max(1, n_bands - 1))
    # dy 物理范围：小步长慢拖 dy≈300-340（重叠巨大 1400px+），
    # fling 大位移 dy≈1250-1700。下限取 100（<100 视为未滚动，
    # 由调用方 dy<40 分支处理），上限 H-30（无重叠）。此范围是采集
    # 动作的物理约束（步长可小可大），非人工校准值。
    DY_MIN, DY_MAX = 100, H - 30
    cands = []          # (dy, conf, pixdiff)
    for y0 in range(0, H - band_h, step):
        band = prev_img[y0:y0 + band_h, :]
        if _band_texture(band) < 0.08:
            continue                      # 纯背景横带跳过（防误匹配）
        bg = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(cg, bg, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        if maxv < 0.55:
            continue
        dy = maxloc[1] - y0
        if not (DY_MIN <= dy <= DY_MAX):
            continue
        pd = _pixdiff_at(prev_img, cur_img, dy)
        cands.append((dy, float(maxv), pd))
    if not cands:
        return None, 0.0

    # 优先级：conf 最高 > 像素差最小。像素差只用于【剔除假匹配】
    # （conf 高但像素差极大 = 位置错），不作为主排序——内容真实变化时
    # 所有 dy 的像素差都大，像素差失去区分度，此时 conf 高的带更可信
    # （实测：顶部带 conf 0.999 的 dy 才是真位移，像素差小的 1140 是次优）。
    best = max(cands, key=lambda t: t[1])       # conf 最高者
    # 共识：有多少带落在 best.dy 附近
    agree = sum(1 for dy, _, _ in cands if abs(dy - best[0]) < 20)
    # conf 计算：模板 conf 高时直接信任（像素差只用于剔除极端假匹配）
    # 像素差 0~50 是正常范围（气泡阴影/渐变/压缩噪声），不应过度拉低 conf
    if best[1] >= 0.9:
        # 模板 conf ≥ 0.9：高度可信，只做极端假匹配剔除
        conf = best[1] if best[2] < 80 else best[1] * 0.5
    else:
        # 模板 conf < 0.9：像素差参与 conf 计算，但放宽归一化因子
        conf = best[1] * (1.0 / (1.0 + best[2] / 50.0))
    if agree >= 2:
        conf = max(conf, 0.85)                    # 多带共识 → 高可信
    return best[0], float(conf)


def find_overlap_dy(prev_img, cur_img, method="template"):
    """相邻两屏的垂直位移 dy（在【裁切后的消息区】上测，2026-08-14 用户定稿：
    重叠检测用裁切消息区对比，排除置顶条/输入栏等固定 UI 污染）。

    dy > 0 表示 cur 相对 prev 内容向下移动 dy px（看更早方向）。

    2026-08-15 智能版：多带共识 + 像素差验证（_dy_multi_band）。
      - 旧版固定顶部横带 → 大 dy(>H-横带高) 假匹配、纯背景带误配；
      - 新版横带自适应 H//n_bands、纹理过滤、像素差验证剔除假匹配、
        多带共识加分。全流程无人工校准阈值（conf/像素差阈值是物理量）。
    """
    H = prev_img.shape[0]
    if method == "phase":
        pg = cv2.cvtColor(prev_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        cg = cv2.cvtColor(cur_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        shift, resp = cv2.phaseCorrelate(pg, cg)
        return float(shift[1]), float(resp)

    # 先裁切内容区（排除置顶条/输入栏固定 UI），再做 dy 检测
    # 否则固定 UI 不随滑动变化 → conf 高但 dy=0 → 被排除，只剩中间变化大的带
    # 注意：调用方可能已传入裁切图（H<2000），此时 bottom 即图底，不再检测输入栏
    from ..ports.android.perception.page_detector import (
        detect_input_bar_top, detect_pinned_bar_end)
    if H < 2000:
        # 裁切图：内容区即整图
        top = 0
        bottom = H
    else:
        top = detect_pinned_bar_end(prev_img) or CONTENT_Y0
        bottom = detect_input_bar_top(prev_img) or INPUT_BAR_Y0
        bottom = max(bottom, top + 1)
    prev_c = prev_img[top:bottom]
    cur_c = cur_img[top:bottom]

    dy, conf = _dy_multi_band(prev_c, cur_c)
    if dy is not None:
        return dy, conf
    # 兜底：全屏测量（置顶条等固定 UI 反成锚点）
    return _find_overlap_dy_fullscreen(prev_img, cur_img)


def _find_overlap_dy_fullscreen(prev_img, cur_img):
    """全屏测量位移（裁切版测不出时的兜底；置顶条等固定 UI 反成锚点）。

    2026-08-15 修复：输入可能是【裁切图】(H<2000) 而非整屏——detect_input_bar_top
    内部按全屏常量 SCREEN_H 扫描会越界(零尺寸数组崩溃)。按实际图高 H 适配：
    H<2000 时跳过输入栏检测（裁切图底部即内容区底），横带直接取 [200, H]。
    """
    from ..ports.android.perception.page_detector import detect_input_bar_top
    H = prev_img.shape[0]
    if H < 2000:
        # 裁切图：内容区即整图，横带取 [200, H-40]（底部保底 40px）
        input_top = H - 40
    else:
        input_top = detect_input_bar_top(cur_img) or INPUT_BAR_Y0
        input_top = max(input_top, H - 700 + 40)   # 保底：底部横带至少 40px 高
    cg = cv2.cvtColor(cur_img, cv2.COLOR_BGR2GRAY)
    best_dy, best_conf = 0.0, -1.0
    for y0, y1 in ((CONTENT_Y0, min(700, H)), (max(0, H - 700), input_top)):
        if y1 - y0 < 40:
            continue
        band = prev_img[y0:y1, :]
        bg = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(cg, bg, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        dy = maxloc[1] - y0
        if maxv > best_conf:
            best_dy, best_conf = dy, maxv
    return float(best_dy), float(best_conf)


# ---------------------------------------------------------------- 两屏缝合（union）
def stitch_union(prev_img, cur_img, dy):
    """把相邻两屏拼成识别 union（设计 §5/§6 的 A/B 差分缝合，整段不丢行）。

    滚动 older：cur 内容相对 prev 下移 dy。内容区（起点=内容顶，排除标题栏/
    置顶条固定 UI；终点=CV 实测输入栏顶）拼接：
        union = vstack([cur[内容顶:内容顶+dy],     # 更旧内容（上）
                        prev[内容顶:输入栏顶]])      # 更新内容（下）
    接缝连续：cur 内容行 k 与 prev 内容行 k-dy 是同一行，故 cur 段末行
    (dy-1) 与 prev 段首行 (0) 恰好相邻，无缺行无重行。

    返回 (union_img, content_y0=0, content_y1=union高)；dy 无效
    （<=0 或 >= 内容区高，即两屏无重叠/缝合不成立）返回 None，
    调用方回退单屏识别。
    """
    from ..ports.android.perception.page_detector import (
        detect_input_bar_top, detect_pinned_bar_end)
    dy = int(round(dy))
    if dy <= 0:
        return None
    top = detect_pinned_bar_end(prev_img) or CONTENT_Y0   # 排除置顶条
    bottom = detect_input_bar_top(prev_img) or INPUT_BAR_Y0
    bottom = max(bottom, top + 1)
    prev_c = prev_img[top:bottom]
    if dy >= prev_c.shape[0]:
        return None                      # 无重叠：缝合不成立（滑动异常）
    cur_c = cur_img[top:top + dy]
    union = np.vstack([cur_c, prev_c])
    return union, 0, union.shape[0]
