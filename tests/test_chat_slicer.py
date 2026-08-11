# -*- coding: utf-8 -*-
"""test_chat_slicer.py — chat_slicer 引用块（quote）识别回归。

覆盖：_match_quote 配对逻辑（引用+文字泡 / 引用+媒体 / 孤儿引用块 /
昵称条带排除），以及真机截图端到端验证。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from interaction.ports.android.perception.chat_slicer import (
    _match_quote, slice_chat)
from interaction.ports.android.perception.ocr_engine import run_ocr

IMG_PATH = "/tmp/cur.png"      # 真机截图（含 MambaSpirit 引用+视频消息）


def _ocr_item(text, cx, cy, w=300, h=40):
    """构造 run_ocr 格式 item（box 以 cx,cy 为中心）。"""
    x0, y0 = cx - w / 2, cy - h / 2
    return {"box": (x0, y0, x0 + w, y0 + h),
            "cx": cx, "cy": cy, "h": h, "text": text, "conf": 1.0}


def _make_quote(qx, qy, qw=420, qh=70):
    """构造 quotes 列表元素 (rect, mean_val)。"""
    return ((qx, qy, qw, qh, qw * qh), 35.0)


class MatchQuoteTest(unittest.TestCase):
    def setUp(self):
        self.quotes = [_make_quote(180, 560)]
        self.used = set()
        self.ocr = [
            _ocr_item("引用人：被引用内容", 390, 595, w=420, h=36),
            _ocr_item("回复正文", 390, 720, w=200, h=40),
        ]
        self.consumed = [False] * len(self.ocr)

    def test_match_bubble_above(self):
        """引用块在气泡上方：配对成功，引用文字提取，consumed 标记。"""
        # 气泡 (180, 660, 420, 120)
        qt, qi = _match_quote(
            self.quotes, self.used, self.ocr, self.consumed,
            (180, 660, 420, 120), 660, 780)
        self.assertEqual(qt, "引用人：被引用内容")
        self.assertEqual(qi, 0)
        self.assertIn(0, self.used)
        self.assertTrue(self.consumed[0])       # 引用文字已消费
        self.assertFalse(self.consumed[1])      # 正文未消费（留给气泡）

    def test_match_gap_too_large(self):
        """引用块距气泡太远（>115px）：不配对。"""
        qt, qi = _match_quote(
            self.quotes, self.used, self.ocr, self.consumed,
            (180, 800, 420, 120), 800, 920)     # gap = 800-630 = 170
        self.assertIsNone(qt)
        self.assertIsNone(qi)
        self.assertNotIn(0, self.used)

    def test_match_no_x_overlap(self):
        """水平不重叠（引用块在左、气泡在右）：不配对。"""
        qt, qi = _match_quote(
            self.quotes, self.used, self.ocr, self.consumed,
            (700, 660, 200, 120), 660, 780)     # x 无重叠
        self.assertIsNone(qt)

    def test_used_quote_skipped(self):
        """已被占用的引用块不重复配对。"""
        self.used.add(0)
        qt, qi = _match_quote(
            self.quotes, self.used, self.ocr, self.consumed,
            (180, 660, 420, 120), 660, 780)
        self.assertIsNone(qt)

    def test_quote_below_bubble(self):
        """引用块在气泡下方（罕见）：gap_below 也接受。"""
        quotes = [_make_quote(180, 700)]
        ocr = [_ocr_item("引用人：被引用内容", 390, 735, w=420, h=36)]
        consumed = [False]
        qt, _ = _match_quote(
            quotes, set(), ocr, consumed,
            (180, 560, 420, 120), 560, 680)     # gap_below = 700-680 = 20
        self.assertEqual(qt, "引用人：被引用内容")


class SliceChatRegressionTest(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(IMG_PATH),
                         "真机截图不存在，跳过端到端验证")
    def test_real_screenshot_quote(self):
        """真机截图端到端：MambaSpirit 引用+视频 → quote。"""
        import cv2
        img = cv2.imread(IMG_PATH)
        ocr_items = run_ocr(img)
        sliced = slice_chat(img, ocr_items, is_group=True, title="YOUSAOBI")
        quotes = [m for m in sliced["messages"]
                  if m["content_type"] == "quote"]
        self.assertEqual(len(quotes), 1, sliced["messages"])
        self.assertIn("偷我表情包", quotes[0]["content"])
        self.assertIn("我最多陪你玩7分钟", quotes[0]["content"])
        # 昵称行不应混入 content（MambaSpirit 只在 sender）
        self.assertNotIn("MambaSpirit", quotes[0]["content"].split("\n")[1:])

    @unittest.skipUnless(os.path.exists(IMG_PATH),
                         "真机截图不存在，跳过端到端验证")
    def test_real_screenshot_no_false_quote(self):
        """真机截图：无引用块的普通消息不误标 quote。"""
        import cv2
        img = cv2.imread(IMG_PATH)
        ocr_items = run_ocr(img)
        sliced = slice_chat(img, ocr_items, is_group=True, title="YOUSAOBI")
        texts = [m for m in sliced["messages"]
                 if m["content_type"] == "text"]
        self.assertGreaterEqual(len(texts), 1, sliced["messages"])
        # 非 quote 消息不误配引用内容（不把引用文字粘进普通 text）
        for m in sliced["messages"]:
            if m["content_type"] != "quote":
                self.assertNotIn("偷我表情包", m["content"])


if __name__ == "__main__":
    unittest.main()
