#!/usr/bin/env python3
"""bubble_model.py - 文字泡几何数学模型（v2 新增）。

用途：
1. OCR 漏行检测：实测泡高 h -> 暗示行数 n_impl，n_impl > OCR 行数时触发补读；
2. 合理性校验：实测宽与预测宽偏差过大 -> low_confidence，触发区域重识别；
3. 字数反推：OCR 失败的泡用 (w,h) 反推等效字数与行数，
   输出 `[无法识别文本: 约N字M行]` 占位（保证回填对齐时格式严谨）。

常量由 tools/calibrate_layout.py 对 18 张样本回归标定（2026-08-04）：
- 单行泡高 H1 = 108（38 个单行泡均值 110.3，闭运算粘昵称行会 +2~8）
- 行距 PITCH = 55（两行泡 162 = 108 + 54；文本行中心距 59）
- 字宽 C_CJK = 45（9 个单行泡回归 43~48）；单侧内边距 PAD_X = 41
- 单行上限 M_MAX = 15 CJK 字：15 字泡宽 756 = 15*45 + 2*41
- 超宽 regime：超长文本（>4 行）泡宽 ≈911，每行 ≈18 字（t6/p_leisure 实测）
"""

import math

# ---------------------------------------------------------------- 标定常量
H1 = 108            # 单行气泡高
PITCH = 55          # 每多一行增加的高度
C_CJK = 45.0        # CJK 字宽（px）
C_ASC_RATIO = 0.55  # ASCII 字宽 = C_CJK * 0.55
PAD_X = 41          # 气泡水平内边距（单侧）
W_MAX = 756         # 常规单行最大泡宽
M_MAX = 15          # 常规单行最大等效 CJK 字数
W_WIDE = 911        # 超宽泡宽（超长文本）
M_WIDE = 18         # 超宽泡单行字数
WIDE_MIN_LINES = 5  # 预测行数 >= 此值按超宽 regime 处理


def eff_len(text):
    """文本等效 CJK 字长：m_eff = n_cjk + 0.55 * n_ascii"""
    n_cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
    n_asc = len(text) - n_cjk
    return n_cjk + C_ASC_RATIO * n_asc


def height_for_lines(n):
    """n 行气泡的预测高度 H(n) = H1 + (n-1) * PITCH"""
    return H1 + (max(1, n) - 1) * PITCH


def implied_lines(h):
    """实测泡高 -> 暗示行数（>=1）"""
    return max(1, round((h - H1) / PITCH) + 1)


def is_wide(text):
    """超长文本进入超宽 regime（泡宽 ~911，每行 ~18 字）"""
    return predict_lines_normal(text) >= WIDE_MIN_LINES


def predict_lines_normal(text):
    return max(1, math.ceil(eff_len(text) / M_MAX))


def predict_lines(text):
    """文本 -> 预测行数（手动换行各自折行后求和）"""
    n = 0
    for ln in text.split("\n"):
        n += max(1, math.ceil(eff_len(ln) / M_MAX))
    if n >= WIDE_MIN_LINES:
        m = eff_len(text)
        return max(1, math.ceil(m / M_WIDE))
    return max(1, n)


def predict_width(text, n_lines=None):
    """文本 -> 预测气泡宽。
    逐条已有行计算（手动换行的行可能不满），取最宽行；
    无手动换行的长文：首行必填满 M_MAX，宽 = 满行宽。"""
    lines = text.split("\n")
    if predict_lines(text) >= WIDE_MIN_LINES:
        return W_WIDE
    w = 0.0
    for ln in lines:
        m = eff_len(ln)
        w = max(w, min(m, M_MAX) * C_CJK + 2 * PAD_X)
    return w


def width_ok(measured_w, text, tol=60):
    """合理性校验：实测宽与预测宽偏差是否在容差内。
    超宽 regime 下只检查泡宽是否接近 W_WIDE。"""
    if not text.strip():
        return True
    if predict_lines(text) >= WIDE_MIN_LINES:
        return abs(measured_w - W_WIDE) <= 60
    return abs(measured_w - predict_width(text)) <= tol


def lines_consistent(measured_h, text):
    """实测高度暗示行数与文本行数是否一致"""
    n_text = max(text.count("\n") + 1, 1) if text.strip() else 0
    if n_text == 0:
        return True
    return implied_lines(measured_h) <= n_text


def infer_unknown(w, h):
    """OCR 失败的文字泡：由 (w,h) 反推等效字数与行数，输出占位文本"""
    n = implied_lines(h)
    wide = w > (W_MAX + W_WIDE) / 2
    m_line = M_WIDE if wide else M_MAX
    per_line = max(1.0, (w - 2 * PAD_X) / C_CJK)
    if n > 1:
        chars = int(round(m_line * (n - 1) + min(per_line, m_line)))
    else:
        chars = int(round(min(per_line, m_line)))
    return f"[无法识别文本: 约{chars}字{n}行]", chars, n
