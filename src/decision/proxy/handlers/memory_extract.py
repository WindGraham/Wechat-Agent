# -*- coding: utf-8 -*-
"""handlers/memory_extract.py — 后置记忆提取事件处理器。

每次 agent 回复后（不是 silent），把本轮对话异步喂给便宜模型，
让它从对话中提取记忆（merge 已有 or add 新）。不占用主决策轮次注意力。

实现：daemon 线程，不阻塞 Proxy 事件循环。
"""

import threading

from ..events import EV_MEMORY_EXTRACT
from .registry import EventHandler, register_handler


@register_handler
class MemoryExtractHandler(EventHandler):
    """后置记忆提取：对话 → memory（不回复用户）。"""

    event_type = EV_MEMORY_EXTRACT

    def handle(self, proxy, ev: dict):
        if proxy._rt("paused", False):
            return
        threading.Thread(
            target=proxy._memory_svc.extract,
            args=(ev["session"],
                  ev.get("conversation_text", ""),
                  ev.get("user_names", [])),
            daemon=True,
            name=f"mem-extract-{ev['session']}",
        ).start()
