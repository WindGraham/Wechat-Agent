# -*- coding: utf-8 -*-
"""test_web_fetch.py — 链接正文抓取的离线单测（mock requests，不联网）。

覆盖：
- _html_to_text：去 script/style/标签、标题前置、空白折叠
- fetch_text：HTTP 200 抽取正文并写磁盘缓存；非 200 / 异常返回 None
- 缓存命中不重抓（第二次调用无 HTTP）
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from src.decision.search import web_fetch as wf


HTML = """<!doctype html><html><head>
<title>  学生助理招新通知  </title>
<style>body{color:red}</style>
</head><body>
<script>var x=1;</script>
<h1>招新通知</h1>
<p>现面向全校招聘&nbsp;学生助理。</p>
<p>有意者请  扫码报名。</p>
</body></html>"""


class _Resp:
    status_code = 200
    text = HTML
    encoding = "utf-8"
    apparent_encoding = "utf-8"


class HtmlToTextTest(unittest.TestCase):
    def test_strip_and_title(self):
        text = wf._html_to_text(HTML)
        self.assertIn("学生助理招新通知", text)          # 标题
        self.assertIn("现面向全校招聘 学生助理。", text)  # 正文+实体
        self.assertNotIn("var x=1", text)                # script 去除
        self.assertNotIn("color:red", text)              # style 去除
        self.assertNotIn("<p>", text)                    # 标签去除


class FetchTextTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="web_fetch_")
        self._orig_cache = wf.CACHE_DIR
        wf.CACHE_DIR = self._dir

    def tearDown(self):
        wf.CACHE_DIR = self._orig_cache
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_fetch_and_cache(self):
        with mock.patch("requests.get", return_value=_Resp()) as m_get:
            text = wf.fetch_text("https://example.com/a")
            self.assertIsNotNone(text)
            self.assertIn("招新通知", text)
            self.assertEqual(m_get.call_count, 1)
            # 第二次：命中缓存，不再发 HTTP
            text2 = wf.fetch_text("https://example.com/a")
            self.assertEqual(text2, text)
            self.assertEqual(m_get.call_count, 1)

    def test_max_chars_truncated(self):
        with mock.patch("requests.get", return_value=_Resp()):
            text = wf.fetch_text("https://example.com/a", max_chars=10)
            self.assertEqual(len(text), 10)

    def test_http_error_returns_none(self):
        bad = _Resp()
        bad.status_code = 404
        with mock.patch("requests.get", return_value=bad):
            self.assertIsNone(wf.fetch_text("https://example.com/404"))

    def test_exception_returns_none(self):
        with mock.patch("requests.get", side_effect=RuntimeError("boom")):
            self.assertIsNone(wf.fetch_text("https://example.com/x"))

    def test_non_http_url_rejected(self):
        self.assertIsNone(wf.fetch_text("ftp://x"))
        self.assertIsNone(wf.fetch_text(""))


if __name__ == "__main__":
    unittest.main()
