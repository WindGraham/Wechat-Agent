# -*- coding: utf-8 -*-
"""test_media_pass.py — 媒体独立处置 pass 的离线单测（假 dev/handler，不连真机）。

覆盖：
- _find_on_screen：模板在屏内命中（bbox 正确）/ 不在屏内返回 None / 太矮跳过
- run_media_pass 全链：定位 → handle → update_media 写回（链接 URL 进日志）
- 定位失败：滚 MAX_SCREENS_PER_ITEM 屏后标 failed，并滚回最新（护书签）
- 红包：不点击直接标 done
- 预算：max_items 限量
"""

import os
import shutil
import tempfile
import unittest

import cv2
import numpy as np

from src.interaction.msglog import message_log as ml
from src.interaction.loop import media_pass as mp
from src.interaction.ports.android.perception.media_handler import (
    MediaResult, MediaTask)


def _screen_with_patch(patch, top=800, left=0):
    """1080x2340 灰底屏，在 (left, top) 放入 patch。"""
    screen = np.full((2340, 1080, 3), 237, dtype=np.uint8)
    h, w = patch.shape[:2]
    screen[top:top + h, left:left + w] = patch
    return screen


def _patch(w=1080, h=120, seed=7):
    rng = np.random.RandomState(seed)
    return rng.randint(0, 255, (h, w, 3), dtype=np.uint8)


class _FakeDev:
    """capture_bytes 依次返回 screens；do_swipe 经 _shell 记数。"""

    def __init__(self, screens):
        self._screens = list(screens)
        self.shell_calls = 0
        self.swipe_calls = 0

    def capture_bytes(self):
        if len(self._screens) > 1:
            return self._screens.pop(0)
        return self._screens[0]

    def _shell(self, cmd):
        self.shell_calls += 1
        return b""

    def swipe(self, *a, **kw):
        self.swipe_calls += 1


class _FakeHandler:
    """记录 task 并按 msg_type 返回成功结果。"""

    def __init__(self, dev):
        self.tasks = []

    def handle(self, task: MediaTask) -> MediaResult:
        self.tasks.append(task)
        if task.msg_type == "card":
            return MediaResult(msg_id=task.msg_id, msg_type="link",
                               content="https://example.com/x", success=True)
        return MediaResult(msg_id=task.msg_id, msg_type="image",
                           content="/tmp/pulled.jpg", success=True)


class FindOnScreenTest(unittest.TestCase):
    def test_hit(self):
        patch = _patch()
        screen = _screen_with_patch(patch, top=800)
        bbox = mp._find_on_screen(screen, patch)
        self.assertIsNotNone(bbox)
        x, y, w, h = bbox
        self.assertEqual((x, y, w, h), (0, 800, 1080, 120))

    def test_miss(self):
        screen = np.full((2340, 1080, 3), 237, dtype=np.uint8)
        self.assertIsNone(mp._find_on_screen(screen, _patch()))

    def test_too_short_template_skipped(self):
        patch = _patch(h=20)
        screen = _screen_with_patch(patch)
        self.assertIsNone(mp._find_on_screen(screen, patch))

    def test_tall_template_uses_head(self):
        tall = _patch(h=900)
        screen = _screen_with_patch(tall[:400], top=500)
        self.assertIsNotNone(mp._find_on_screen(screen, tall[:400]))


class RunMediaPassTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="media_pass_")
        self.conn = ml.connect(os.path.join(self._dir, "chatlog.db"))
        self.sid = ml.get_or_create_session(self.conn, "猫猫群", True)
        # 存档裁图（落临时文件，crop_path 用绝对路径）
        self.patch = _patch()
        self.crop_file = os.path.join(self._dir, "crop.jpg")
        cv2.imwrite(self.crop_file, self.patch)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _add_media(self, content="[链接]", ctype="link", seq_content=None):
        placeholder = content

        class E:
            kind = "msg"
            sender = "张三"
            is_mine = False
            content_type = ctype
            mentions = []
            ocr_conf = None
            complete = 1
            partial_top = False
            partial_bottom = False
            media_path = ""
            crop_path = self.crop_file
            content = seq_content or placeholder
        e = E()
        ml.append_incremental(self.conn, self.sid, [e], gap_ok=True)
        return self.conn.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY seq DESC",
            (self.sid,)).fetchone()["id"]

    def test_link_end_to_end(self):
        msg_id = self._add_media()
        screen = _screen_with_patch(self.patch)
        dev = _FakeDev([screen])
        stats = mp.run_media_pass(dev, self.conn, "猫猫群",
                                  handler_cls=_FakeHandler,
                                  sleep_fn=lambda s: None)
        self.assertEqual(stats["handled"], 1)
        row = self.conn.execute(
            "SELECT content, media_status, content_type FROM messages"
            " WHERE id=?", (msg_id,)).fetchone()
        self.assertEqual(row["content"], "[链接] https://example.com/x")
        self.assertEqual(row["media_status"], "done")
        self.assertEqual(row["content_type"], "link")

    def test_locate_fail_marks_failed_and_scrolls_back(self):
        from unittest import mock
        msg_id = self._add_media()
        blank = np.full((2340, 1080, 3), 237, dtype=np.uint8)
        dev = _FakeDev([blank])
        with mock.patch.object(mp.RS, "scroll_to_latest") as m_scroll:
            stats = mp.run_media_pass(dev, self.conn, "猫猫群",
                                      handler_cls=_FakeHandler,
                                      sleep_fn=lambda s: None)
        self.assertEqual(stats["failed"], 1)
        row = self.conn.execute(
            "SELECT media_status FROM messages WHERE id=?", (msg_id,)).fetchone()
        self.assertEqual(row["media_status"], "failed")
        # 滚过屏（定位）且结束后 scroll_to_latest 回滚（护书签）
        self.assertGreater(dev.shell_calls, 0)
        m_scroll.assert_called_once()

    def test_red_packet_no_click(self):
        msg_id = self._add_media(content="[红包]", ctype="red_packet")
        dev = _FakeDev([np.full((2340, 1080, 3), 237, dtype=np.uint8)])
        handler = _FakeHandler(dev)
        stats = mp.run_media_pass(dev, self.conn, "猫猫群",
                                  handler_cls=lambda d: handler,
                                  sleep_fn=lambda s: None)
        self.assertEqual(stats["handled"], 1)
        self.assertEqual(handler.tasks, [])           # 红包不点击
        row = self.conn.execute(
            "SELECT media_status FROM messages WHERE id=?", (msg_id,)).fetchone()
        self.assertEqual(row["media_status"], "done")

    def test_budget_limits_items(self):
        for _ in range(3):
            self._add_media(seq_content=f"[链接]{os.urandom(2).hex()}")
        screen = _screen_with_patch(self.patch)
        dev = _FakeDev([screen])
        stats = mp.run_media_pass(dev, self.conn, "猫猫群", max_items=2,
                                  handler_cls=_FakeHandler,
                                  sleep_fn=lambda s: None)
        self.assertEqual(stats["handled"], 2)
        left = self.conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE session_id=?"
            " AND media_status=''", (self.sid,)).fetchone()["c"]
        self.assertEqual(left, 1)                     # 剩余下轮继续

    def test_image_writes_media_path(self):
        class E:
            kind = "msg"
            sender = "张三"
            is_mine = False
            content = "[图片]"
            content_type = "image"
            mentions = []
            ocr_conf = None
            complete = 1
            partial_top = False
            partial_bottom = False
            media_path = ""
            crop_path = self.crop_file
        ml.append_incremental(self.conn, self.sid, [E()], gap_ok=True)
        msg_id = self.conn.execute(
            "SELECT id FROM messages WHERE session_id=?",
            (self.sid,)).fetchone()["id"]
        screen = _screen_with_patch(self.patch)
        dev = _FakeDev([screen])
        stats = mp.run_media_pass(dev, self.conn, "猫猫群",
                                  handler_cls=_FakeHandler,
                                  sleep_fn=lambda s: None)
        self.assertEqual(stats["handled"], 1)
        row = self.conn.execute(
            "SELECT content, media_path, media_status FROM messages"
            " WHERE id=?", (msg_id,)).fetchone()
        self.assertEqual(row["content"], "[图片]")     # 占位符不动
        self.assertEqual(row["media_path"], "/tmp/pulled.jpg")
        self.assertEqual(row["media_status"], "done")


if __name__ == "__main__":
    unittest.main()
