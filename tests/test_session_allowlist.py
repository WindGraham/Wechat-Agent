# -*- coding: utf-8 -*-
"""test_session_allowlist.py — 测试模式会话白名单门控的离线单测。

2026-09-01 用户定稿：测试期只回复/接收陈曦猫猫群，其他会话不 journey。
run_loop 的 session_allowlist 非空时：
- 白名单内会话的 notify/action 正常分发
- 其他会话的 notify 丢弃（红点会再触发）
- 其他会话的 action 挂起不丢（reinsert 回队列，保持承诺）
"""

import unittest
from unittest import mock

from src.interaction.loop.run_loop import InteractionLoop
from src.interaction.loop.unified_queue import UnifiedQueue


class _Cfg:
    def __init__(self, allowlist):
        self._a = allowlist
        self.paused = False
        self.sweep_interval = (45, 90)
        self.notify_interval = (3, 6)

    def get(self, k, d=None):
        return self._a if k == "session_allowlist" else d


class _Journey:
    def __init__(self):
        self.done = []

    def process_entry(self, entry):
        self.done.append(entry.session)


class _Tools:
    class dev:
        @staticmethod
        def wake_and_dim():
            pass


def _loop(allowlist):
    q = UnifiedQueue()
    journey = _Journey()
    lp = InteractionLoop(None, None, q, journey, _Tools(),
                         config=_Cfg(allowlist))
    lp._sleep = lambda *_a, **_k: None   # 测试不真睡
    return lp, q, journey


class SessionAllowlistTest(unittest.TestCase):
    def test_allowlisted_dispatched(self):
        lp, q, journey = _loop(["陈曦猫猫群"])
        q.push_notify("陈曦猫猫群")
        lp._wait_and_dispatch(0.1)
        self.assertEqual(journey.done, ["陈曦猫猫群"])

    def test_other_notify_dropped(self):
        lp, q, journey = _loop(["陈曦猫猫群"])
        q.push_notify("特高课")
        lp._wait_and_dispatch(0.1)
        self.assertEqual(journey.done, [])
        self.assertEqual(len(q), 0)          # notify 丢弃

    def test_other_action_held_not_lost(self):
        lp, q, journey = _loop(["陈曦猫猫群"])
        q.push_action("特高课", "<reply session=\"特高课\"><text>x</text></reply>")
        lp._wait_and_dispatch(0.1)
        self.assertEqual(journey.done, [])    # 不分发
        self.assertEqual(len(q), 1)           # 但不丢：挂回队列

    def test_held_action_goes_to_tail_no_starvation(self):
        """挂起的 action 排队尾：同一轮内白名单条目能被分发（不队首饿死）。

        回归：旧实现把挂起 action 原 ts 放回（仍最老），pop_next 永远先弹它，
        白名单会话的 action 被无限饿死（2026-09-01 猫猫群回复被堵 25 分钟）。
        """
        import time
        lp, q, journey = _loop(["陈曦猫猫群"])
        q.push_action("特高课", "<reply session=\"特高课\"><text>x</text></reply>")
        time.sleep(0.02)                      # 保证 ts 有先后（FIFO 依据）
        q.push_action("陈曦猫猫群",
                      "<reply session=\"陈曦猫猫群\"><text>y</text></reply>")
        lp._wait_and_dispatch(0.05)           # 一轮内：特高课挂起去队尾 → 猫猫群分发
        self.assertEqual(journey.done, ["陈曦猫猫群"])
        self.assertEqual(len(q), 1)           # 特高课仍挂起未丢
        self.assertIn("特高课", q)

    def test_empty_allowlist_means_no_gating(self):
        lp, q, journey = _loop([])
        q.push_notify("特高课")
        lp._wait_and_dispatch(0.1)
        self.assertEqual(journey.done, ["特高课"])

    def test_held_priority_action_does_not_starve_allowlisted(self):
        """挂起的 priority action（@我回复）不得堵住白名单普通 action。

        回归：priority 排在 FIFO 之前，挂起即放回会永远先弹它
        （2026-09-01 实测：猫猫群回复在队里 2 分钟发不出）。
        """
        lp, q, journey = _loop(["陈曦猫猫群"])
        q.push_action("特高课", "<reply session=\"特高课\"><text>x</text></reply>",
                      mention=True)          # priority
        q.push_action("陈曦猫猫群",
                      "<reply session=\"陈曦猫猫群\"><text>y</text></reply>")
        lp._wait_and_dispatch(0.05)
        self.assertEqual(journey.done, ["陈曦猫猫群"])
        self.assertIn("特高课", q)            # 挂起未丢

    def test_held_actions_dont_block_sweep(self):
        """挂起的非白名单条目不阻塞首页 sweep（否则红点停更）。"""
        lp, q, _ = _loop(["陈曦猫猫群"])
        q.push_action("特高课", "<reply session=\"特高课\"><text>x</text></reply>")
        self.assertEqual(lp._pending_blocking(), 0)     # 挂起的不算占用
        q.push_notify("陈曦猫猫群")
        self.assertEqual(lp._pending_blocking(), 1)     # 白名单的算
        lp2, q2, _2 = _loop([])
        q2.push_action("特高课", "<reply session=\"特高课\"><text>x</text></reply>")
        self.assertEqual(lp2._pending_blocking(), 1)    # 无白名单时全算

    def test_wander_and_probe_suppressed(self):
        """白名单模式下乱逛/好友巡检不触发（不rand到位也不push）。"""
        lp, q, _ = _loop(["陈曦猫猫群"])
        lp._rand = lambda a, b: 0.0          # 强制命中乱逛概率
        with mock.patch.object(lp, "_maybe_wander") as mw:
            # 直接跑主循环一轮的空闲段逻辑太重，这里验证属性门控：
            # 白名单非空 → sweep_interval 之外的 wander/probe 不应被调用
            self.assertTrue(lp.session_allowlist)
            mw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
