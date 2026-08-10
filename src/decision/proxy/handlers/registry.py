# -*- coding: utf-8 -*-
"""decision/proxy/handlers/registry.py — 事件处理器注册表。"""


class EventHandler:
    """事件处理器接口。

    子类需定义 event_type（处理的事件类型）并实现 handle(proxy, ev)。
    proxy 作为上下文注入：处理器通过 proxy 访问状态/方法。
    """

    event_type = None

    def handle(self, proxy, ev: dict):
        raise NotImplementedError


# 注册表：事件类型 -> 处理器类
_HANDLERS = {}


def register_handler(handler_cls):
    """注册事件处理器（模块导入时自动注册，实现热插拔）。"""
    if not handler_cls.event_type:
        raise ValueError(f"处理器 {handler_cls.__name__} 缺 event_type")
    _HANDLERS[handler_cls.event_type] = handler_cls
    return handler_cls


def get_handler(event_type: str):
    """按事件类型取处理器类；未注册返回 None。"""
    return _HANDLERS.get(event_type)
