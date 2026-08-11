# -*- coding: utf-8 -*-
"""test_reader.py — 感知层 Reader 离线单测（假 frame_bus/dev，不触真机）。

覆盖（docs/BUGREPORT_TIMING_RACE_20260810.md §3.1/§5.2）：
- read_current 截到非聊天页帧（搜索→聊天转场动画）时等待稳定并重截，
  最多 READ_PAGE_RETRIES 次；重截后仍非聊天页则按原样返回不死等
- 首帧即聊天页时不重截（零额外开销）
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.interaction.ports.android.perception.reader import Reader


def _state(page_type):
    return {"page": {"type": page_type, "title": "t"}, "elements": []}


class FakeFrameBus:
    """按序返回预置 state；latest_raw 返回 None（_slice_entries 直接降级）。"""

    def __init__(self, states):
        self._states = list(states)
        self.captures = 0

    def capture(self):
        self.captures += 1
        return self._states[min(self.captures - 1, len(self._states) - 1)]

    def latest_raw(self):
        return None


def make_reader(states):
    dev = SimpleNamespace(wait_random=lambda a, b: None)
    tools = SimpleNamespace(dev=dev)
    bus = FakeFrameBus(states)
    # conn 仅需非 None（read_current 路径不触库）
    return Reader(tools, bus, conn=object()), bus


class TestReadCurrentTransition(unittest.TestCase):
    def test_chat_page_first_frame_no_retry(self):
        r, bus = make_reader([_state("wechat_chat")])
        entries, state = r.read_current()
        self.assertEqual(bus.captures, 1)
        self.assertEqual(state["page"]["type"], "wechat_chat")

    def test_transition_frame_retried_until_chat(self):
        """搜索页过渡帧 → 重截到聊天页为止。"""
        r, bus = make_reader([_state("wechat_search"), _state("wechat_chat")])
        entries, state = r.read_current()
        self.assertEqual(bus.captures, 2)
        self.assertEqual(state["page"]["type"], "wechat_chat")

    def test_non_chat_page_gives_up_after_retries(self):
        """一直不是聊天页：重试 READ_PAGE_RETRIES 次后按原样返回（不死等）。"""
        r, bus = make_reader([_state("wechat_home")])
        entries, state = r.read_current()
        self.assertEqual(bus.captures, 1 + Reader.READ_PAGE_RETRIES)
        self.assertEqual(state["page"]["type"], "wechat_home")


# ------------------------------------------------------------------ 会话名单
class TestKnownSessions(unittest.TestCase):
    """SessionReader.known_sessions（2026-08-10 发错群事故）：
    决策层跨会话投递需要准确会话名——按最近活跃排序、空会话不上榜。"""

    def setUp(self):
        import tempfile
        self._dir = tempfile.mkdtemp(prefix="reader_test_")
        from src.interaction.msglog import message_log as ml
        self._ml = ml
        self.conn = ml.connect(os.path.join(self._dir, "chatlog.db"))

    def tearDown(self):
        import shutil
        self.conn.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _append(self, name, is_group, content, ts=None):
        ml = self._ml
        sid = ml.get_or_create_session(self.conn, name, is_group)
        e = SimpleNamespace(
            sender="某人", content=content, content_type="text",
            is_mine=False, mentions=[], ocr_conf=None, complete=1,
            partial_top=False, partial_bottom=False, media_path="")
        if ts is not None:
            e.ts_hint = ts
        ml.append_incremental(self.conn, sid, [e])

    def test_sorted_by_recent_activity(self):
        import time
        from src.interaction.reader.session_reader import SessionReader
        now = time.time()
        self._append("老群", True, "旧消息")
        self._append("活跃群", True, "新消息")
        # 把"老群"的消息时间戳往回拨，保证排序按活跃时间而非插入序
        self.conn.execute(
            "UPDATE messages SET ts_captured=? WHERE content='旧消息'",
            (now - 3600,))
        self.conn.commit()
        # 无消息的空会话不上榜
        self._ml.get_or_create_session(self.conn, "空会话", True)
        # 私聊也上榜（带 is_group 标记）
        self._append("风图", False, "私聊消息")

        sr = SessionReader(None, self.conn)
        pairs = sr.known_sessions()
        names = [n for n, _ in pairs]
        self.assertNotIn("空会话", names)
        self.assertLess(names.index("活跃群"), names.index("老群"))
        self.assertIn(("风图", False), pairs)
        self.assertIn(("活跃群", True), pairs)


if __name__ == "__main__":
    unittest.main()
