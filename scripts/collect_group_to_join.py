# -*- coding: utf-8 -*-
"""scripts/collect_group_to_join.py — 指定群固定采集：进群后差分拼接深采，
一直往回采到「滑到没变化」（滚到可见历史顶部）为止。

用法：
    ~/.venvs/wechat-agent/bin/python scripts/collect_group_to_join.py 被打信科2026游泳馆

停止条件：
    1. 滑到没变化：相邻两屏重叠位移 dy 过小（<40px）→ 已到顶，停；
    2. 重叠置信过低（conf<0.5）或达到 --max-rounds 上限。

注意：本脚本独占设备（adb）。运行前必须先停掉正在占用手机的进程——
    pkill -f "src.main"   （以及任何其它 adb 驱动脚本），否则两边抢手机。

参数：
    group            群名（必填，默认「被打信科2026游泳馆」）
    --max-rounds     最大滚动屏数（默认 300）
    --stop-empty     连续 N 屏无新入库才停；默认 0（重采已部分入库的群时
                     关闭，否则会被「已采过的最近消息」卡在开头）
"""

import argparse
import os
import sys
import time

sys.path.insert(0, ".")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from src.interaction.ports.android.action.wechat_tools import WeChatTools
from src.interaction.msglog import message_log
from src.interaction.loop.history_collect import collect_group_history

DB = os.path.join(PROJECT_ROOT, "workspace", "chatlogs", "chatlog.db")


def main():
    ap = argparse.ArgumentParser(description="指定群固定采集，采到滑不动为止")
    ap.add_argument("group", nargs="?", default="被打信科2026游泳馆")
    ap.add_argument("--max-rounds", type=int, default=300)
    ap.add_argument("--stop-empty", type=int, default=0,
                    help="连续 N 屏无新入库才停（0=关闭，重采已入库群建议关）")
    args = ap.parse_args()

    print(f"群: {args.group} | max_rounds={args.max_rounds} "
          f"| stop_empty={args.stop_empty}")
    print("提示：请确认没有其它进程在占用手机（如 --collect agent）。")

    conn = message_log.connect(DB)
    tools = WeChatTools()
    print(f"进入会话 {args.group} ...")
    r = tools.enter_session(args.group)
    if not getattr(r, "success", True):
        print(f"进入失败: {getattr(r, 'error', r)}")
        sys.exit(1)
    time.sleep(1.0)

    total = collect_group_history(
        tools.dev, conn, args.group,
        max_rounds=args.max_rounds,
        stop_empty_rounds=args.stop_empty,
        stop_at_anchor=False,   # 本脚本要整段历史深采到顶，不按上次书签停
    )
    print(f"\n完成：本次入库 {total} 条（群 {args.group}）")
    conn.close()


if __name__ == "__main__":
    main()
