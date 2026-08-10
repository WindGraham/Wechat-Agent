# -*- coding: utf-8 -*-
"""handlers/log_updated.py — LogUpdated 事件处理器（决策入口）。"""

from ..events import EV_LOG_UPDATED
from .registry import EventHandler, register_handler


@register_handler
class LogUpdatedHandler(EventHandler):
    """交互层日志更新通知 → 触发该会话决策。"""

    event_type = EV_LOG_UPDATED

    def handle(self, proxy, ev: dict):
        if proxy._rt("paused", False):
            return
        proxy._decide_session(ev["session"], mention_hint=ev.get("mention"))
