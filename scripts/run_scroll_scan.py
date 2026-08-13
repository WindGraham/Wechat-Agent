# -*- coding: utf-8 -*-
"""scripts/run_scroll_scan.py — 真机滚动识别闭环

把 scroll_stitch 主循环接上真机：
  device_ctl 随机步长滑动 + 截图 → slice_chat 识别 → classify_message 四态
  → 完整消息收集去重；相邻屏用 find_overlap_dy 对齐验证。

不依赖 ImageReader，用 screencap 先跑通逻辑。
"""

import sys
sys.path.insert(0, ".")
import random
from collections import Counter

from src.interaction.ports.android.device.device_ctl import DeviceCtl
from src.interaction.ports.android.perception.ocr_engine import run_ocr
from src.interaction.ports.android.perception.chat_slicer import slice_chat, classify_message
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher
from src.interaction.loop.scroll_stitch import find_overlap_dy
from src.shared.fling_physics import plan_swipe
from src.interaction.msglog.message_log import normalize

GROUP = "交流一下？"
N_PER_DIRECTION = 8   # 每个方向滑几屏
# 随机滚动范围（每次随机，但都用 fling 物理模型精确反解，随机也知道滚了多长）
SCROLL_RANGE = (1000, 1400)   # 期望滚动距离随机范围（px）
SWIPE_RANGE = (600, 900)      # 手指位移随机范围（px）
X_RANGE = (400, 680)          # 手指横向落点随机范围（不固定 540，防直线轨迹）
Y_START_RANGE = (1100, 1300)  # 手指纵向起点随机范围
DUR_JITTER = (0.9, 1.1)       # 时长随机微调系数
SLEEP_S = 0.6                 # 滑动后等待（手指移动 + 惯性停止）

dev = DeviceCtl()
rm = RosterMatcher(GROUP)


def fingerprint(m):
    """消息指纹：身份 + 内容归一化前 40 字；图片用身份 + 类型 + 头像 y。"""
    sender = m.get("matched_user_name") or m.get("nickname") or m.get("side") or "?"
    cn = normalize(m.get("content") or "")
    if cn:
        return f"{sender}|{m.get('content_type')}|{cn[:40]}"
    av = m.get("avatar") or {}
    return f"{sender}|{m.get('content_type')}|img@{av.get('y', 0)}"


def do_swipe(direction):
    """随机化 fling 滚动：目标距离/手指位移/落点/起点/时长全随机。

    每次随机后仍用 fling 物理模型精确反解时长，所以「随机了也知道滚多长」。
    返回预测滚动距离（实际对齐仍靠 find_overlap_dy 动态算）。
    """
    target = random.uniform(*SCROLL_RANGE)
    swipe_px = random.uniform(*SWIPE_RANGE)
    plan = plan_swipe(target, swipe=swipe_px)
    dur_ms = int(plan.duration_ms * random.uniform(*DUR_JITTER))
    x = int(random.uniform(*X_RANGE))
    y_start = int(random.uniform(*Y_START_RANGE))
    swipe_px = int(plan.swipe_px)
    if direction == "earlier":
        dev.swipe(x, y_start, x, y_start + swipe_px, dur_ms)   # 手指下划，看更早
    else:
        dev.swipe(x, y_start, x, y_start - swipe_px, dur_ms)   # 手指上划，看更新
    return plan.predicted_px


def scan_direction(direction, n, complete, prev_img):
    """朝一个方向扫 n 屏。direction: 'earlier'(手指下划看更早) | 'later'(手指上划看更新)"""
    for i in range(n):
        img = dev.capture_bytes()
        if prev_img is not None:
            dy, conf = find_overlap_dy(prev_img, img)
            print(f"  [{direction} {i+1}/{n}] dy={dy:5.0f} 对齐置信={conf:.3f}")
        else:
            print(f"  [{direction} {i+1}/{n}] 首屏")

        ocr = run_ocr(img)
        res = slice_chat(img, ocr, is_group=True, title=GROUP, roster_matcher=rm)
        for m in res["messages"]:
            c = classify_message(m)
            if c["state"] == "complete":
                fp = fingerprint(m)
                if fp not in complete:
                    complete[fp] = {
                        "sender": m.get("matched_user_name") or m.get("nickname") or "我",
                        "content": m.get("content", ""),
                        "content_type": m.get("content_type"),
                        "y_top": c["y_top"], "y_bottom": c["y_bottom"],
                    }

        prev_img = img
        # fling 模式快速滑动（一次滚 SCROLL_TARGET px）
        if i < n - 1:
            predicted = do_swipe(direction)
            import time
            time.sleep(SLEEP_S)
    return prev_img


print(f"=== 真机滚动识别闭环（群：{GROUP}）===")
complete = {}
prev_img = None

print("--- Phase 1: 看更早 ---")
prev_img = scan_direction("earlier", N_PER_DIRECTION, complete, prev_img)
print(f"Phase1 后完整消息 {len(complete)} 条")

print("--- Phase 2: 看更新 ---")
scan_direction("later", N_PER_DIRECTION, complete, prev_img)
print(f"Phase2 后完整消息 {len(complete)} 条")

# 汇总
cnt = Counter()
for v in complete.values():
    cnt[v["content_type"]] += 1
print(f"\n=== 结果：{len(complete)} 条完整消息 ===")
print("类型分布:", dict(cnt))
print("\n完整消息列表（前 30 条）:")
for v in list(complete.values())[:30]:
    print(f"  [{v['content_type']:11s}] {v['sender'][:12]:12s} {v['content'][:30]!r}")
