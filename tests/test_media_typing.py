# -*- coding: utf-8 -*-
"""test_media_typing.py — 媒体打标入库与写回的离线单测（不连真机）。

覆盖（2026-08-27 多媒体接入轮询）：
- messages 表 media_status 列迁移（老库 ALTER TABLE 兼容）
- update_media 按 id 精确写回（content/media_path/media_status/content_type）
- append_incremental 写入 frame_phash（媒体同图去重身份）
- history_collect._known/_key 的媒体去重语义：
  同一发送者的多个"[图片]"占位符不再互相误杀（dedup_hash 区分），
  同一图片跨 union（hamming 容差内）仍判重
"""

import os
import shutil
import tempfile
import unittest

from src.interaction.msglog import message_log as ml
from src.interaction.loop import history_collect as hc


class Entry:
    def __init__(self, sender, content, content_type="text", frame_phash=None):
        self.kind = "msg"
        self.sender = sender
        self.content = content
        self.content_type = content_type
        self.is_mine = False
        self.mentions = []
        self.ocr_conf = None
        self.complete = 1
        self.partial_top = False
        self.partial_bottom = False
        self.media_path = ""
        if frame_phash is not None:
            self.frame_phash = frame_phash


class MediaStatusTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="media_typing_")
        self.db = os.path.join(self._dir, "chatlog.db")
        self.conn = ml.connect(self.db)
        self.sid = ml.get_or_create_session(self.conn, "测试群", True)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_media_status_column_migrated(self):
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(messages)")}
        self.assertIn("media_status", cols)

    def test_frame_phash_written(self):
        # 真实路径存 16 字符 hex（64bit 无符号 int 超 SQLite INTEGER 上限）
        big = 0xFEDCBA9876543210   # > 2^63-1
        e = Entry("张三", "[图片]", "image", frame_phash=f"{big:016x}")
        r = ml.append_incremental(self.conn, self.sid, [e], gap_ok=True)
        self.assertEqual(r["inserted"], 1)
        row = self.conn.execute(
            "SELECT frame_phash FROM messages WHERE session_id=?",
            (self.sid,)).fetchone()
        self.assertEqual(int(row["frame_phash"], 16), big)

    def test_update_media_by_id(self):
        e = Entry("张三", "[图片]", "image")
        ml.append_incremental(self.conn, self.sid, [e], gap_ok=True)
        row = self.conn.execute(
            "SELECT id FROM messages WHERE session_id=?", (self.sid,)).fetchone()
        n = ml.update_media(self.conn, row["id"],
                            content="[链接] https://example.com/x",
                            media_path="/tmp/a.jpg", media_status="done",
                            content_type="link")
        self.assertEqual(n, 1)
        r2 = self.conn.execute(
            "SELECT content, content_norm, media_path, media_status,"
            " content_type FROM messages WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(r2["content"], "[链接] https://example.com/x")
        self.assertEqual(r2["content_norm"], ml.normalize(r2["content"]))
        self.assertEqual(r2["media_path"], "/tmp/a.jpg")
        self.assertEqual(r2["media_status"], "done")
        self.assertEqual(r2["content_type"], "link")

    def test_update_media_partial_and_noop(self):
        e = Entry("张三", "[图片]", "image")
        ml.append_incremental(self.conn, self.sid, [e], gap_ok=True)
        row = self.conn.execute(
            "SELECT id FROM messages WHERE session_id=?", (self.sid,)).fetchone()
        # 全部 None → 无操作
        self.assertEqual(ml.update_media(self.conn, row["id"]), 0)
        # 只标状态（红包路径）
        self.assertEqual(
            ml.update_media(self.conn, row["id"], media_status="done"), 1)
        r2 = self.conn.execute(
            "SELECT content, media_status FROM messages WHERE id=?",
            (row["id"],)).fetchone()
        self.assertEqual(r2["content"], "[图片]")     # content 未被误改
        self.assertEqual(r2["media_status"], "done")


class MediaDedupTest(unittest.TestCase):
    """history_collect._known/_key 的媒体去重语义（纯函数，无 db）。"""

    def _entry(self, content, dedup_hash=None, sender="张三", ctype="image"):
        e = Entry(sender, content, ctype)
        if dedup_hash is not None:
            e.dedup_hash = dedup_hash
        return e

    def test_same_placeholder_different_images_both_kept(self):
        """同一发送者两张不同图片（占位符相同）不再互相误杀。"""
        e1 = self._entry("[图片]", dedup_hash=0b1111000011110000)
        e2 = self._entry("[图片]", dedup_hash=0b0000111100001111)
        hashes = [e1.dedup_hash]
        self.assertFalse(hc._known(e2, [], set(), [], set(), hashes))

    def test_same_image_across_unions_deduped(self):
        """同一图片跨 union（aHash 有 ≤6 位抖动）判重。"""
        h = 0b1010101010101010
        e2 = self._entry("[图片]", dedup_hash=h ^ 0b101)  # 2 位差
        self.assertTrue(hc._known(e2, [], set(), [], set(), [h]))

    def test_media_key_unique_per_image(self):
        """媒体去重键带 hash（裁图文件名因此唯一，不互相覆盖）。"""
        e1 = self._entry("[图片]", dedup_hash=0xAAAA)
        e2 = self._entry("[图片]", dedup_hash=0xBBBB)
        self.assertNotEqual(hc._key(e1), hc._key(e2))

    def test_text_entries_unaffected(self):
        """文本条目仍走原有 fuzzy 去重。"""
        e = self._entry("你好", sender="张三", ctype="text")
        existing = [("张三", "text", "你好")]
        self.assertTrue(hc._known(e, existing, {hc._key(e)}, [], set(), []))


if __name__ == "__main__":
    unittest.main()
