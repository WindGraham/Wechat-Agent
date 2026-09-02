# -*- coding: utf-8 -*-
"""scripts/redo_media.py — 手工重跑单条媒体消息的处置（调试用）。

用法：.venv/bin/python scripts/redo_media.py <session> <msg_id>
流程：复位 media_status → 进会话 → 向旧滚模板定位 → MediaHandler 处置
→ _apply_result 写回。需先停 agent（避免抢手机）。
"""

import sys
import time

sys.path.insert(0, ".")

from src.interaction.ports.android.action.wechat_tools import WeChatTools
from src.interaction.ports.android.action.navigator import Navigator
from src.interaction.msglog import message_log
from src.interaction.loop import media_pass as mp
from src.interaction.loop import realtime_scan as RS
from src.interaction.ports.android.perception.media_handler import (
    MediaHandler, MediaTask)

DB = "workspace/chatlogs/chatlog.db"
MAX_SCROLL = 120


def main():
    session, msg_id = sys.argv[1], int(sys.argv[2])
    conn = message_log.connect(DB)
    row = conn.execute(
        "SELECT content_type, crop_path FROM messages WHERE id=?",
        (msg_id,)).fetchone()
    if not row:
        print(f"#{msg_id} 不存在")
        sys.exit(1)
    ctype, crop_path = row["content_type"], row["crop_path"]
    print(f"目标 #{msg_id} ({ctype}) crop={crop_path}", flush=True)
    message_log.update_media(conn, msg_id, media_status="")

    tools = WeChatTools()
    dev = tools.dev
    template = mp._load_template(crop_path)
    if template is None:
        print("模板加载失败")
        sys.exit(1)

    state = tools._snap()
    title = (state.get("page", {}) or {}).get("title") or ""
    if session[:4] not in title:
        Navigator(tools).back_to_home()
        time.sleep(1.0)
        r = tools.enter_session(session)
        if not getattr(r, "success", True):
            print(f"进入失败: {getattr(r, 'error', r)}")
            sys.exit(1)
        time.sleep(1.5)

    print("向旧滚定位目标 ...", flush=True)
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
    if not found:
        print(f"滚了 {MAX_SCROLL} 屏未命中，放弃")
        sys.exit(2)
    print(f"命中 bbox={found}，开始处置", flush=True)

    screen_now = dev.capture_bytes()
    tap_bbox = mp._content_block(screen_now, found) \
        if screen_now is not None else found
    handler = MediaHandler(dev)
    task = MediaTask(msg_id=str(msg_id), msg_type=mp._msg_type_of(ctype),
                     bbox=tap_bbox, screen_path="", group_name=session)
    result = handler.handle(task)
    mp._apply_result(conn, msg_id, result)
    print(f"success={result.success} type={result.msg_type} "
          f"content={str(result.content)[:200]} error={result.error}",
          flush=True)
    row = conn.execute(
        "SELECT content_type, substr(content,1,80), media_status, media_path"
        " FROM messages WHERE id=?", (msg_id,)).fetchone()
    print(tuple(row), flush=True)
    conn.close()


if __name__ == "__main__":
    main()
