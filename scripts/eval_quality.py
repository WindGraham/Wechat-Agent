# -*- coding: utf-8 -*-
"""质量评估：扫描时逐条记录头像/昵称/正文/引用四维质量，输出统计报告"""
import sys
sys.path.insert(0, ".")
import random, time
from collections import Counter

from src.interaction.ports.android.device.device_ctl import DeviceCtl
from src.interaction.ports.android.perception.ocr_engine import run_ocr
from src.interaction.ports.android.perception.chat_slicer import slice_chat, classify_message
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher
from src.interaction.loop.scroll_stitch import find_overlap_dy
from src.shared.fling_physics import plan_swipe

GROUP = "交流一下？"
N = 8
dev = DeviceCtl()
rm = RosterMatcher(GROUP)

records = {}
prev_img = None

for direction in ("earlier", "later"):
    for i in range(N):
        img = dev.capture_bytes()
        ocr = run_ocr(img)
        res = slice_chat(img, ocr, is_group=True, title=GROUP, roster_matcher=rm)
        for m in res["messages"]:
            c = classify_message(m)
            if c["state"] != "complete":
                continue
            sender = m.get("matched_user_name") or m.get("nickname") or "我"
            av = m.get("avatar") or {}
            key = f"{sender}|{m.get('content_type')}|{m.get('content_norm','')[:30]}"
            records[key] = {
                "sender": sender,
                "nickname": m.get("nickname"),
                "matched": m.get("matched_user_name"),
                "uncertain": m.get("uncertain_entity"),
                "content_type": m.get("content_type"),
                "content": m.get("content", ""),
                "avatar_h": av.get("h"),
                "avatar_w": av.get("w"),
                "side": m.get("side"),
            }
        prev_img = img
        if i < N - 1:
            t = random.uniform(1000, 1400)
            s = random.uniform(600, 900)
            plan = plan_swipe(t, swipe=s)
            x = int(random.uniform(400, 680)); y = int(random.uniform(1100, 1300))
            if direction == "earlier":
                dev.swipe(x, y, x, y + int(plan.swipe_px), int(plan.duration_ms))
            else:
                dev.swipe(x, y, x, y - int(plan.swipe_px), int(plan.duration_ms))
            time.sleep(0.6)

# ===== 质量统计 =====
msgs = list(records.values())
print(f"\n===== 质量报告（{len(msgs)} 条完整消息）=====\n")

# 1. 内容类型分布
ct = Counter(m["content_type"] for m in msgs)
print(f"1. 内容类型: {dict(ct)}")

# 2. 双因子匹配（头像+昵称联合）
with_avatar = [m for m in msgs if m["avatar_h"]]
matched = [m for m in with_avatar if m["matched"]]
print(f"2. 双因子匹配: 有头像 {len(with_avatar)} 条，匹配成功 {len(matched)} 条 ({len(matched)/max(1,len(with_avatar))*100:.0f}%)")

# 3. 头像完整度（h>=100 为完整）
av_ok = [m for m in with_avatar if m["avatar_h"] >= 100]
print(f"3. 头像完整度: h>=100 的 {len(av_ok)}/{len(with_avatar)} 条；最小 h={min((m['avatar_h'] for m in with_avatar), default=0)}")

# 4. 昵称质量：nickname 不含引用文本（"："）才算干净
nick_dirty = [m for m in msgs if m["nickname"] and "：" in str(m["nickname"])]
print(f"4. 昵称被引用污染: {len(nick_dirty)} 条 {[(m['nickname'][:16]) for m in nick_dirty[:4]]}")

# 5. 引用分离：quote 消息 content 是否"正文+引用"正确分离
quotes = [m for m in msgs if m["content_type"] == "quote"]
print(f"5. 引用消息: {len(quotes)} 条")
for q in quotes[:5]:
    print(f"    content={q['content'][:40]!r}")

# 6. 身份为"我"的（side=self）
self_msgs = [m for m in msgs if m["side"] == "self"]
print(f"6. 自己的消息: {len(self_msgs)} 条")
