# -*- coding: utf-8 -*-
"""好友申请流程单测：纯函数（查看按钮/昵称提取/去重）+ 队列 friend 条目
+ 旅程 friend 分发。全程假 dev，不触设备。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.interaction.ports.android.action.friend_requests import (
    find_view_buttons, extract_applicant_name, dedup_names,
    contacts_tab_has_dot)
from src.interaction.ports.android.perception import layout_consts as LC
from src.interaction.loop.unified_queue import UnifiedQueue
from src.interaction.loop.journey import JourneyManager


def _it(text, cx, cy, h=50):
    return {"text": text, "cx": cx, "cy": cy, "h": h}


class TestFindViewButtons(unittest.TestCase):
    def test_basic(self):
        items = [
            _it("查看", 965, 1005),          # ✓ 列表区右侧
            _it("查看", 965, 100),           # ✗ 太靠上（标题区）
            _it("查看", 300, 1005),          # ✗ 不在右侧
            _it("已添加", 965, 800),         # ✗ 非按钮文字
            _it("查看", 965, 2260),          # ✗ 太靠下（tab 栏）
            _it("查看", 965, 1340),
        ]
        btns = find_view_buttons(items)
        self.assertEqual([b["cy"] for b in btns], [1005, 1340])


class TestExtractName(unittest.TestCase):
    def test_nickname_same_line_message_below(self):
        """实测布局：昵称 cy≈按钮 cy，验证消息在下方且以'我是'开头。"""
        items = [
            _it("Andromeda", 291, 975, h=48),
            _it("查看", 965, 1005),
            _it("我是群聊“交流一下？”的Andromeda", 475, 1036, h=47),
        ]
        name = extract_applicant_name(items, {"cx": 965, "cy": 1005})
        self.assertEqual(name, "Andromeda")

    def test_exclude_avatar_column_noise(self):
        """头像列（cx<150）的 OCR 噪声（"四"/"C"）不能当昵称。"""
        items = [
            _it("如空", 218, 812, h=62),
            _it("四", 97, 845, h=43),
            _it("查看", 965, 810),
            _it("我是群聊“交流一下？”的如空", 414, 873, h=45),
        ]
        name = extract_applicant_name(items, {"cx": 965, "cy": 810})
        self.assertEqual(name, "如空")

    def test_message_not_picked(self):
        """验证消息（我是...）字再大也不能当昵称。"""
        items = [
            _it("v", 200, 700, h=30),
            _it("我是群聊“交流一下？”的v", 385, 704, h=60),
            _it("查看", 965, 704),
        ]
        name = extract_applicant_name(items, {"cx": 965, "cy": 704})
        self.assertEqual(name, "v")

    def test_no_name(self):
        items = [_it("查看", 965, 1005),
                 _it("我是余念可安", 286, 1037, h=47)]
        self.assertEqual(
            extract_applicant_name(items, {"cx": 965, "cy": 1005}), "")


class TestContactsTabDot(unittest.TestCase):
    """通讯录 tab 红点检测（合成 HSV 帧，不触设备）。"""

    def _frame(self, dot=False):
        import numpy as np
        import cv2
        img = np.zeros((LC.SCREEN_H, LC.SCREEN_W, 3), np.uint8)
        img[:] = (30, 30, 30)                       # 深色底
        if dot:
            x0, y0, x1, y1 = LC.TAB_ROIS["通讯录"][0]
            # 红点涂在图标 ROI 右上角（BGR 红）
            cv2.circle(img, (x1 - 18, y0 + 18), 16, (0, 0, 250), -1)
        return cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    def test_dot_detected(self):
        self.assertTrue(contacts_tab_has_dot(self._frame(dot=True)))

    def test_no_dot(self):
        self.assertFalse(contacts_tab_has_dot(self._frame(dot=False)))

    def test_dot_elsewhere_not_counted(self):
        """红点涂在微信 tab 上不算通讯录红点。"""
        import numpy as np
        import cv2
        img = np.zeros((LC.SCREEN_H, LC.SCREEN_W, 3), np.uint8)
        img[:] = (30, 30, 30)
        x0, y0, x1, y1 = LC.TAB_ROIS["微信"][0]
        cv2.circle(img, (x1 - 18, y0 + 18), 16, (0, 0, 250), -1)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        self.assertFalse(contacts_tab_has_dot(hsv))


class TestDedup(unittest.TestCase):
    def test_order_kept(self):
        self.assertEqual(dedup_names(["a", "b", "a", "", "c", "b"]),
                         ["a", "b", "c"])


class TestQueueFriend(unittest.TestCase):
    def test_push_friend_dedup(self):
        q = UnifiedQueue()
        e1 = q.push_friend(source="probe")
        self.assertEqual(e1.kind, "friend")
        self.assertEqual(e1.session, "新的朋友")
        e2 = q.push_friend(source="watch")
        self.assertIs(e1, e2)                      # 去重不挪动
        self.assertEqual(len(q), 1)
        self.assertEqual(e1.sources, {"probe", "watch"})

    def test_notify_redirect(self):
        """首页列表/通知里的"新的朋友"标签必须转成 friend 条目。"""
        q = UnifiedQueue()
        e = q.push_notify("新的朋友", source="notify")
        self.assertIsNotNone(e)
        self.assertEqual(e.kind, "friend")
        # 普通会话不受影响
        e2 = q.push_notify("怨憎会", source="watch")
        self.assertEqual(e2.kind, "notify")

    def test_snapshot_restore_friend(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "queue.json")
            q = UnifiedQueue(snapshot_path=p)
            q.push_friend(source="probe")
            q2 = UnifiedQueue(snapshot_path=p)
            n = q2.restore()
            self.assertEqual(n, 1)
            entry = q2.pop_next()
            self.assertEqual(entry.kind, "friend")


class _FakeNav:
    def __init__(self):
        self.tools = object()          # friend_ops 是假实现，不碰 tools
        self.backed = 0

    def back_to_home(self):
        self.backed += 1
        return True


class TestJourneyFriend(unittest.TestCase):
    def _mk(self, friend_ops, config=None):
        q = UnifiedQueue()
        jm = JourneyManager(q, session_reader=None, bundle_sender=None,
                            port_navigator=_FakeNav(),
                            config=config, friend_ops=friend_ops)
        return q, jm

    def test_auto_accept_flow(self):
        calls = []
        q, jm = self._mk({
            "accept": lambda tools, max_accept, sleep_fn: (
                calls.append(max_accept) or
                {"ok": True, "accepted": ["A", "B"], "remaining": 0,
                 "error": None}),
            "probe": lambda tools, sleep_fn: self.fail("auto_accept 不应巡检"),
        })
        q.push_friend(source="probe")
        sent = jm.process_entry(q.pop_next())
        self.assertTrue(sent)                  # 有通过 → True
        self.assertEqual(len(calls), 1)        # accept 被调一次

    def test_probe_only_when_auto_off(self):
        calls = []
        class Cfg:
            @staticmethod
            def get(k, d=None):
                return {"friend_auto_accept": False}.get(k, d)
        q, jm = self._mk({
            "accept": lambda *a, **k: self.fail("auto_accept=False 不应通过"),
            "probe": lambda tools, sleep_fn: (
                calls.append(1) or (3, ["A", "B", "C"])),
        }, config=Cfg)
        q.push_friend(source="auto_probe")
        sent = jm.process_entry(q.pop_next())
        self.assertFalse(sent)                 # 只巡检不通过 → False
        self.assertEqual(len(calls), 1)        # probe 被调一次

    def test_zero_pending_ok(self):
        """巡检为 0：流程正常结束，不重排、返回 False。"""
        q, jm = self._mk({
            "accept": lambda tools, max_accept, sleep_fn:
                {"ok": True, "accepted": [], "remaining": 0, "error": None},
            "probe": lambda tools, sleep_fn: (0, []),
        })
        q.push_friend(source="auto_probe")
        sent = jm.process_entry(q.pop_next())
        self.assertFalse(sent)
        self.assertEqual(len(q), 0)            # ok 不重排

    def test_failure_requeues(self):
        q, jm = self._mk({
            "accept": lambda tools, max_accept, sleep_fn:
                {"ok": False, "accepted": [], "remaining": -1,
                 "error": "无法进入新的朋友页"},
            "probe": lambda tools, sleep_fn: (None, []),
        })
        q.push_friend(source="probe")
        jm.process_entry(q.pop_next())
        self.assertEqual(len(q), 1)          # 失败已重排
        entry = q.pop_next()
        self.assertEqual(entry.kind, "friend")
        self.assertEqual(entry.attempts, 1)


if __name__ == "__main__":
    unittest.main()
