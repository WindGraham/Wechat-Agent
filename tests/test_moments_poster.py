# -*- coding: utf-8 -*-
"""moments_poster 离线单元测试：只测纯函数，不连手机。"""

import unittest

from src.interaction.ports.android.action.moments_poster import (
    _find_text, _is_moment_text_page, _is_moments_feed_page,
    _is_wechat_home, _is_discover_page, _norm_for_match,
)


class MomentsPosterPureTest(unittest.TestCase):

    def test_find_text_basic(self):
        items = [
            {"text": "朋友圈", "cx": 200, "cy": 200, "conf": 0.9},
            {"text": "发现", "cx": 540, "cy": 100, "conf": 0.9},
        ]
        hit = _find_text(items, "朋友圈")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["text"], "朋友圈")

    def test_find_text_region_filter(self):
        items = [
            {"text": "朋友圈", "cx": 200, "cy": 50, "conf": 0.9},
            {"text": "朋友圈", "cx": 200, "cy": 200, "conf": 0.9},
        ]
        hit = _find_text(items, "朋友圈", region=(0, 100, 1080, 300))
        self.assertEqual(hit["cy"], 200)

    def test_is_wechat_home(self):
        items = [
            {"text": "微信", "cx": 135, "cy": 2230, "conf": 0.9},
        ]
        self.assertTrue(_is_wechat_home(items))

    def test_is_discover_page(self):
        items = [
            {"text": "发现", "cx": 675, "cy": 2230, "conf": 0.9},
            {"text": "朋友圈", "cx": 200, "cy": 200, "conf": 0.9},
        ]
        self.assertTrue(_is_discover_page(items))

    def test_is_moment_text_page(self):
        items = [
            {"text": "发表文字", "cx": 540, "cy": 120, "conf": 0.9},
            {"text": "这一刻的想法", "cx": 200, "cy": 250, "conf": 0.9},
        ]
        self.assertTrue(_is_moment_text_page(items))

    def test_is_moments_feed_page(self):
        items = [
            {"text": "轻触更换封面", "cx": 540, "cy": 500, "conf": 0.9},
        ]
        self.assertTrue(_is_moments_feed_page(items))

    def test_norm_for_match(self):
        self.assertEqual(
            _norm_for_match("测试 朋友圈 自动化 001"),
            "测试朋友圈自动化001"
        )


if __name__ == "__main__":
    unittest.main()
