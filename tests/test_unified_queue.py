# -*- coding: utf-8 -*-
"""test_unified_queue.py — 统一时间序队列离线单测（假依赖，不触真机）。

覆盖关键规则（docs/INTERACTION_LAYER.md §5）：
- 去重不挪动 / mention 粘滞不挪动位置
- 行动吞并通知
- @我 插队队首
- 行动失败重试：未达上限保持原位，达上限排队尾（attempts 归零）
- reinsert（暂停模式行动不丢）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.interaction.loop.unified_queue import UnifiedQueue, QueueEntry


class TestDedupNoMove(unittest.TestCase):
    def test_dedup_keeps_position_and_content(self):
        q = UnifiedQueue()
        a = q.push_notify("A")
        q.push_notify("B")
        a2 = q.push_notify("A", source="notify")
        # 同一条目、位置不变（ts 未更新）
        self.assertIs(a, a2)
        order = [e.session for e in q.snapshot()]
        self.assertEqual(order, ["A", "B"])
        # sources 登记了新来源
        self.assertEqual(a.sources, {"sweep", "notify"})
        # 出队顺序仍是 A 先
        self.assertEqual(q.pop_next().session, "A")

    def test_mention_sticky_without_moving(self):
        """mention 升级是对'去重不挪动'的有意例外：只改优先级标记，不改 ts。"""
        q = UnifiedQueue()
        a = q.push_notify("A")
        ts_before = a.ts
        q.push_notify("A", mention=True)
        self.assertTrue(a.mention)
        self.assertEqual(a.ts, ts_before)


class TestActionSwallowNotify(unittest.TestCase):
    def test_action_swallows_notify(self):
        q = UnifiedQueue()
        n = q.push_notify("A")
        ts_before = n.ts
        e = q.push_action("A", "<bundle/>")
        self.assertIs(e, n)                      # 同一坑位
        self.assertEqual(e.kind, "action")
        self.assertEqual(e.payload, "<bundle/>")
        self.assertEqual(e.ts, ts_before)        # 位置不变
        self.assertEqual(len(q), 1)              # 通知被吞并

    def test_action_merges_payload(self):
        q = UnifiedQueue()
        q.push_action("A", "<b1/>")
        e = q.push_action("A", "<b2/>")
        self.assertEqual(e.payload, "<b1/><b2/>")
        self.assertEqual(len(q), 1)


class TestPriorityJump(unittest.TestCase):
    def test_mention_jumps_to_front(self):
        q = UnifiedQueue()
        q.push_notify("A")
        q.push_notify("B")
        q.push_notify("C", mention=True)
        self.assertEqual(q.pop_next().session, "C")
        self.assertEqual(q.pop_next().session, "A")
        self.assertEqual(q.pop_next().session, "B")


class TestRequeueEntry(unittest.TestCase):
    def test_retry_keeps_position_before_limit(self):
        """B2：失败重试计数器——pop 后 requeue_entry 保留 attempts。"""
        q = UnifiedQueue(max_attempts=2)
        e = q.push_action("A", "<b/>")
        popped = q.pop_next()
        self.assertIs(popped, e)
        self.assertEqual(len(q), 0)
        r = q.requeue_entry(popped)
        self.assertIs(r, e)
        self.assertEqual(r.attempts, 1)
        self.assertEqual(r.ts, e.ts)             # 保持原位置语义

    def test_max_attempts_requeue_at_tail(self):
        """失败 2 次达上限 → 排队尾（attempts 不归零；硬上限 2×max 后丢弃）。"""
        q = UnifiedQueue(max_attempts=2)
        a = q.push_action("A", "<b/>")
        q.push_action("B", "<b/>")
        # 第一次失败：原位重试
        p1 = q.pop_next()
        self.assertEqual(p1.session, "A")
        q.requeue_entry(p1)
        # A 原位（ts 更早），仍先出队
        p2 = q.pop_next()
        self.assertEqual(p2.session, "A")
        self.assertEqual(p2.attempts, 1)
        # 第二次失败：达上限 → 排队尾（attempts 保留为 2，ts 更新）
        t = q.requeue_entry(p2)
        self.assertEqual(t.attempts, 2)
        self.assertGreaterEqual(t.ts, a.ts)
        # B 现在排在 A 前面
        self.assertEqual(q.pop_next().session, "B")
        p3 = q.pop_next()          # A（attempts=2）
        self.assertEqual(p3.session, "A")
        q.requeue_entry(p3)        # attempts=3 → 队尾
        p4 = q.pop_session("A")
        self.assertIsNone(q.requeue_entry(p4))   # attempts=4=2×max → 丢弃
        self.assertNotIn("A", q)

    def test_requeue_action_backward_compat(self):
        """旧接口保留：条目不在队列时按新条目入队。"""
        q = UnifiedQueue()
        e = q.requeue_action("A", "<b/>")
        self.assertEqual(e.kind, "action")
        self.assertEqual(e.attempts, 0)
        self.assertIn("A", q)


class TestReinsert(unittest.TestCase):
    def test_reinsert_preserves_entry(self):
        """暂停模式：pop 出的行动原样放回，不碰 attempts/ts。"""
        q = UnifiedQueue()
        q.push_action("A", "<b/>")
        e = q.pop_next()
        e.attempts = 1
        ts = e.ts
        q.reinsert(e)
        self.assertIn("A", q)
        e2 = q.pop_next()
        self.assertIs(e2, e)
        self.assertEqual(e2.attempts, 1)
        self.assertEqual(e2.ts, ts)


if __name__ == "__main__":
    unittest.main()


class SnapshotTest(unittest.TestCase):
    """队列快照落盘（网关展示用）。"""

    def test_snapshot_written_on_mutation(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "queue.json")
            q = UnifiedQueue(snapshot_path=path)
            q.push_notify("特高课", mention=True)
            data = json.load(open(path, encoding="utf-8"))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["session"], "特高课")
            self.assertTrue(data[0]["mention"])
            q.push_action("风图", "<reply><text>hi</text></reply>")
            data = json.load(open(path, encoding="utf-8"))
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]["session"], "特高课")  # @我 排前
            q.pop_next()
            data = json.load(open(path, encoding="utf-8"))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["kind"], "action")
