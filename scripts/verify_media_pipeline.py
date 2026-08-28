# -*- coding: utf-8 -*-
"""scripts/verify_media_pipeline.py — 多媒体轮询接入的真机验证（2026-08-27）。

流程：进陈曦猫猫群 → collect_group_history 小轮数采集（打标）
→ 查库看媒体条目（content_type/media_status/frame_phash/crop_path）
→ run_media_pass 小预算处置 → 查库看写回 → 列落盘产物。

运行前确认无其它进程占用手机。
"""

import os
import sys
import time

sys.path.insert(0, ".")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(PROJECT_ROOT, "workspace", "chatlogs", "chatlog.db")
GROUP = "陈曦猫猫群"

from src.interaction.ports.android.action.wechat_tools import WeChatTools
from src.interaction.msglog import message_log
from src.interaction.loop.history_collect import collect_group_history
from src.interaction.loop.media_pass import run_media_pass


def show_media_rows(conn, tag):
    sid = message_log.get_or_create_session(conn, GROUP, True)
    print(f"\n--- {tag}: 最近媒体条目 ---")
    rows = conn.execute(
        "SELECT id, seq, content_type, substr(content,1,60) c, media_status,"
        " frame_phash, substr(crop_path,1,50) cp, substr(media_path,1,60) mp"
        " FROM messages WHERE session_id=? AND content_type NOT IN"
        " ('text','quote','time_divider','system') ORDER BY seq DESC LIMIT 12",
        (sid,)).fetchall()
    for r in rows:
        print(dict(zip(["id", "seq", "ctype", "content", "status",
                        "phash", "crop", "media_path"], r)))
    return rows


def main():
    conn = message_log.connect(DB)
    tools = WeChatTools()
    print(f"进入会话 {GROUP} ...")
    r = tools.enter_session(GROUP)
    if not getattr(r, "success", True):
        print(f"进入失败: {getattr(r, 'error', r)}")
        sys.exit(1)
    time.sleep(1.0)

    debug_dir = os.path.join(
        PROJECT_ROOT, "workspace", "collect_debug",
        "media_e2e_" + time.strftime("%H%M%S"))
    print("\n=== 阶段1：采集打标（max_rounds=4）===")
    t0 = time.time()
    total = collect_group_history(
        tools.dev, conn, GROUP, max_rounds=4, stop_empty_rounds=2,
        stop_at_anchor=True, use_cutlines=True, debug_dir=debug_dir)
    print(f"采集入库 {total} 条；debug 落盘 {debug_dir}")
    rows1 = show_media_rows(conn, "采集后")

    print("\n=== 阶段2：媒体处置 pass（max_items=4）===")
    stats = run_media_pass(tools.dev, conn, GROUP, max_items=4,
                           timeout_s=300, since_ts=t0)
    print(f"media pass: {stats}")
    show_media_rows(conn, "处置后")

    print("\n=== 落盘产物 ===")
    os.system(f"ls -lt {os.path.join(PROJECT_ROOT, 'workspace', 'media')}/*/ | head -30")

    print("\n=== 回首页 ===")
    tools.back()
    time.sleep(0.8)
    tools.back()


if __name__ == "__main__":
    main()
