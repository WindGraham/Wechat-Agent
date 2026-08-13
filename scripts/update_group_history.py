# -*- coding: utf-8 -*-
"""scripts/update_group_history.py — 进入指定群，滚动采集补历史，直到「上一次的位置」。

与 realtime_scan 的区别：这里用「全库指纹去重」而不是只认日志尾部窗口——
往上翻到库里已有的历史消息时，能被正确判为"已存在"而跳过，从而安全地
停在「上一次采集的位置」，不会把旧消息误当新消息重复入库。

用法：
    ~/.venvs/wechat-agent/bin/python scripts/update_group_history.py 陈曦猫猫群
"""

import sys
import time

sys.path.insert(0, ".")

from src.interaction.ports.android.action.wechat_tools import WeChatTools
from src.interaction.msglog import message_log
from src.interaction.msglog.message_log import normalize
from src.interaction.ports.android.perception.ocr_engine import run_ocr
from src.interaction.ports.android.perception.chat_slicer import slice_chat, classify_message
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher
from src.interaction.loop import realtime_scan as RS

GROUP = sys.argv[1] if len(sys.argv) > 1 else "陈曦猫猫群"
DB = "workspace/chatlogs/chatlog.db"
STOP_EMPTY_ROUNDS = 2     # 连续 2 屏无新消息 → 到上一次位置，停
MAX_ROUNDS = 12           # 上限保险丝


def _count(conn, sid):
    return conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?",
                        (sid,)).fetchone()[0]


def main():
    conn = message_log.connect(DB)
    sid_row = conn.execute("SELECT session_id FROM sessions WHERE name=?",
                           (GROUP,)).fetchone()
    sid = sid_row["session_id"] if sid_row else None
    before = _count(conn, sid) if sid else 0
    print(f"[{GROUP}] 开始前 {before} 条")

    # 全库指纹集合（跨屏去重 + 判断"已翻到上一次位置"）：
    # 键与 to_entry/_entry_key 一致：sender|content_type|normalize(content)[:30]
    existing = set()
    if sid:
        for r in conn.execute(
                "SELECT sender, is_mine, content_type, content_norm "
                "FROM messages WHERE session_id=?", (sid,)):
            s = r["sender"]
            if r["content_type"] == "time_divider":
                s = ""  # to_entry 对 divider 的 sender 为空串
            existing.add(f"{s}|{r['content_type']}|{r['content_norm'][:30]}")

    tools = WeChatTools()
    print(f"进入会话 {GROUP} ...")
    r = tools.enter_session(GROUP)
    if not getattr(r, "success", True):
        print(f"进入失败: {getattr(r, 'error', r)}")
        sys.exit(1)
    time.sleep(1.0)

    dev = tools.dev
    rm = RosterMatcher(GROUP)
    print("滑回最新位置...")
    RS.scroll_to_latest(dev)
    time.sleep(1.0)

    seen = set()
    total = 0
    empty_streak = 0
    cur_divider = None

    for rnd in range(MAX_ROUNDS):
        img = dev.capture_bytes()
        ocr = run_ocr(img)
        res = slice_chat(img, ocr, is_group=True, title=GROUP, roster_matcher=rm)

        new_entries = []
        clipped = 0
        for m in res["messages"]:
            c = classify_message(m)
            ctype = m.get("content_type")
            if ctype == "time_divider":
                cur_divider = m.get("content") or cur_divider
                e = RS.to_entry(m, GROUP)
                k = RS._entry_key(e)
                if k not in existing and k not in seen:
                    seen.add(k)
                    new_entries.append(e)
                continue
            if c["state"] == "complete":
                e = RS.to_entry(m, GROUP)
                e.time_hint = cur_divider
                k = RS._entry_key(e)
                if k not in existing and k not in seen:
                    seen.add(k)
                    e.crop_path = RS.save_crop(img, GROUP, c["y_top"], c["y_bottom"], k)
                    new_entries.append(e)
            elif c["state"] == "bottom_clipped":
                e = RS.to_entry(m, GROUP)
                e.time_hint = cur_divider
                if e.sender and e.content.strip() \
                        and e.content_type in ("text", "quote"):
                    k = RS._entry_key(e)
                    if k not in existing and k not in seen:
                        seen.add(k)
                        e.crop_path = RS.save_crop(img, GROUP, c["y_top"], c["y_bottom"], k)
                        new_entries.append(e)
                clipped += 1
            else:
                clipped += 1

        n = 0
        if new_entries:
            r = message_log.append_incremental(
                conn, sid, new_entries, source="incremental", gap_ok=True)
            n = r.get("inserted", 0)
            total += n
            for e in new_entries:
                existing.add(RS._entry_key(e))   # 本屏新消息也入指纹集
            print(f"  [rnd{rnd+1}] 新识别 {len(new_entries)} 条，入库 {n} 条"
                  f"（残缺 {clipped} 条），累计 {total} 条")
        else:
            print(f"  [rnd{rnd+1}] 无新消息（残缺 {clipped} 条）")

        empty_streak = empty_streak + 1 if n == 0 else 0
        if empty_streak >= STOP_EMPTY_ROUNDS:
            print(f"  连续 {empty_streak} 屏无新消息，已到上一次位置，停止")
            break
        if rnd < MAX_ROUNDS - 1:
            RS.do_swipe(dev, "earlier")
            time.sleep(RS.SLEEP_S)

    after = _count(conn, sid) if sid else 0
    print(f"\n[{GROUP}] 完成：入库 {total} 条，{before} → {after}")
    conn.close()


if __name__ == "__main__":
    main()
