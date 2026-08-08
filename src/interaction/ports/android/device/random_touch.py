# -*- coding: utf-8 -*-
"""random_touch.py — 随机化触控框架 v2（用户铁律逐条落地）。

依据 docs/V2_RESEARCH_NOTES.md 方向4 §3：

- 一切可点组件都是 Rect，tap(rect) 内部高斯偏中心随机选点，clamp 到 inset 内
  → 落点永不偏出组件（安全边界与随机化并列）。
- 按压时长拟人：用 `input swipe x y x y <40~120ms>` 模拟真人按压，替代瞬时 tap。
- swipe_zone：起止点各自在区域内随机、二次贝塞尔轨迹、支持斜滑
  （|横向偏移| ≤ 0.4×|纵向位移|，超过会被系统/列表判成横滑，这是语义安全边界）、
  分段链式 input swipe、时长先快后慢 + ±10% 抖动、轨迹全部 clamp 在 zone 内。

RandomTouch 不直接依赖 DeviceCtl（避免循环 import），构造时注入 shell 执行函数
（签名 shell(cmd_str, timeout=...) -> bytes）。
"""

import logging
import random
import time
from dataclasses import dataclass

log = logging.getLogger("random_touch")


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def from_xyxy(cls, x0, y0, x1, y1):
        return cls(int(x0), int(y0), int(x1 - x0), int(y1 - y0))

    @property
    def center(self):
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)

    def inset(self, ratio=0.15):
        """四边内缩，保证落点在组件内部。"""
        dx = self.w * ratio
        dy = self.h * ratio
        return Rect(int(self.x + dx), int(self.y + dy),
                    max(1, int(self.w - 2 * dx)), max(1, int(self.h - 2 * dy)))

    def contains(self, px, py):
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def clamp_point(self, px, py):
        """点钳制到本 rect 范围内。"""
        cx = min(max(px, self.x), self.x + self.w)
        cy = min(max(py, self.y), self.y + self.h)
        return (int(round(cx)), int(round(cy)))

    def __repr__(self):
        return f"Rect(x={self.x},y={self.y},w={self.w},h={self.h})"


