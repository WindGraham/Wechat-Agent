# -*- coding: utf-8 -*-
"""fling_physics.py — Android fling 物理模型封装。

复刻 Android ``OverScroller``/``Scroller`` 的 fling 减速曲线（与 Chrome
``ui/events/android/scroller.cc``、Firefox ``AndroidFlingPhysics.cpp`` 三方
独立实现逐常量一致），把「手指滑动参数（位移 / 时长 / 释放速度）」与
「屏幕实际滚动距离」互相换算，供 scroll_scan 等滚动采集脚本做确定性滑动。

参考实现：
- Firefox AndroidFlingPhysics.cpp（注释明确声明 adapted from Chrome）
- Chromium ui/events/android/scroller.cc
- AOSP OverScroller.java

物理模型关键结论
----------------
1. 实际滚动 = 拖动段 + 惯性段：scroll = (swipe - slop) + fling(velocity)
2. 拖动段与手指位移 1:1（扣掉 touch slop），松手即停；
   只有当释放速度 v >= minFlingVelocity 时才额外进入惯性段。
3. 惯性段距离对速度是幂律：fling ∝ v^(R/(R-1))，R=2.358，指数≈1.736。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ---- Android 默认常量（ViewConfiguration / Scroller） ----
GRAVITY_EARTH = 9.80665        # g (m/s^2)
INCH_PER_METER = 39.37         # inch / meter
LOOK_AND_FEEL = 0.84           # look and feel tuning
FRICTION = 0.015               # ViewConfiguration.getScrollFriction() 默认值
INFLEXION = 0.35               # 速度曲线张力拐点
DECELERATION_RATE = math.log(0.78) / math.log(0.9)   # ≈ 2.3582018

# dp/s（ViewConfiguration.getMinimumFlingVelocity / getMaximumFlingVelocity）
MIN_FLING_VELOCITY_DP = 50.0
MAX_FLING_VELOCITY_DP = 8000.0

# touch slop（dp）—— 滚动真正开始前被消耗掉的手指位移
TOUCH_SLOP_DP = 8.0

# 一加 6T 实测 DPI（见 workspace/IMAGEREADER_SERVICE_PLAN.md）
DEFAULT_DPI = 420.0


# --------------------------------------------------------------------------
# 基础换算
# --------------------------------------------------------------------------
def _density(dpi: float) -> float:
    return dpi / 160.0


def physical_coeff(dpi: float) -> float:
    """mPhysicalCoeff = g × 39.37 × dpi × 0.84，单位 px/s²（加速度）。"""
    return GRAVITY_EARTH * INCH_PER_METER * dpi * LOOK_AND_FEEL


def touch_slop_px(dpi: float) -> float:
    """touch slop（px）—— 手指移动超过它，列表才开始跟手滚动。"""
    return TOUCH_SLOP_DP * _density(dpi)


def min_fling_velocity_px(dpi: float) -> float:
    """触发 fling 的最小释放速度（px/s）。低于它只有拖动、无惯性。"""
    return MIN_FLING_VELOCITY_DP * _density(dpi)


def max_fling_velocity_px(dpi: float) -> float:
    """系统允许的最大释放速度（px/s）。超过会被 clamp。"""
    return MAX_FLING_VELOCITY_DP * _density(dpi)


# --------------------------------------------------------------------------
# 正算：速度 → 惯性段距离 / 时长
# --------------------------------------------------------------------------
def spline_deceleration(velocity: float, dpi: float,
                        friction: float = FRICTION,
                        inflexion: float = INFLEXION) -> float:
    """splineDecel = ln( inflexion × |v| / (friction × coeff) )。"""
    v = abs(velocity)
    coeff = physical_coeff(dpi)
    return math.log(inflexion * v / (friction * coeff))


def fling_distance(velocity: float, dpi: float,
                   friction: float = FRICTION,
                   inflexion: float = INFLEXION) -> float:
    """释放速度 velocity（px/s）对应的惯性段滚动距离（px）。

    distance = friction × coeff × exp( R/(R-1) × splineDecel )
             = friction × coeff × ( inflexion×v / (friction×coeff) )^(R/(R-1))
    """
    v = abs(velocity)
    if v == 0.0:
        return 0.0
    coeff = physical_coeff(dpi)
    l = spline_deceleration(v, dpi, friction, inflexion)
    return friction * coeff * math.exp(DECELERATION_RATE / (DECELERATION_RATE - 1.0) * l)


def fling_duration(velocity: float, dpi: float,
                   friction: float = FRICTION,
                   inflexion: float = INFLEXION) -> float:
    """释放速度 velocity（px/s）对应的惯性滚动时长（秒）。"""
    v = abs(velocity)
    if v == 0.0:
        return 0.0
    l = spline_deceleration(v, dpi, friction, inflexion)
    return math.exp(l / (DECELERATION_RATE - 1.0))


# --------------------------------------------------------------------------
# 反算：惯性段距离 → 所需释放速度
# --------------------------------------------------------------------------
def velocity_for_fling_distance(distance: float, dpi: float,
                                friction: float = FRICTION,
                                inflexion: float = INFLEXION) -> float:
    """反解：要得到 distance（px）的惯性滚动，需要多大的释放速度（px/s）。

    v = (friction×coeff / inflexion) × ( distance/(friction×coeff) )^((R-1)/R)
    """
    d = abs(distance)
    if d == 0.0:
        return 0.0
    coeff = physical_coeff(dpi)
    return (friction * coeff / inflexion) * \
        (d / (friction * coeff)) ** ((DECELERATION_RATE - 1.0) / DECELERATION_RATE)


# --------------------------------------------------------------------------
# 总滚动预测：给定手指位移 + 时长，预测屏幕实际滚动距离
# --------------------------------------------------------------------------
def predict_scroll(swipe_px: float, duration_s: float, dpi: float = DEFAULT_DPI) -> float:
    """预测一次 input swipe（位移 swipe_px、时长 duration_s）的实际滚动距离。

    - 释放速度 v = swipe_px / duration_s
    - v < minFling → 纯拖动：scroll = swipe_px - slop
    - v ≥ minFling → 拖动 + 惯性：scroll = (swipe_px - slop) + fling_distance(v)
    - v > maxFling → 速度被系统 clamp 到 maxFling 后再算惯性段
    """
    slop = touch_slop_px(dpi)
    v_min = min_fling_velocity_px(dpi)
    v_max = max_fling_velocity_px(dpi)

    drag = max(0.0, swipe_px - slop)
    if duration_s <= 0.0:
        v = math.inf
    else:
        v = swipe_px / duration_s

    if v < v_min:
        return drag
    v = min(v, v_max)
    return drag + fling_distance(v, dpi)


# --------------------------------------------------------------------------
# 方案求解：给定期望滚动距离 + 一个参数 → 反解其余参数
# --------------------------------------------------------------------------
@dataclass
class SwipePlan:
    distance_px: float        # 期望滚动距离（输入）
    swipe_px: float           # 手指位移（px）
    duration_s: float         # 时长（秒）
    velocity_px_s: float      # 释放速度（px/s）
    mode: str                 # 'drag' | 'fling'
    drag_px: float            # 拖动段实际滚动
    fling_px: float           # 惯性段实际滚动
    predicted_px: float       # 按模型预测的总滚动
    feasible: bool            # 是否能精确达到期望距离
    note: str = ""

    @property
    def duration_ms(self) -> float:
        return self.duration_s * 1000.0

    def __str__(self) -> str:
        return (
            f"期望滚动 {self.distance_px:.0f}px → "
            f"手指位移 {self.swipe_px:.0f}px, 时长 {self.duration_ms:.0f}ms "
            f"(v={self.velocity_px_s:.0f}px/s, {self.mode})\n"
            f"  拖动段 {self.drag_px:.0f}px + 惯性段 {self.fling_px:.0f}px "
            f"= 预测 {self.predicted_px:.0f}px "
            f"[{'可达' if self.feasible else '不可达'}]"
            + (f"\n  ⚠ {self.note}" if self.note else "")
        )


def plan_swipe(distance: float,
               duration: float | None = None,
               swipe: float | None = None,
               dpi: float = DEFAULT_DPI) -> SwipePlan:
    """给定期望滚动距离 distance（px），固定 duration（秒）或
    swipe（手指位移 px）之一，反解出另一参数并返回完整方案。

    - 传 duration → 反解 swipe（二分，单调函数）
    - 传 swipe → 反解 duration
    - 都不传 → 默认确定性「拖动」模式（速度压到最小 fling 阈值以下）
    """
    if duration is not None and swipe is not None:
        raise ValueError("duration 与 swipe 只能给其中一个")
    if distance < 0:
        raise ValueError("distance 必须 >= 0")

    slop = touch_slop_px(dpi)
    v_min = min_fling_velocity_px(dpi)
    v_max = max_fling_velocity_px(dpi)

    # ---- 都不传：纯拖动（确定性最强） ----
    if duration is None and swipe is None:
        swipe = distance + slop                       # 拖动段 = 手指位移 - slop
        duration = swipe / (v_min * 0.5)              # 速度压在阈值一半，确保纯拖动
        return _make_plan(distance, swipe, duration, dpi)

    # ---- 固定时长 → 反解手指位移 ----
    if duration is not None:
        if duration <= 0.0:
            raise ValueError("duration 必须 > 0")
        # f(S) = predict_scroll(S, duration) 关于 S 单调递增；
        # f(slop)=0，f(distance+slop) >= distance（纯拖动已达 distance，惯性只增不减）
        lo, hi = slop, distance + slop
        for _ in range(200):
            mid = (lo + hi) / 2.0
            if predict_scroll(mid, duration, dpi) < distance:
                lo = mid
            else:
                hi = mid
        swipe = (lo + hi) / 2.0
        return _make_plan(distance, swipe, duration, dpi)

    # ---- 固定手指位移 → 反解时长 ----
    drag = max(0.0, swipe - slop)
    if distance <= drag:
        if math.isclose(distance, drag, abs_tol=1e-6):
            # 恰好等于拖动段：任意足够慢的时长都行，取最小安全时长
            duration = swipe / (v_min * 0.5)
            return _make_plan(distance, swipe, duration, dpi)
        raise ValueError(
            f"手指位移 {swipe:.0f}px 的拖动段就已有 {drag:.0f}px，"
            f"已超过期望 {distance:.0f}px；请减小 swipe（≤ {distance + slop:.0f}px）")

    fling_needed = distance - drag
    v = velocity_for_fling_distance(fling_needed, dpi)
    if v < v_min:
        # 死区：fling_needed 太小，触发不了惯性（纯拖动又到不了 distance）
        v_clamped = v_min
        duration = swipe / v_clamped
        return _make_plan(distance, swipe, duration, dpi, feasible=False,
                          note=f"死区：期望 {distance:.0f}px 介于拖动段 {drag:.0f}px 与 "
                               f"最小 fling {drag + fling_distance(v_min, dpi):.0f}px 之间，"
                               f"无法精确命中；已按最小 fling 给出最近解")
    if v > v_max:
        v_clamped = v_max
        duration = swipe / v_clamped
        return _make_plan(distance, swipe, duration, dpi, feasible=False,
                          note=f"所需速度 {v:.0f}px/s 超上限 {v_max:.0f}px/s，"
                               f"惯性段会被 clamp；请增大 swipe 才能达到 {distance:.0f}px")
    duration = swipe / v
    return _make_plan(distance, swipe, duration, dpi)


def _make_plan(distance: float, swipe: float, duration: float, dpi: float,
               feasible: bool = True, note: str = "") -> SwipePlan:
    slop = touch_slop_px(dpi)
    v_min = min_fling_velocity_px(dpi)
    v_max = max_fling_velocity_px(dpi)

    v = swipe / duration if duration > 0 else math.inf
    drag = max(0.0, swipe - slop)
    if v < v_min:
        mode, fling = "drag", 0.0
    else:
        mode, fling = "fling", fling_distance(min(v, v_max), dpi)
    predicted = drag + fling
    return SwipePlan(
        distance_px=distance, swipe_px=swipe, duration_s=duration,
        velocity_px_s=v, mode=mode, drag_px=drag, fling_px=fling,
        predicted_px=predicted, feasible=feasible, note=note,
    )


# --------------------------------------------------------------------------
# 命令行演示
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Android fling 物理模型：距离/位移/时长互算")
    p.add_argument("distance", type=float, help="期望滚动距离 (px)")
    p.add_argument("--duration", type=float, default=None, help="固定时长 (秒)")
    p.add_argument("--swipe", type=float, default=None, help="固定手指位移 (px)")
    p.add_argument("--dpi", type=float, default=DEFAULT_DPI, help="屏幕 DPI")
    p.add_argument("--predict", action="store_true",
                   help="把 distance 当手指位移，配合 --duration 只预测实际滚动")
    a = p.parse_args()

    if a.predict:
        if a.duration is None:
            p.error("--predict 需要 --duration")
        print(f"swipe={a.distance:.0f}px, {a.duration * 1000:.0f}ms → "
              f"预测滚动 {predict_scroll(a.distance, a.duration, a.dpi):.0f}px")
    else:
        print(plan_swipe(a.distance, duration=a.duration, swipe=a.swipe, dpi=a.dpi))
