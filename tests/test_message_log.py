# -*- coding: utf-8 -*-
"""test_message_log.py — msglog 离线单测（临时文件 db，不连真机）。

覆盖：
- 版本号递增（increment_sync_version / get_sync_version）
- 水位差分严格大于（get_new_since: seq > last_seq）
- 幂等重复同步（同一批 entries 二次 append 不重复入库）
- is_group 不被 get_or_create_session 覆写，set_session_kind 显式更新
- update_content 同步更新 content_norm
- media_path 写读全链路 + 老库 ALTER TABLE 兼容
"""

import os
import shutil
import sqlite3
import tempfile
import unittest

from src.interaction.msglog import message_log as ml


class Entry:
    """frame_align.Entry 兼容的 duck-typed 栈条目。"""

    def __init__(self, sender, content, content_type="text", is_mine=False,
                 mentions=None, media_path=""):
        self.kind = "msg"
        self.sender = sender
        self.content = content
        self.content_type = content_type
        self.is_mine = is_mine
        self.mentions = mentions or []
        self.ocr_conf = None
        self.complete = 1
        self.partial_top = False
        self.partial_bottom = False
        self.media_path = media_path


class MessageLogTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="msglog_test_")
        self.db = os.path.join(self._dir, "chatlog.db")
        self.conn = ml.connect(self.db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _session(self, name="测试群", is_group=True):
        return ml.get_or_create_session(self.conn, name, is_group)

    # ---------------------------------------------------------- 版本号
    def test_sync_version_increments(self):
        sid = self._session()
        self.assertEqual(ml.get_sync_version(self.conn, sid), 0)
        self.assertEqual(ml.increment_sync_version(self.conn, sid), 1)
        self.assertEqual(ml.increment_sync_version(self.conn, sid), 2)
        self.assertEqual(ml.get_sync_version(self.conn, sid), 2)

    # ---------------------------------------------------------- 水位差分（严格大于）
    def test_get_new_since_strictly_greater(self):
        sid = self._session()
        entries = [Entry("张三", f"消息{i}") for i in range(1, 6)]
        r = ml.append_incremental(self.conn, sid, entries)
        self.assertEqual(r["inserted"], 5)

        all_rows = ml.get_new_since(self.conn, sid, 0)
        self.assertEqual([m["seq"] for m in all_rows], [1, 2, 3, 4, 5])

        # seq == last_seq 的条目不得出现在差分里（严格大于）
        delta = ml.get_new_since(self.conn, sid, 3)
        self.assertEqual([m["seq"] for m in delta], [4, 5])
        self.assertEqual(ml.get_new_since(self.conn, sid, 5), [])

    # ---------------------------------------------------------- 幂等重复同步
    def test_repeated_sync_is_idempotent(self):
        sid = self._session()
        batch = [Entry("张三", "你好"), Entry("李四", "在吗"), Entry("张三", "回聊")]
        r1 = ml.append_incremental(self.conn, sid, batch)
        self.assertEqual(r1["inserted"], 3)
        n1 = len(ml.get_new_since(self.conn, sid, 0))

        # 完全重复的一批：一条都不许再入库
        r2 = ml.append_incremental(self.conn, sid, batch)
        self.assertEqual(r2["inserted"], 0)
        self.assertEqual(len(ml.get_new_since(self.conn, sid, 0)), n1)

        # 旧 2 条 + 新 1 条：只补新条，seq 续写不重排
        batch2 = batch[1:] + [Entry("李四", "好的")]
        r3 = ml.append_incremental(self.conn, sid, batch2)
        self.assertEqual(r3["inserted"], 1)
        rows = ml.get_new_since(self.conn, sid, 0)
        self.assertEqual([m["seq"] for m in rows], [1, 2, 3, 4])
        self.assertEqual(rows[-1]["content"], "好的")

    # ---------------------------------------------------------- is_group 不被覆写
    def test_is_group_not_overwritten_by_get_or_create(self):
        sid = self._session("群A", is_group=True)
        # 读路径拿不到真实 is_group，硬编码 False 再取一次：不得覆写成群->私
        sid2 = ml.get_or_create_session(self.conn, "群A", False)
        self.assertEqual(sid, sid2)
        self.assertTrue(ml.get_session_kind(self.conn, "群A"))

        # 显式写回真实值才允许改
        self.assertTrue(ml.set_session_kind(self.conn, "群A", False))
        self.assertFalse(ml.get_session_kind(self.conn, "群A"))
        # 值无变化时不更新
        self.assertFalse(ml.set_session_kind(self.conn, "群A", False))
        self.assertTrue(ml.set_session_kind(self.conn, "群A", True))
        self.assertTrue(ml.get_session_kind(self.conn, "群A"))

    # ---------------------------------------------------------- update_content 同步 norm
    def test_update_content_syncs_norm(self):
        sid = self._session()
        ml.append_incremental(self.conn, sid, [Entry("张三", "原始内容 Hello")])
        n = ml.update_content(self.conn, sid, "张三", "原始内容 Hello",
                              "[图片] 一只猫")
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT content, content_norm FROM messages WHERE session_id=?",
            (sid,)).fetchone()
        self.assertEqual(row["content"], "[图片] 一只猫")
        self.assertEqual(row["content_norm"], ml.normalize("[图片] 一只猫"))
        # 匹配不到时不改
        self.assertEqual(
            ml.update_content(self.conn, sid, "王五", "不存在", "x"), 0)

    # ---------------------------------------------------------- media_path 读写
    def test_media_path_roundtrip(self):
        sid = self._session()
        entries = [
            Entry("张三", "看这张图"),
            Entry("李四", "[图片]", content_type="multimedia",
                  media_path="workspace/media/测试群/m2.png"),
        ]
        ml.append_incremental(self.conn, sid, entries)
        rows = ml.get_new_since(self.conn, sid, 0)
        self.assertEqual(rows[0]["media_path"], "")
        self.assertEqual(rows[1]["media_path"], "workspace/media/测试群/m2.png")
        ctx = ml.get_context(self.conn, sid)
        self.assertEqual(ctx[1]["media_path"], "workspace/media/测试群/m2.png")

    # ---------------------------------------------------------- 老库 ALTER 兼容
    def test_existing_db_migrated_with_media_path(self):
        # 造一个没有 media_path 列的老库（现 schema 去掉该行，模拟旧版建表）
        self.conn.close()
        os.remove(self.db)
        old_schema = "\n".join(
            line for line in ml.SCHEMA.splitlines()
            if "media_path" not in line)
        raw = sqlite3.connect(self.db)
        raw.executescript(old_schema)
        raw.commit()
        raw.close()

        conn = ml.connect(self.db)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        self.assertIn("media_path", cols)
        # 迁移后的库读写正常
        sid = ml.get_or_create_session(conn, "老库群", True)
        ml.append_incremental(conn, sid, [
            Entry("张三", "[图片]", content_type="multimedia",
                  media_path="workspace/media/老库群/m1.png")])
        rows = ml.get_new_since(conn, sid, 0)
        self.assertEqual(rows[0]["media_path"], "workspace/media/老库群/m1.png")
        conn.close()
        # 重新打开本测试的标准连接，避免 tearDown 重复 close 报错
        self.conn = ml.connect(self.db)


if __name__ == "__main__":
    unittest.main()
