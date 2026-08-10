# -*- coding: utf-8 -*-
"""handlers/memory_warm.py — 记忆预热事件处理器。"""

from ..events import EV_MEMORY_WARM
from .registry import EventHandler, register_handler


@register_handler
class MemoryWarmHandler(EventHandler):
    """冷启动记忆预热：从一批历史生成 memory（不回复）。"""

    event_type = EV_MEMORY_WARM

    def handle(self, proxy, ev: dict):
        if proxy._rt("paused", False):
            return
        proxy._warm_memory(ev["session"], ev.get("history_batch", []))
