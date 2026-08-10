# -*- coding: utf-8 -*-
"""handlers/task_done.py — 任务完成事件处理器。"""

from ..events import EV_TASK_DONE
from .registry import EventHandler, register_handler


@register_handler
class TaskDoneHandler(EventHandler):
    """任务完成 → 拼回执 → 再决策（人格化交付结果）。"""

    event_type = EV_TASK_DONE

    def handle(self, proxy, ev: dict):
        if proxy._rt("paused", False):
            return
        proxy._handle_task_done(ev["task_id"])
