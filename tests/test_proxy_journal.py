# -*- coding: utf-8 -*-
"""test_proxy_journal.py — proxy._journal 事件流水与 scanner home_scan 快照。

全离线：journal 路径与 home_scan 路径都替换到 tmp 目录，不碰真实 workspace/。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.decision.proxy import proxy as proxy_mod          # noqa: E402
from src.interaction.ports.android.perception import scanner as scanner_mod  # noqa: E402


class JournalTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "proxy_events.jsonl")
        self._old_path = proxy_mod.EVENTS_PATH
        proxy_mod.EVENTS_PATH = self.path

    def tearDown(self):
        proxy_mod.EVENTS_PATH = self._old_path

    def _lines(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_journal_writes_json_line(self):
        proxy_mod._journal("decision_start", session="特高课",
                           trigger="有人@我", new_messages=2)
        proxy_mod._journal("route", session="特高课", blocks=["reply"],
                           deliveries=[{"session": "特高课", "ok": True}])
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        rec = json.loads(lines[0])
        self.assertEqual(rec["type"], "decision_start")
        self.assertEqual(rec["session"], "特高课")
        self.assertEqual(rec["new_messages"], 2)
        self.assertIn("ts", rec)
        rec2 = json.loads(lines[1])
        self.assertEqual(rec2["deliveries"][0]["ok"], True)

    def test_journal_creates_parent_dir(self):
        os.rmdir(self.tmp)  # 目录不存在也能写
        proxy_mod._journal("tick")
        self.assertTrue(os.path.isfile(self.path))

    def test_journal_rotates_oversized_file(self):
        # 造一个超限文件（垃圾内容 + 一条完整旧记录）→ 归档轮转，旧数据不丢
        old_rec = json.dumps({"ts": 1, "type": "old"},
                             ensure_ascii=False) + "\n"
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("x" * proxy_mod.EVENTS_MAX_BYTES)
            f.write(old_rec)
        proxy_mod._journal("route", session="s")
        # 新文件从头写，只有新记录
        recs = [json.loads(ln) for ln in self._lines()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[-1]["type"], "route")
        # 旧文件被完整归档（垃圾 + 旧记录都在），没有丢数据
        archives = sorted(f for f in os.listdir(self.tmp)
                          if f.startswith("proxy_events.jsonl."))
        self.assertEqual(len(archives), 1)
        with open(os.path.join(self.tmp, archives[0]), encoding="utf-8") as f:
            data = f.read()
        self.assertIn(old_rec, data)
        self.assertTrue(data.startswith("x" * proxy_mod.EVENTS_MAX_BYTES))

    def test_journal_rotates_garbage_only_file(self):
        # 超限且完全没有换行：归档旧文件，新行从头写
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("x" * (proxy_mod.EVENTS_MAX_BYTES + 10))
        proxy_mod._journal("tick", n=1)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["type"], "tick")
        # 垃圾文件同样被归档留存
        archives = [f for f in os.listdir(self.tmp)
                    if f.startswith("proxy_events.jsonl.")]
        self.assertEqual(len(archives), 1)
        self.assertEqual(
            os.path.getsize(os.path.join(self.tmp, archives[0])),
            proxy_mod.EVENTS_MAX_BYTES + 10)

    def test_clip_marks_truncation(self):
        text = "a" * (proxy_mod.PROMPT_JOURNAL_LIMIT + 100)
        out = proxy_mod._clip(text)
        self.assertLess(len(out), len(text))
        self.assertIn("已截断", out)
        short = proxy_mod._clip("hello")
        self.assertEqual(short, "hello")


class HomeScanTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "runtime", "home_scan.json")
        self._old_path = scanner_mod.HOME_SCAN_PATH
        scanner_mod.HOME_SCAN_PATH = self.path

    def tearDown(self):
        scanner_mod.HOME_SCAN_PATH = self._old_path

    def test_write_home_scan_snapshot(self):
        """_write_home_scan 把 state 里的全部会话条目落成 JSON。"""
        sc = scanner_mod.Scanner.__new__(scanner_mod.Scanner)  # 绕过构造（不碰设备）
        state = {"elements": [
            {"type": "session_item", "label": "特高课", "unread_count": 3,
             "unread_kind": "number", "mention_me": True, "muted": False,
             "partial": False},
            {"type": "session_item", "label": "摸鱼群", "unread_count": 0,
             "unread_kind": "dot", "mention_me": False, "muted": True,
             "partial": True},
            {"type": "top_bar", "label": "微信"},   # 非会话条目不收录
        ]}
        sc._write_home_scan(state)
        with open(self.path, encoding="utf-8") as f:
            snap = json.load(f)
        self.assertIn("ts", snap)
        self.assertEqual(len(snap["sessions"]), 2)
        s0 = snap["sessions"][0]
        self.assertEqual(s0, {"label": "特高课", "unread_count": 3,
                              "unread_kind": "number", "mention_me": True,
                              "muted": False, "partial": False})
        s1 = snap["sessions"][1]
        self.assertEqual(s1["unread_kind"], "dot")
        self.assertTrue(s1["muted"])
        self.assertTrue(s1["partial"])

    def test_write_home_scan_atomic_tmp_cleaned(self):
        sc = scanner_mod.Scanner.__new__(scanner_mod.Scanner)
        sc._write_home_scan({"elements": []})
        self.assertTrue(os.path.isfile(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["sessions"], [])

    def test_write_home_scan_failure_swallowed(self):
        """路径不可写时只记日志，不抛异常。"""
        scanner_mod.HOME_SCAN_PATH = os.path.join(self.tmp, "not-a-dir",
                                                  "x", "home_scan.json")
        # "not-a-dir" 是文件：makedirs 建子目录会失败
        with open(os.path.join(self.tmp, "not-a-dir"), "w") as f:
            f.write("block")
        sc = scanner_mod.Scanner.__new__(scanner_mod.Scanner)
        with self.assertLogs("perception.scanner", level="ERROR"):
            sc._write_home_scan({"elements": []})


if __name__ == "__main__":
    unittest.main()
