# -*- coding: utf-8 -*-
"""scripts/test_collect_from_current.py — 真机验证书签+union 缝合采集。

前置：用户已手动进入目标群（停在最新底部），本脚本不导航、不退出，
直接对当前屏跑 collect_group_history：
  首屏 = 最新屏 N1（结束时存为下次书签）
  次屏起两屏拼 union 识别，union 完整包含上次书签（N0）即停。

用法：
    ~/.venvs/wechat-agent/bin/python scripts/test_collect_from_current.py [群名]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from src.interaction.msglog import message_log
from src.interaction.loop.history_collect import collect_group_history, _load_anchor, _anchor_path
from src.interaction.ports.android.device.device_ctl import DeviceCtl

DB = os.path.join(PROJECT_ROOT, "workspace", "chatlogs", "chatlog.db")


def main():
    ap = argparse.ArgumentParser(description="当前屏跑书签+union 缝合采集（不导航）")
    ap.add_argument("group", nargs="?", default="被打信科2026游泳馆")
    ap.add_argument("--max-rounds", type=int, default=40)
    ap.add_argument("--stop-empty", type=int, default=2)
    args = ap.parse_args()

    anchor = _load_anchor(args.group)
    print(f"群: {args.group}")
    print(f"书签: {_anchor_path(args.group)} 存在={anchor is not None}"
          + (f" 尺寸={anchor.shape}" if anchor is not None else ""))

    conn = message_log.connect(DB)
    dev = DeviceCtl()
    total = collect_group_history(
        dev, conn, args.group,
        max_rounds=args.max_rounds,
        stop_empty_rounds=args.stop_empty,
    )
    print(f"\n完成：本次入库 {total} 条（新的下次书签已存为本次首屏）")
    conn.close()


if __name__ == "__main__":
    main()