class RandomTouch:
    def __init__(self, shell_fn, sleep_fn=time.sleep):
        self._shell = shell_fn
        self._sleep = sleep_fn

    # ------------------------------------------------------------------ 点按
    def _pick_point(self, rect, sigma_ratio=0.25):
        """区域内随机采样一点：高斯偏向中心，σ=min(w,h)*sigma_ratio，
        采样结果 clamp 到 rect.inset() 内 → 永不偏出组件。"""
        inner = rect.inset()
        cx, cy = inner.center
        sigma = min(inner.w, inner.h) * sigma_ratio
        px, py = cx, cy
        for _ in range(8):
            px = cx + random.gauss(0, sigma)
            py = cy + random.gauss(0, sigma)
            if inner.contains(px, py):
                break
        return inner.clamp_point(px, py)

    def tap_rect(self, rect, sigma_ratio=0.25, dwell_ms=(40, 120)):
        """区域内随机点：高斯偏向中心，σ=min(w,h)*sigma_ratio，
        采样结果 clamp 到 rect.inset() 内 → 永不偏出组件。"""
        px, py = self._pick_point(rect, sigma_ratio)
        self._tap_with_dwell(px, py, dwell_ms)
        return (px, py)

    def long_press_rect(self, rect, sigma_ratio=0.25, dwell_ms=(500, 800)):
        """区域内随机点长按：input swipe x y x y <500~800ms>。

        按压时长超过系统长按阈值（~500ms），触发长按菜单/多选等。
        落点采样与 tap_rect 同一框架（高斯偏中心 + clamp 进 inset）。"""
        px, py = self._pick_point(rect, sigma_ratio)
        d = random.randint(*dwell_ms)
        log.debug("long_press_rect (%d,%d) %dms", px, py, d)
        self._shell(f"input swipe {px} {py} {px} {py} {d}")
        return (px, py)

    def double_tap_rect(self, rect, interval_ms=(100, 220), sigma_ratio=0.25):
        """双击：同一 rect 内两次独立随机点按，间隔 interval_ms 随机。
        两次落点各自采样（真人双击也不会落在同一像素）。返回 (pt1, pt2)。"""
        p1 = self.tap_rect(rect, sigma_ratio=sigma_ratio)
        gap = random.uniform(*interval_ms) / 1000.0
        log.debug("double_tap gap %.0fms", gap * 1000)
        self._sleep(gap)
        p2 = self.tap_rect(rect, sigma_ratio=sigma_ratio)
        return (p1, p2)

    def _tap_with_dwell(self, x, y, dwell_ms=(40, 120)):
        """用 input swipe x y x y <40~120ms> 模拟真人按压时长。"""
        d = random.randint(*dwell_ms)
        log.debug("tap_with_dwell (%d,%d) %dms", x, y, d)
        self._shell(f"input swipe {x} {y} {x} {y} {d}")

    # ------------------------------------------------------------------ 滑动
    def swipe_zone(self, zone, direction="up",
                   length_ratio=(0.35, 0.65),
                   diag_ratio=0.30,
                   duration_ms=(250, 550),
                   n_points=None):
        """区域内随机化滑动。

        direction: up/down（沿 zone 纵轴）或 left/right（沿横轴）。
        - 滑动长度 = zone 主轴 extent × length_ratio 区间随机
        - 起/终点在交叉轴上各自独立随机（始末位置随机）
        - 斜滑：交叉轴位移 ≤ length×diag_ratio（且 diag_ratio 必须 ≤0.4，
          超过会被列表判成横滑——语义安全边界）
        - 轨迹：二次贝塞尔，控制点 = 中点 + 垂直方向 gauss(0, length*0.06)
          截断 ±60px；采样 4~7 点，全部 clamp 到 zone.inset() 内
        - 分段链式 input swipe，每段时长按先快后慢权重分配 + ±10% 抖动
        返回 (起点, 终点)。
        """
        assert diag_ratio <= 0.4, "斜滑横向偏移不得超过 0.4 倍纵向位移（防横滑误判）"
        inner = zone.inset(0.08)
        vertical = direction in ("up", "down")
        extent = inner.h if vertical else inner.w          # 主轴长度
        cross_lo = inner.x if vertical else inner.y        # 交叉轴区间
        cross_hi = (inner.x + inner.w) if vertical else (inner.y + inner.h)

        sign = -1 if direction in ("up", "left") else 1
        length = extent * random.uniform(*length_ratio)
        length = min(length, extent)

        # 主轴起点：保证 起点 + sign*length 仍在 [axis_lo, axis_hi]
        axis_lo = inner.y if vertical else inner.x
        axis_hi = (inner.y + inner.h) if vertical else (inner.x + inner.w)
        if sign < 0:
            start_axis = random.uniform(axis_lo + length, axis_hi)
        else:
            start_axis = random.uniform(axis_lo, axis_hi - length)
        end_axis = start_axis + sign * length

        # 交叉轴：起终点独立随机，再约束位移 ≤ length*diag_ratio
        c1 = random.uniform(cross_lo, cross_hi)
        max_dc = length * diag_ratio
        dc = random.uniform(-max_dc, max_dc)
        c2 = min(max(c1 + dc, cross_lo), cross_hi)

        if vertical:
            p1, p2 = (c1, start_axis), (c2, end_axis)
        else:
            p1, p2 = (start_axis, c1), (end_axis, c2)

        pts = self._bezier_points(p1, p2, length, inner,
                                  n_points or random.randint(4, 7))
        self._run_swipe_chain(pts, duration_ms)
        log.debug("swipe_zone %s %s: %s -> %s (%d pts)", direction, zone,
                  pts[0], pts[-1], len(pts))
        return (pts[0], pts[-1])

    @staticmethod
    def _bezier_points(p1, p2, length, zone, n):
        """二次贝塞尔：控制点在中点 + 垂直于连线方向 gauss(0, length*0.06)，
        截断 ±60px；采样 n 点（含首尾），全部 clamp 到 zone 内。"""
        x1, y1 = p1
        x2, y2 = p2
        dx, dy = x2 - x1, y2 - y1
        L = max(1.0, (dx * dx + dy * dy) ** 0.5)
        ux, uy = -dy / L, dx / L                          # 垂直方向单位向量
        off = max(-60.0, min(60.0, random.gauss(0, length * 0.06)))
        mx, my = (x1 + x2) / 2 + ux * off, (y1 + y2) / 2 + uy * off
        pts = []
        for i in range(n):
            t = i / (n - 1)
            bx = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * mx + t ** 2 * x2
            by = (1 - t) ** 2 * y1 + 2 * (1 - t) * t * my + t ** 2 * y2
            pts.append(zone.clamp_point(bx, by))
        return pts

    def _run_swipe_chain(self, pts, duration_ms):
        """分段链式 input swipe：先快后慢（权重递增）+ ±10% 抖动。"""
        total = random.randint(*duration_ms)
        n_seg = len(pts) - 1
        weights = [i + 1 for i in range(n_seg)]
        total_w = sum(weights)
        cmds = []
        for i in range(n_seg):
            d = max(60, int(total * weights[i] / total_w * random.uniform(0.9, 1.1)))
            (ax, ay), (bx, by) = pts[i], pts[i + 1]
            cmds.append(f"input swipe {ax} {ay} {bx} {by} {d}")
        self._shell(";".join(cmds), timeout=total / 1000 + 15)
