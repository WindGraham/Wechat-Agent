# -*- coding: utf-8 -*-
"""handlers/search_done.py — websearch 结果回送事件处理器。"""

from ..events import EV_SEARCH_DONE
from .registry import EventHandler, register_handler


@register_handler
class SearchDoneHandler(EventHandler):
    """搜索结果回送：把结果作为工具反馈，触发该会话再决策一轮。"""

    event_type = EV_SEARCH_DONE

    def handle(self, proxy, ev: dict):
        if proxy._rt("paused", False):
            return
        proxy._handle_search_done(ev["session"], ev.get("query", ""),
                                  ev.get("results"), ev.get("error"))
