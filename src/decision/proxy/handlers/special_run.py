# -*- coding: utf-8 -*-
"""handlers/special_run.py — 特殊 Prompt 触发事件处理器。

scheduler 投硬币命中后，事件循环分发给本 handler。
handler 起 daemon 线程执行（不阻塞事件循环），
线程内加载特殊 prompt 文件，收集上下文，调 LLM，
根据 output_mode 解析输出并路由执行。
"""

import threading

from ..events import EV_SPECIAL_RUN
from .registry import EventHandler, register_handler


@register_handler
class SpecialRunHandler(EventHandler):
    """特殊 prompt 执行：加载 → 收集上下文 → LLM → 路由执行。"""

    event_type = EV_SPECIAL_RUN

    def handle(self, proxy, ev: dict):
        if proxy._rt("paused", False):
            return
        threading.Thread(
            target=proxy._special.run,
            args=(ev["prompt_name"], ev.get("session", "")),
            daemon=True,
            name=f"special-{ev['prompt_name']}",
        ).start()
