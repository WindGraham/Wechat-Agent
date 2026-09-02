# -*- coding: utf-8 -*-
"""scripts/redo_seq65_media.py — 重跑 seq65 链接卡 + seq72 图片的媒体处置。

背景：seq65（AI寒武纪公众号链接卡）曾因深色主题被误判 text 入库
（2026-08-27），embedded_thumb 修复后重标为 link，本脚本：
1. 进入「交流一下（2026秋）」；
2. 逐屏向旧滚，模板匹配定位 seq72（猫图，两条中物理位置更旧）；
3. 从该位置起 run_media_pass(max_items=2)，按 seq 降序顺路处置
   seq72（图片保存流）→ seq65（链接取 URL 流）。

用法：PYTHONPATH=.venv_pkgs python3 scripts/redo_seq65_media.py
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
SEQ72_CROP = "crops/交流一下_2026秋_/1a625b8310ac4c7f.jpg"
MAX_SCROLL = 120


def main():
    conn = message_log.connect(DB)
    tools = WeChatTools()
    dev = tools.dev

    print(f"进入会话 {SESSION} ...", flush=True)
    r = tools.enter_session(SESSION)
    if not getattr(r, "success", True):
        print(f"进入失败: {getattr(r, 'error', r)}")
        sys.exit(1)
    time.sleep(1.5)

    template = mp._load_template(SEQ72_CROP)
    assert template is not None, "seq72 模板加载失败"

    print("向旧滚定位 seq72 猫图 ...", flush=True)
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
        print(f"滚了 {MAX_SCROLL} 屏仍未找到 seq72，放弃")
        sys.exit(2)
    print(f"命中 seq72 bbox={found}，启动 media pass", flush=True)

    stats = mp.run_media_pass(dev, conn, SESSION, max_items=2,
                              timeout_s=420, since_ts=0)
    print("media pass:", stats, flush=True)

    for mid in (10825, 10730):
        row = conn.execute(
            "SELECT id, seq, content_type, substr(content,1,80) c,"
            " media_status, media_path FROM messages WHERE id=?",
            (mid,)).fetchone()
        print(tuple(row), flush=True)
    conn.close()


if __name__ == "__main__":
    main()
