# -*- coding: utf-8 -*-
"""scripts/redo_seq65_link.py — 单独重跑 seq65 链接卡取 URL（第二轮）。

第一轮（redo_seq65_media.py）已处置 seq72（表情包，done）；
seq65 因 seq72 处置后屏幕位置跳到目标新侧、_locate 只向新滚而
定位失败。本脚本先逐屏向旧滚、用存档裁图模板把 seq65 卡片找到，
再 run_media_pass(max_items=1)（此时 pending 里 seq 最大的就是 65）。

用法：PYTHONPATH=.venv_pkgs python3 scripts/redo_seq65_link.py
"""

import sys
import time

sys.path.insert(0, ".")

from src.interaction.ports.android.action.wechat_tools import WeChatTools
from src.interaction.msglog import message_log
from src.interaction.loop import media_pass as mp
from src.interaction.loop import realtime_scan as RS

DB = "workspace/chatlogs/chatlog.db"
SESSION = "交流一下（2026秋）"
SEQ65_CROP = "crops/交流一下_2026秋_/2a7cb09d39933a3a.jpg"
MAX_SCROLL = 200


def main():
    conn = message_log.connect(DB)
    tools = WeChatTools()
    dev = tools.dev

    template = mp._load_template(SEQ65_CROP)
    assert template is not None, "seq65 模板加载失败"

    # 确认当前在目标会话页（不在则进入）
    state = tools._snap()
    title = (state.get("page", {}) or {}).get("title") or ""
    if "交流一下" not in title:
        print(f"当前不在目标会话（{title}），进入 {SESSION} ...", flush=True)
        r = tools.enter_session(SESSION)
        if not getattr(r, "success", True):
            print(f"进入失败: {getattr(r, 'error', r)}")
            sys.exit(1)
        time.sleep(1.5)

    print("向旧滚定位 seq65 链接卡 ...", flush=True)
    found = None
    for i in range(MAX_SCROLL):
        for wait in (0.0, 0.5):
            if wait:
                time.sleep(wait)
            screen = dev.capture_bytes()
            bbox = mp._find_on_screen(screen, template)
            if bbox is not None:
                found = bbox
                break
        if found:
            break
        RS.do_swipe(dev, "earlier")
        time.sleep(0.8)
        if i % 10 == 9:
            print(f"  已滚 {i + 1} 屏未命中 ...", flush=True)

    if not found:
        print(f"滚了 {MAX_SCROLL} 屏仍未找到 seq65，放弃")
        sys.exit(2)
    print(f"命中 seq65 bbox={found}，启动 media pass", flush=True)

    stats = mp.run_media_pass(dev, conn, SESSION, max_items=1,
                              timeout_s=300, since_ts=0)
    print("media pass:", stats, flush=True)

    row = conn.execute(
        "SELECT id, seq, content_type, content, media_status, media_path"
        " FROM messages WHERE id=10730").fetchone()
    print(tuple(row), flush=True)
    conn.close()


if __name__ == "__main__":
    main()
