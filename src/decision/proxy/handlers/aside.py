# -*- coding: utf-8 -*-
"""handlers/aside.py — 旁注事件处理器（网关直接对 proxy 输入）。

旁注 = 网关界面上注入一条消息，等价于在目标会话里以指定发送者
身份说了一句话，触发一次带该消息的决策。
"""

from ....shared.types import Message
from ..events import EV_ASIDE
from .registry import EventHandler, register_handler


@register_handler
class AsideHandler(EventHandler):
    """旁注 → 构造 Message 附加到新消息 → 触发决策。"""

    event_type = EV_ASIDE

    def handle(self, proxy, ev: dict):
        if proxy._rt("paused", False):
            return
        session = ev.get("session", "")
        text = ev.get("text", "")
        if not session or not text.strip():
            return
        sender = ev.get("sender") or proxy._rt("owner", "")
        try:
            is_group = bool(proxy._reader.last_is_group(session))
        except Exception:  # noqa: BLE001
            is_group = True
        aside = Message(
            session=session, is_group=is_group,
            sender=sender, is_mine=False,
            content=text.strip(), content_type="text",
            mentions=[],
            ts=proxy._clock(), seq=10 ** 9,   # 大 seq：排在真实新消息之后
            msg_uid="aside",
        )
        proxy._decide_session(
            session, mention_hint=True, force=True, extra_msgs=[aside])
