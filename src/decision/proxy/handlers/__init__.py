# -*- coding: utf-8 -*-
"""decision/proxy/handlers — 事件处理器（热插拔）。

每个事件类型一个处理器文件，实现 EventHandler 接口：
    class XxxHandler(EventHandler):
        event_type = EV_XXX          # 处理的事件类型
        def handle(self, proxy, ev):  # proxy 注入上下文, 处理该事件

注册：模块导入即自动注册（@register_handler），
proxy 的 _handle 只需查 _HANDLERS 分发。

热插拔：新增事件类型 = 新建 handlers/<name>.py + 导入注册，
不改 Proxy 主类。
"""

from .registry import EventHandler, get_handler, register_handler, _HANDLERS

# 导入各处理器模块（触发注册）
from . import (log_updated, task_done, memory_warm, memory_extract,
               special_run, search_done, aside)  # noqa: F401

__all__ = ["EventHandler", "get_handler", "register_handler", "_HANDLERS"]
