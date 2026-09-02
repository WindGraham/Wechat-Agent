# -*- coding: utf-8 -*-
"""scripts/redo_seq65_direct.py — 直接处置 seq65 链接卡（绕过 pass 排序）。

前一轮 run_media_pass 把 seq65 误标 failed（第一次跑时它因排序
排在 seq72 后、位置已跳过；第二次跑时 media_status=failed 被
_pending_media 排除，pass 反而去处理更旧的 seq54 再次误伤）。
本脚本：复位两条的 media_status → 向旧滚模板定位 seq65 →
直接 MediaHandler.handle → _apply_result 写回。

用法：.venv/bin/python scripts/redo_seq65_direct.py
"""

import sys
import time

sys.path.insert(0, ".")

from src.interaction.ports.android.action.wechat_tools import WeChatTools
from src.interaction.msglog import message_log
from src.interaction.loop import media_pass as mp
from src.interaction.loop import realtime_scan as RS
from src.interaction.ports.android.perception.media_handler import (
    MediaHandler, MediaTask)

DB = "workspace/chatlogs/chatlog.db"
SESSION = "交流一下（2026秋）"
SEQ65_CROP = "crops/交流一下_2026秋_/2a7cb09d39933a3a.jpg"
MAX_SCROLL = 200


def main():
    conn = message_log.connect(DB)
    # 复位被前两轮误伤的条目
    for mid in (10730, 10713):
        message_log.update_media(conn, mid, media_status="")
    print("已复位 10730/10713 media_status=''", flush=True)

    tools = WeChatTools()
    dev = tools.dev
    template = mp._load_template(SEQ65_CROP)
    assert template is not None, "seq65 模板加载失败"

    state = tools._snap()
    title = (state.get("page", {}) or {}).get("title") or ""
    if "交流一下" not in title.replace("—", "一"):
        print(f"当前不在目标会话（{title}），先回首页再进入 ...", flush=True)
        from src.interaction.ports.android.action.navigator import Navigator
        Navigator(tools).back_to_home()
        time.sleep(1.0)
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
    print(f"命中 seq65 bbox={found}，开始处置", flush=True)

    screen_now = dev.capture_bytes()
    tap_bbox = mp._content_block(screen_now, found) \
        if screen_now is not None else found
    handler = MediaHandler(dev)
    task = MediaTask(msg_id="10730", msg_type="card", bbox=tap_bbox,
                     screen_path="", group_name=SESSION)
    result = handler.handle(task)
    mp._apply_result(conn, 10730, result)
    print(f"success={result.success} type={result.msg_type} "
          f"content={str(result.content)[:200]} error={result.error}",
          flush=True)

    row = conn.execute(
        "SELECT id, seq, content_type, content, media_status, media_path"
        " FROM messages WHERE id=10730").fetchone()
    print(tuple(row), flush=True)
    conn.close()


if __name__ == "__main__":
    main()
