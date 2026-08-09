# -*- coding: utf-8 -*-
"""run_loop.py — 交互层主事件循环（红点驱动）。

主循环（每周期）：
  回首页 → 解析未读标记（数字红圈/免打扰红点/红色@前缀）
  → 有标记的会话：逐个进入 → 旅程处理 → LogUpdated
  → 无标记：本轮结束（不进任何会话）
  → 空闲时：通知栏监听（近实时辅助）+ 乱逛/退避

暂停模式：只捕获不操作（消息入队但不进入会话）。
"""

import logging
import random
import time
from typing import Optional

from .unified_queue import UnifiedQueue
from .journey import JourneyManager
from ..ports.android.device import layout

log = logging.getLogger("interaction.loop")


class InteractionLoop:
    """交互层主事件循环。

    依赖注入（全部在入口装配处连接）：
    - scanner: 首页扫描器（ports/android/perception/scanner.py 的 Scanner）
    - watcher: 通知监听器（NotifyWatcher 线程）
    - queue: 统一时间序队列
    - journey: 旅程管理器
    - tools: 端口工具（WeChatTools，用于 wake_and_dim 等）
    - config: 运行时配置（sweep_interval 等）
    """

    def __init__(self, scanner, watcher, queue: UnifiedQueue,
                 journey: JourneyManager, tools, config=None):
        self._scanner = scanner
        self._watcher = watcher
        self._queue = queue
        self._journey = journey
        self._tools = tools
        self._config = config

        self._stop = False
        self._sleep = time.sleep
        self._rand = random.uniform
        self._clock = time.time

    # ------------------------------------------------------------------ 配置
    @property
    def sweep_interval(self):
        if self._config:
            return getattr(self._config, 'sweep_interval', (45, 90))
        return (45, 90)

    @property
    def notify_interval(self):
        if self._config:
            return getattr(self._config, 'notify_interval', (3, 6))
        return (3, 6)

    @property
    def is_paused(self):
        if self._config:
            return getattr(self._config, 'paused', False)
        return False

    # ------------------------------------------------------------------ 主循环
    def run(self, once: bool = False):
        """启动主循环。once=True 时只跑一轮。"""
        log.info("interaction loop start: once=%s", once)
        try:
            self._tools.dev.wake_and_dim()
        except Exception:
            log.exception("wake_and_dim failed")

        self._watcher.start()
        # 持续盯屏：首页红点即时入队（不等 sweep 周期）
        from .screen_watch import ScreenWatcher
        self._screen_watch = ScreenWatcher(self._tools, self._queue,
                                           config=self._config)
        self._screen_watch.start()

        try:
            if once:
                self._run_once()
            else:
                self._main_loop()
        finally:
            try:
                self._screen_watch.stop()
            except Exception:
                log.exception("screen watch stop failed")
            try:
                self._watcher.stop()
            except Exception:
                log.exception("watcher stop failed")
            try:
                self._tools.dev.restore_screen()
            except Exception:
                log.exception("restore_screen failed")

    def _main_loop(self):
        """主循环：sweep → drain queue → sleep。"""
        next_sweep = self._clock() + self._rand(*self.sweep_interval)

        while not self._stop:
            # dirty 会话优先补同步（上一轮同步失败的会话，push 到队首）
            self._resync_dirty()

            now = self._clock()
            timeout = max(0.0, next_sweep - now)

            # 等队列事件或超时
            self._wait_and_dispatch(timeout)

            now = self._clock()
            if now >= next_sweep:
                if len(self._queue) > 0:
                    # 队列未清空不开轮询（用户规则 2026-08-08）：
                    # 旅程优先，sweep 顺延到下一周期
                    log.debug("队列未清空（%d），sweep 顺延", len(self._queue))
                else:
                    try:
                        self._do_sweep()
                    except Exception:
                        log.exception("sweep failed")
                next_sweep = self._clock() + self._rand(*self.sweep_interval)

            # 乱逛：队列空时模拟真人随机浏览
            if not self.is_paused and len(self._queue) == 0:
                self._maybe_wander()

    def _run_once(self):
        """单轮执行。"""
        self._resync_dirty()
        try:
            self._do_sweep()
        except Exception:
            log.exception("sweep failed")
        self._drain_queue()

    # ------------------------------------------------------------------ dirty 补同步
    def _resync_dirty(self):
        """把上一轮同步失败的 dirty 会话重新入队（mention 插到队首，优先补同步）。"""
        for session in self._journey.take_dirty_sessions():
            log.info("dirty resync: %s → queue front", session)
            self._queue.push_notify(session=session, mention=True,
                                    source="dirty_resync")

    # ------------------------------------------------------------------ Sweep
    def _do_sweep(self):
        """首页扫描：双击微信 Tab → 解析未读 → 入队。"""
        if self.is_paused:
            log.debug("paused, skip sweep")
            return

        events = self._scanner.sweep()
        for ev in events:
            self._queue.push_notify(
                session=ev.session,
                mention=ev.mention,
                source=ev.source,
            )

    # ------------------------------------------------------------------ 分发
    def _wait_and_dispatch(self, timeout: float):
        """等队列事件（1s 轮询粒度），有事件时立即分发。

        暂停模式：只捕获不操作——notify 条目可丢（红点会再触发），
        action 条目永不许丢，必须原样重新入队（保持原位置）。
        """
        deadline = self._clock() + timeout
        while not self._stop:
            entry = self._queue.pop_next()
            if entry is not None:
                if self.is_paused:
                    if entry.kind == "action":
                        # 行动承载着对用户的承诺：暂停时原样放回队列
                        log.info("paused: requeue action %s (keep position)",
                                 entry.session)
                        self._queue.reinsert(entry)
                    else:
                        log.debug("paused: drop notify %s (红点会再触发)",
                                  entry.session)
                    # 放回/丢弃后睡到本轮超时，避免暂停时空转
                    self._sleep(min(1.0, max(0.0, deadline - self._clock())))
                else:
                    self._dispatch(entry)
                    return
            if self._clock() >= deadline:
                return
            self._sleep(min(1.0, max(0.0, deadline - self._clock())))

    def _drain_queue(self):
        """清空队列（once 模式）。暂停时行动条目保留在队列中。"""
        while not self._stop:
            entry = self._queue.pop_next()
            if entry is None:
                return
            if self.is_paused:
                if entry.kind == "action":
                    # 行动永不许丢：放回原位，暂停期间不再继续排空
                    self._queue.reinsert(entry)
                    return
                continue  # notify 可丢（红点会再触发）
            self._dispatch(entry)

    def _dispatch(self, entry):
        """分发一个队列条目到旅程管理器。"""
        try:
            self._journey.process_entry(entry)
        except Exception:
            log.exception("[%s] journey failed", entry.session)

    # ------------------------------------------------------------------ 乱逛
    def _maybe_wander(self):
        """队列空时以概率随机浏览微信，模拟真人行为。"""
        if self._rand(0, 1) > 0.33:
            return
        duration = self._rand(30, 60)
        log.info("wander: starting (%.0fs)", duration)
        deadline = self._clock() + duration

        while self._clock() < deadline and not self._stop:
            if len(self._queue) > 0:
                log.info("wander: queue has events, stopping")
                break
            try:
                self._do_wander_action()
            except Exception:
                log.exception("wander action failed")
            self._sleep(self._rand(3, 8))

        try:
            self._tools.back_to_home()
        except Exception:
            pass
        log.info("wander: ended")

    def _do_wander_action(self):
        """执行一次随机乱逛动作。"""
        actions = [
            self._wander_scroll_home,
            self._wander_peek_chat,
            self._wander_switch_tab,
        ]
        action = actions[int(self._rand(0, len(actions) - 0.01))]
        action()

    def _wander_scroll_home(self):
        try:
            self._tools.dev.swipe_zone(
                layout.HOME_LIST_ZONE,
                direction="up" if self._rand(0, 1) > 0.5 else "down",
                length_ratio=(0.2, 0.5),
            )
        except Exception:
            pass

    def _wander_peek_chat(self):
        try:
            state = self._scanner.frame_bus.latest()
            if not state:
                return
            sessions = [
                e for e in state.get("elements", [])
                if e.get("type") == "session_item"
            ]
            if not sessions:
                return
            target = sessions[int(self._rand(0, len(sessions) - 0.01))]
            label = target.get("label", "")
            if not label:
                return
            self._journey.navigator.enter_session(label)
            self._sleep(self._rand(2, 4))
            self._tools.dev.swipe_zone(
                layout.CHAT_SCROLL_ZONE, direction="up",
                length_ratio=(0.3, 0.6),
            )
            self._sleep(self._rand(1, 2))
            self._journey.navigator.back()
        except Exception:
            pass

    def _wander_switch_tab(self):
        try:
            # 只乱逛"发现"：点通讯录 tab 会把好友申请的红点信号消掉
            # （tab 红点开一次就没，但申请还在——2026-08-09 用户规则）
            self._tools.dev.tap_rect(layout.TAB_DISCOVER)
            self._sleep(self._rand(2, 4))
            self._tools.dev.tap_rect(layout.TAB_WECHAT)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def stop(self):
        self._stop = True
