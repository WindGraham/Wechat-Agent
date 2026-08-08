# -*- coding: utf-8 -*-
"""frame_bus.py — 统一截图入口，单帧缓存（raw bytes + parsed state）。

所有感知消费者读同一帧，避免重复截图。
capture() 截图一次 → 同时缓存 raw bytes 和 parsed state。
"""

import threading
import logging

log = logging.getLogger("perception.frame_bus")


class FrameBus:
    """单帧缓存：capture() 截图并缓存 raw bytes + state，latest() 读缓存。"""

    def __init__(self, tools):
        self._tools = tools
        self._lock = threading.Lock()
        self._state = None
        self._raw: bytes = None

    def capture(self):
        """截图 + 解析，缓存 raw 帧 + state dict。返回 state。

        单截图路径：capture_bytes 内存截图 → WeChatTools.parse_state 复用同一帧
        （V2 感知层内存解析，不再二次截图/不落盘）。低质量帧走 tools._snap()
        重截重试。"""
        with self._lock:
            self._raw = self._tools.dev.capture_bytes()
            state = self._tools.parse_state(self._raw)
            ptype = state.get("page", {}).get("type", "")
            conf = state.get("meta", {}).get("confidence", 0.0)
            if ptype == "wechat_unknown" or conf < 0.5:
                state = self._tools._snap()      # 低质量：重截 + 重试逻辑
            self._state = state
            return self._state

    def latest(self):
        """返回缓存的最新 state，不重新截图。"""
        with self._lock:
            return self._state

    def latest_raw(self) -> bytes:
        """返回缓存的最新 raw PNG bytes，不重新截图。"""
        with self._lock:
            return self._raw

    def capture_raw(self) -> bytes:
        """截图并返回原始 PNG 字节（同时缓存 state）。"""
        self.capture()
        return self._raw

    def invalidate(self):
        """清空缓存。"""
        with self._lock:
            self._state = None
            self._raw = None
