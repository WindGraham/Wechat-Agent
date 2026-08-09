# -*- coding: utf-8 -*-
"""群聊热情度配置（网关 → 人格卡映射）离线测试。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.gateway.group_config import LEVELS, read_level, write_level   # noqa: E402


class GroupConfigTest(unittest.TestCase):

    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_level(td, "交流一下？", "mention", "少发表情包")
            self.assertTrue(os.path.exists(path))
            level, extra = read_level(td, "交流一下？")
            self.assertEqual(level, "mention")
            self.assertEqual(extra, "少发表情包")

    def test_no_card(self):
        with tempfile.TemporaryDirectory() as td:
            level, extra = read_level(td, "不存在的群")
            self.assertIsNone(level)
            self.assertEqual(extra, "")

    def test_bad_level(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                write_level(td, "x", "crazy")

    def test_card_rules_content(self):
        import yaml
        with tempfile.TemporaryDirectory() as td:
            write_level(td, "特高课", "active")
            data = yaml.safe_load(open(
                os.path.join(td, "特高课.yaml"), encoding="utf-8"))
            rules = data["rules"]["reply_rules"]
            self.assertTrue(any("积极回复" in r["action"] for r in rules))
            self.assertEqual(data["x_level"], "active")

    def test_filename_sanitize(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_level(td, 'a/b:c', "normal")
            self.assertNotIn("/", os.path.basename(path)[:-5])


if __name__ == "__main__":
    unittest.main()
