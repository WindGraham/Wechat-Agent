# -*- coding: utf-8 -*-
"""screen_watch.py — 持续盯屏：GPU 帧监视，首页红点即时入队。

设计（用户要求 2026-08-08）：
- 不等 sweep 周期：每隔 watch_interval 秒拍一帧
- 只要屏幕在微信首页 Tab，就解析未读标记（数字红圈/红点/@前缀）
- 识别到立即 push 进统一时间序队列（队列自带"去重不挪动"，重复 push 无副作用）
- 屏幕不在首页（旅程处理中/其他页）→ 本帧跳过，天然与动作互斥
"""

import logging
import threading
import time

log = logging.getLogger("interaction.screen_watch")


class ScreenWatcher:
    """后台盯屏线程。

    tools: WeChatTools（dev.capture_bytes）
    queue: UnifiedQueue
    config: runtime 配置（screen_watch_interval，默认 [2, 4] 秒）
    """

    def __init__(self, tools, queue, config=None, clock=time.time):
        self._tools = tools
        self._queue = queue
        self._config = config
        self._clock = clock
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ 控制
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="screen-watch",
                                        daemon=True)
        self._thread.start()
        log.info("screen watcher started (interval=%s)", self._interval())

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def _interval(self):
        if self._config:
            iv = getattr(self._config, "get", lambda k, d=None: d)(
                "screen_watch_interval", (2.0, 4.0))
            return iv
        return (2.0, 4.0)

    @property
    def is_paused(self):
        if self._config:
            return bool(getattr(self._config, "get",
                                lambda k, d=None: d)("paused", False))
        return False

    # ------------------------------------------------------------------ 主循环
    def _run(self):
        import random
        while not self._stop.is_set():
            lo, hi = self._interval()
            self._stop.wait(random.uniform(lo, hi))
            if self._stop.is_set():
                break
            if self.is_paused:
                continue
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("screen watch tick failed")

    def _tick(self):
        """拍一帧：首页 → 解析未读 → 红点入队；非首页 → 跳过。"""
        import cv2
        from ..ports.android.perception.page_detector import detect_page

        img = self._tools.dev.capture_bytes()
        if isinstance(img, (bytes, bytearray)):
            import numpy as np
            img = cv2.imdecode(np.frombuffer(img, np.uint8),
                               cv2.IMREAD_COLOR)
        if img is None:
            return

        page = detect_page(img)      # 纯掩膜快路径（<5ms），不做全图 OCR
        if page.type != "wechat_home":
            return

        # 首页：全量解析拿未读标记
        from ..ports.android.perception.state_builder import build_state
        state = build_state(img)
        if state.get("page", {}).get("type") != "wechat_home":
            return
        pushed = 0
        for e in state.get("elements", []):
            if e.get("type") != "session_item" or e.get("partial"):
                continue
            unread = e.get("unread_count", 0)
            mention = bool(e.get("mention_me"))
            if unread == 0 and not mention:
                continue
            label = e.get("label") or ""
            if not label:
                continue
            if self._queue.push_notify(label, mention=mention,
                                       source="watch"):
                pushed += 1
        if pushed:
            log.info("watch: 首页红点/未读 %d 个会话已入队", pushed)
