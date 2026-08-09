# -*- coding: utf-8 -*-
"""test_journey.py — 旅程管理器离线单测（假 reader/navigator/sender，不触真机）。

覆盖关键规则（docs/INTERACTION_LAYER.md §3/§5）：
- 铁律：进会话必须完成日志更新回传；同步失败重试 2 次（随机间隔），
  仍失败 → 允许退出但标 dirty，下轮优先补同步
- LogUpdated 只在行动清空后发送
- 行动失败重试经 requeue_entry（attempts 递增，B2 修复）
- follow-up 行动吸收（不设驻留上限）
- 暂停不丢行动（run_loop._drain_queue / _wait_and_dispatch）
- dirty 会话下轮 push 到队首（run_loop._resync_dirty）
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.interaction.loop.unified_queue import UnifiedQueue
from src.interaction.loop.journey import JourneyManager, SYNC_MAX_RETRIES
from src.interaction.loop.run_loop import InteractionLoop


# ------------------------------------------------------------------ fakes
class FakeNav:
    def __init__(self, enter_ok=True):
        self.enter_ok = enter_ok
        self.entered = []
        self.back_home_count = 0

    def enter_session(self, session):
        self.entered.append(session)
        return SimpleNamespace(success=self.enter_ok,
                               error=None if self.enter_ok else "enter fail")

    def back_to_home(self):
        self.back_home_count += 1


class FakeReader:
    """sync_session 返回对象表示成功，None 表示失败。

    fail_times: 前 N 次调用失败，之后成功。
    """
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0

    def sync_session(self, session, is_group):
        self.calls += 1
        if self.calls <= self.fail_times:
            return None
        return SimpleNamespace(session=session, version=self.calls)


class FakeSender:
    def __init__(self, ok=True, retryable=True):
        self.ok = ok
        self.retryable = retryable
        self.submitted = []

    def submit_bundle(self, session, payload):
        self.submitted.append((session, payload))
        return SimpleNamespace(ok=self.ok,
                               error=None if self.ok else "send fail",
                               retryable=self.retryable)


def make_journey(reader=None, sender=None, nav=None, queue=None):
    if queue is None:
        queue = UnifiedQueue()
    reader = reader or FakeReader()
    sender = sender or FakeSender()
    nav = nav or FakeNav()
    sent_updates = []
    jm = JourneyManager(queue, reader, sender, nav,
                        on_log_updated=sent_updates.append)
    jm._sleep = lambda s: None          # 不真睡
    jm._rand = lambda a, b: a           # 确定性间隔
    return jm, queue, reader, sender, nav, sent_updates


# ------------------------------------------------------------------ 铁律
class TestIronRuleSync(unittest.TestCase):
    def test_sync_success_sends_log_updated(self):
        jm, q, reader, sender, nav, updates = make_journey()
        q.push_notify("A")
        entry = q.pop_next()
        jm.process_entry(entry)
        # 初次同步 + 末次同步
        self.assertEqual(reader.calls, 2)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].session, "A")
        self.assertEqual(nav.back_home_count, 1)
        self.assertEqual(jm.take_dirty_sessions(), [])

    def test_sync_retry_then_success(self):
        """同步失败重试：前 2 次失败，第 3 次（重试上限内）成功。"""
        jm, q, reader, sender, nav, updates = make_journey(
            reader=FakeReader(fail_times=2))
        q.push_notify("A")
        jm.process_entry(q.pop_next())
        self.assertEqual(reader.calls, 4)   # 初次 1+2 次重试成功 + 末次同步 1 次
        self.assertEqual(jm.take_dirty_sessions(), [])

    def test_sync_failure_marks_dirty(self):
        """同步彻底失败（重试 2 次仍失败）→ 允许退出，会话标 dirty。"""
        jm, q, reader, sender, nav, updates = make_journey(
            reader=FakeReader(fail_times=99))
        q.push_notify("A")
        sent = jm.process_entry(q.pop_next())
        self.assertFalse(sent)
        self.assertEqual(reader.calls, SYNC_MAX_RETRIES + 1)
        self.assertEqual(jm.take_dirty_sessions(), ["A"])
        self.assertEqual(nav.back_home_count, 1)   # 允许退出
        self.assertEqual(len(updates), 0)          # 未回传

    def test_final_sync_failure_marks_dirty(self):
        """行动清空了但末次同步失败 → 不发 LogUpdated，标 dirty。"""
        reader = FakeReader()
        real_sync = reader.sync_session
        state = {"n": 0}

        def sync(session, is_group):
            state["n"] += 1
            if state["n"] >= 2:   # 初次成功，末次及重试全失败
                return None
            return real_sync(session, is_group)

        reader.sync_session = sync
        jm, q, reader, sender, nav, updates = make_journey(reader=reader)
        q.push_action("A", "<b/>")
        jm.process_entry(q.pop_next())
        self.assertEqual(len(updates), 0)              # 静默吞掉是 bug，不许发
        self.assertEqual(jm.take_dirty_sessions(), ["A"])

    def test_dirty_cleared_on_success(self):
        jm, q, reader, sender, nav, updates = make_journey()
        jm._dirty.add("A")
        q.push_notify("A")
        jm.process_entry(q.pop_next())
        self.assertEqual(jm.take_dirty_sessions(), [])


# ------------------------------------------------------------------ 行动
class TestActions(unittest.TestCase):
    def test_action_executed_and_log_updated_after_cleared(self):
        jm, q, reader, sender, nav, updates = make_journey()
        q.push_action("A", "<b/>")
        sent = jm.process_entry(q.pop_next())
        self.assertTrue(sent)
        self.assertEqual(sender.submitted, [("A", "<b/>")])
        self.assertEqual(len(updates), 1)   # LogUpdated 在行动清空后才发

    def test_action_retry_uses_requeue_entry(self):
        """B2：行动失败 → requeue_entry，attempts 递增而非重置。"""
        jm, q, reader, sender, nav, updates = make_journey(
            sender=FakeSender(ok=False, retryable=True))
        q.push_action("A", "<b/>")
        jm.process_entry(q.pop_next())
        self.assertIn("A", q)
        e = q.pop_next()
        self.assertEqual(e.attempts, 1)     # 旧 bug：永远 0
        self.assertEqual(e.payload, "<b/>") # 行动未丢

    def test_action_not_retried_when_not_retryable(self):
        jm, q, reader, sender, nav, updates = make_journey(
            sender=FakeSender(ok=False, retryable=False))
        q.push_action("A", "<b/>")
        jm.process_entry(q.pop_next())
        self.assertNotIn("A", q)

    def test_enter_session_failure_requeues_action(self):
        jm, q, reader, sender, nav, updates = make_journey(
            nav=FakeNav(enter_ok=False))
        q.push_action("A", "<b/>")
        sent = jm.process_entry(q.pop_next())
        self.assertFalse(sent)
        self.assertIn("A", q)
        self.assertEqual(q.pop_next().attempts, 1)

    def test_sync_failure_does_not_drop_action(self):
        """同步彻底失败退出时，行动条目也必须退回队列（行动永不许丢）。"""
        jm, q, reader, sender, nav, updates = make_journey(
            reader=FakeReader(fail_times=99))
        q.push_action("A", "<b/>")
        jm.process_entry(q.pop_next())
        self.assertIn("A", q)
        self.assertEqual(sender.submitted, [])   # 未执行

    def test_followup_absorbed_until_cleared(self):
        """follow-up：执行期间新来的行动一并吸收，直到清空（无轮数上限）。"""
        jm, q, reader, sender, nav, updates = make_journey()
        q.push_action("A", "<b1/>")
        entry = q.pop_next()
        q.push_action("A", "<b2/>")   # 处理期间又来
        q.push_action("A", "<b3/>")
        jm.process_entry(entry)
        self.assertEqual(sender.submitted,
                         [("A", "<b1/>"), ("A", "<b2/><b3/>")])
        self.assertEqual(len(q), 0)
        self.assertEqual(len(updates), 1)

    def test_absorb_fuse_prevents_deadloop(self):
        """保险丝：行动持续涌入时连续吸收 ABSORB_FUSE_ROUNDS 轮后强制退出。"""
        from src.interaction.loop import journey as journey_mod
        sender = FakeSender()

        class EndlessQueue(UnifiedQueue):
            """每次 pop_session 都还有新行动（模拟对端不停发）。"""
            def pop_session(self, session):
                e = super().pop_session(session)
                if e is None:
                    self.push_action(session, "<more/>")
                    e = super().pop_session(session)
                return e

        q = EndlessQueue()
        jm, q, reader, sender, nav, updates = make_journey(queue=q, sender=sender)
        q.push_action("A", "<b/>")
        jm.process_entry(q.pop_next())
        self.assertEqual(len(sender.submitted),
                         1 + journey_mod.ABSORB_FUSE_ROUNDS)
        self.assertEqual(len(updates), 1)   # 铁律：退出前仍回传


# ------------------------------------------------------------------ 暂停不丢行动（run_loop）
class FakeScanner:
    frame_bus = None

    def sweep(self):
        return []


class FakeWatcher:
    def start(self): pass
    def stop(self): pass


class FakeTools:
    dev = SimpleNamespace(wake_and_dim=lambda: None,
                          restore_screen=lambda: None)

    def back_to_home(self): pass


def make_loop(paused, queue=None, journey=None):
    if queue is None:
        queue = UnifiedQueue()
    processed = []
    if journey is None:
        journey = SimpleNamespace(process_entry=processed.append,
                                  take_dirty_sessions=lambda: [],
                                  navigator=FakeNav())
    config = SimpleNamespace(paused=paused)
    loop = InteractionLoop(FakeScanner(), FakeWatcher(), queue,
                           journey, FakeTools(), config)
    loop._sleep = lambda s: None
    return loop, queue, processed


class TestPausedNoActionLoss(unittest.TestCase):
    def test_paused_drain_keeps_action_drops_notify(self):
        loop, q, processed = make_loop(paused=True)
        q.push_notify("N")
        q.push_action("A", "<b/>")
        loop._drain_queue()
        self.assertEqual(processed, [])      # 暂停不分发
        self.assertNotIn("N", q)             # notify 可丢（红点会再触发）
        self.assertIn("A", q)                # 行动保留
        e = q.pop_next()
        self.assertEqual(e.payload, "<b/>")

    def test_paused_wait_requeues_action(self):
        loop, q, processed = make_loop(paused=True)
        q.push_action("A", "<b/>")
        loop._wait_and_dispatch(timeout=0.0)
        self.assertEqual(processed, [])
        self.assertIn("A", q)

    def test_unpaused_dispatch(self):
        loop, q, processed = make_loop(paused=False)
        q.push_action("A", "<b/>")
        loop._wait_and_dispatch(timeout=0.0)
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0].session, "A")


class TestDirtyResync(unittest.TestCase):
    def test_dirty_sessions_pushed_to_front(self):
        q = UnifiedQueue()
        q.push_notify("B")
        journey = SimpleNamespace(
            process_entry=lambda e: None,
            take_dirty_sessions=lambda: ["A"],
            navigator=FakeNav())
        loop, q, processed = make_loop(paused=False, queue=q, journey=journey)
        loop._resync_dirty()
        # A 以 mention 插队到队首
        self.assertEqual(q.pop_next().session, "A")
        self.assertEqual(q.pop_next().session, "B")


if __name__ == "__main__":
    unittest.main()
