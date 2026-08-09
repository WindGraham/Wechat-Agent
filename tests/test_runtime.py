# -*- coding: utf-8 -*-
"""test_runtime.py — src/shared/runtime.py 的单测（unittest，不连真机）。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.runtime import RuntimeConfig, DEFAULTS


class RuntimeConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "runtime.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, data, mtime=None):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        if mtime is not None:
            os.utime(self.path, (mtime, mtime))

    # ------------------------------------------------------------------ 加载
    def test_load_fields(self):
        self._write({"paused": True, "owner": "特高课", "history_size": 50})
        cfg = RuntimeConfig(self.path)
        self.assertTrue(cfg.get("paused"))
        self.assertEqual(cfg.get("owner"), "特高课")
        self.assertEqual(cfg.get("history_size"), 50)
        # 属性访问与 get() 等价
        self.assertEqual(cfg.owner, "特高课")
        self.assertEqual(cfg.config.history_size, 50)

    def test_defaults_fallback(self):
        # 缺字段 → DEFAULTS 兜底
        self._write({"owner": "me"})
        cfg = RuntimeConfig(self.path)
        for key, val in DEFAULTS.items():
            if key == "owner":
                continue
            self.assertEqual(cfg.get(key), val, key)
        # 完全未知字段 → 调用方 default
        self.assertEqual(cfg.get("no_such_key", "x"), "x")
        self.assertIsNone(cfg.get("no_such_key"))
        with self.assertRaises(AttributeError):
            cfg.no_such_key

    def test_missing_file_uses_defaults(self):
        cfg = RuntimeConfig(self.path)  # 文件不存在
        self.assertEqual(cfg.get("history_size"), 200)
        self.assertFalse(cfg.get("paused"))

    def test_bad_json_keeps_previous(self):
        self._write({"paused": True}, mtime=1000)
        cfg = RuntimeConfig(self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        os.utime(self.path, (2000, 2000))
        self.assertFalse(cfg.check())     # mtime 变了但解析失败 → reload 未生效
        self.assertTrue(cfg.get("paused"))  # 解析失败保留旧值

    # ------------------------------------------------------------------ 热重读
    def test_hot_reload_on_mtime_change(self):
        self._write({"paused": False}, mtime=1000)
        cfg = RuntimeConfig(self.path)
        self.assertFalse(cfg.get("paused"))

        self.assertFalse(cfg.check())     # mtime 未变 → 不重读

        self._write({"paused": True}, mtime=2000)
        self.assertTrue(cfg.check())      # mtime 变了 → 重读
        self.assertTrue(cfg.get("paused"))

    def test_on_change_callback(self):
        self._write({"history_size": 100}, mtime=1000)
        cfg = RuntimeConfig(self.path)
        seen = []
        cfg.on_change(lambda c: seen.append(c.get("history_size")))
        self.assertEqual(seen, [100])     # 注册即对齐初值

        self._write({"history_size": 300}, mtime=2000)
        cfg.check()
        self.assertEqual(seen, [100, 300])

    def test_interval_types(self):
        # sweep_interval / notify_interval 是区间列表，可 unpack 给 random.uniform
        self._write({})
        cfg = RuntimeConfig(self.path)
        lo, hi = cfg.get("sweep_interval")
        self.assertEqual((lo, hi), (45, 90))
        lo, hi = cfg.notify_interval
        self.assertEqual((lo, hi), (3, 6))


if __name__ == "__main__":
    unittest.main()
