# -*- coding: utf-8 -*-
"""tests/test_moments_reader.py — FeedStitcher 拼接/水位 + 样本滚动集成。

合成数据测试为纯逻辑（无 OCR/设备）；
集成测试用 assets/samples/moments/04→05→06 模拟滚动序列，需 OCR 引擎。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from src.interaction.ports.android.perception.moments_reader import (
    FeedStitcher, entry_fp, _norm)


def mk_entry(nick, text, time=None, dots=True, likes=None, comments=None,
             partial_bottom=False):
    lines = [l for l in text.split("\n") if l]
    return {
        "idx": 1, "avatar": {"x": 87, "y": 300, "w": 105, "h": 92},
        "nickname": nick, "text": text,
        "text_items": [{"text": l} for l in lines],
        "time": time, "time_cy": 1000 if time else None,
        "dots": {"cx": 990, "cy": 1000, "verified": True} if dots else None,
        "fulltext_btn": None,
        "likes": list(likes or []), "comments": list(comments or []),
        "images": [], "partial_top": False, "partial_bottom": partial_bottom,
        "complete": False,
    }


def mk_comment(frm, to, content):
    return {"from_user": frm, "reply_to": to, "content": content,
            "raw_text": "", "y0": 0, "y1": 10, "click_x": 300, "click_y": 5,
            "blue_ranges": [], "conf": 0.9}


def mk_orphan(text_lines=(), time=None, dots=False, likes=(), comments=()):
    return {
        "text_items": [{"text": l} for l in text_lines],
        "comments": list(comments), "likes": list(likes),
        "time": time, "time_cy": 500 if time else None,
        "dots": {"cx": 990, "cy": 500, "verified": True} if dots else None,
    }


class StitcherBasicTest(unittest.TestCase):

    def test_cross_screen_text_stitch(self):
        """条目跨屏：正文尾巴拼接 + 重叠行去重 + 时间/点补齐。"""
        s = FeedStitcher()
        e1 = mk_entry("陈曦", "第一行\n第二行\n第三行", partial_bottom=True)
        s.feed([e1])
        self.assertFalse(s.entries[0]["complete"])

        orphan = mk_orphan(text_lines=["第三行", "第四行"],
                           time="昨天", dots=True,
                           likes=["Doo"], comments=[mk_comment("A", None, "好")])
        changed = s.feed([], orphan)
        self.assertTrue(changed)
        e = s.entries[0]
        self.assertEqual(e["text"], "第一行\n第二行\n第三行\n第四行")
        self.assertEqual(e["time"], "昨天")
        self.assertIsNotNone(e["dots"])
        self.assertEqual(e["likes"], ["Doo"])
        self.assertEqual(len(e["comments"]), 1)
        self.assertTrue(e["text_complete"])

    def test_same_entry_reappears_no_dup(self):
        """滚动重叠导致同一条目再次入屏 → 合并而非重复。"""
        s = FeedStitcher()
        s.feed([mk_entry("陈曦", "今天群里好热闹", partial_bottom=True)])
        again = mk_entry("陈曦", "今天群里好热闹\n续了一行",
                         time="昨天", likes=["Doo"])
        s.feed([again])
        self.assertEqual(len(s.entries), 1)
        e = s.entries[0]
        self.assertIn("续了一行", e["text"])
        self.assertEqual(e["time"], "昨天")
        self.assertEqual(e["likes"], ["Doo"])

    def test_empty_text_continuation(self):
        """头像在屏底正文未入屏（fp='nick|'）→ 下一屏同昵称条目要合并。"""
        s = FeedStitcher()
        s.feed([mk_entry("陈曦", "", partial_bottom=True)])
        self.assertEqual(s.entries[0]["fp"], "陈曦|")
        s.feed([mk_entry("陈曦", "正文出现了", time="昨天")])
        self.assertEqual(len(s.entries), 1)
        self.assertEqual(s.entries[0]["text"], "正文出现了")

    def test_different_entries_same_author(self):
        """同作者相邻两条不同正文 → 两条独立条目。"""
        s = FeedStitcher()
        s.feed([mk_entry("陈曦", "今天群里好热闹", partial_bottom=True)])
        s.feed([mk_entry("陈曦", "今日份摸鱼日记", time="昨天")])
        self.assertEqual(len(s.entries), 2)

    def test_comment_dedup(self):
        """同一条评论跨屏出现 → 去重。"""
        s = FeedStitcher()
        c = mk_comment("Leisure", "Doo", "不可以")
        s.feed([mk_entry("陈曦", "正文", partial_bottom=True,
                         comments=[c])])
        s.feed([mk_entry("陈曦", "正文", time="昨天",
                         comments=[mk_comment("Leisure", "Doo", "不可以"),
                                   mk_comment("Doo", "Leisure", "[表情]")])])
        self.assertEqual(len(s.entries), 1)
        self.assertEqual(len(s.entries[0]["comments"]), 2)


class StitcherWatermarkTest(unittest.TestCase):

    def test_watermark_stop(self):
        """命中水位指纹 → 停止收录，水位以下不入列表。"""
        old_fp = entry_fp("陈曦", "今日份摸鱼日记")
        s = FeedStitcher(watermark_fp=old_fp)
        s.feed([mk_entry("陈曦", "新条目", time="6小时前"),
                mk_entry("陈曦", "今日份摸鱼日记", time="昨天"),
                mk_entry("陈曦", "更旧的", time="前天")])
        self.assertTrue(s.hit_watermark)
        self.assertEqual(len(s.entries), 1)
        self.assertEqual(s.entries[0]["text"], "新条目")

    def test_watermark_first_run(self):
        """无水位（首次）→ 全部收录。"""
        s = FeedStitcher(watermark_fp=None)
        s.feed([mk_entry("陈曦", "A"), mk_entry("陈曦", "B")])
        self.assertFalse(s.hit_watermark)
        self.assertEqual(len(s.entries), 2)

    def test_latest_fp(self):
        s = FeedStitcher()
        self.assertIsNone(s.latest_fp())
        s.feed([mk_entry("陈曦", "最新一条")])
        self.assertEqual(s.latest_fp(), entry_fp("陈曦", "最新一条"))


class NormTest(unittest.TestCase):

    def test_fp_ignores_whitespace_and_time(self):
        a = entry_fp("陈曦", "今天 群里\n好热闹")
        b = entry_fp("陈 曦", "今天群里好热闹")
        self.assertEqual(a, b)  # 空白全去掉
        self.assertNotIn("昨天", a)
        self.assertEqual(_norm(" a b\n"), "ab")


SAMPLES = os.path.join(ROOT, "assets", "samples", "moments")


@unittest.skipUnless(os.path.isdir(SAMPLES), "样本截图不存在")
class SampleScrollIntegrationTest(unittest.TestCase):
    """04→05→06 模拟滚动序列：跨屏拼接 + 去重 + 水位。"""

    @classmethod
    def setUpClass(cls):
        try:
            import cv2  # noqa: F401
            from src.interaction.ports.android.perception import (
                ocr_engine, moments_parser)
            cls.ok = True
        except Exception:
            cls.ok = False
            return
        cls.screens = []
        for name in ("04_feed_top", "05_feed_scrolled1", "06_feed_scrolled2"):
            img = cv2.imread(os.path.join(SAMPLES, f"{name}.png"))
            items = ocr_engine.run_ocr(img)
            entries, _ = moments_parser.parse_moments_entries(img, items)
            first_y = (entries[0]["avatar"]["y"] - moments_parser.NICK_DY - 10) \
                if entries else moments_parser.SCREEN_H
            orphan = moments_parser.parse_top_orphan(img, items, first_y)
            cls.screens.append((entries, orphan))

    def setUp(self):
        if not getattr(self, "ok", False):
            self.skipTest("cv2/OCR 不可用")

    def test_stitched_feed(self):
        s = FeedStitcher()
        for entries, orphan in self.screens:
            s.feed(entries, orphan)
        self.assertFalse(s.hit_watermark)
        # 04 的"今天群里好热闹" + 05/06 的"今日份摸鱼日记" + "热水～" + 底部 partial
        self.assertEqual(len(s.entries), 4)

        e1 = s.entries[0]
        self.assertEqual(e1["nickname"], "陈曦")
        self.assertIn("今天群里好热闹", e1["text"])
        self.assertIn("主人今天只冒", e1["text"])       # 05 孤儿段拼上的尾巴
        self.assertEqual(e1["time"], "昨天")            # 孤儿段补的时间
        self.assertIsNotNone(e1["dots"])
        self.assertTrue(e1["text_complete"])
        self.assertIn("桐", e1["likes"])
        self.assertEqual(len(e1["comments"]), 4)        # 许湘悦/Leisure/风图/如空
        frm = [c["from_user"] for c in e1["comments"]]
        self.assertEqual(frm, ["许湘悦", "Leisure", "风图", "如空"])

        e2 = s.entries[1]
        self.assertIn("今日份摸鱼日记", e2["text"])
        self.assertEqual(len(e2["comments"]), 3)        # 含 [表情] 去重后 3 条
        contents = [c["content"] for c in e2["comments"]]
        self.assertIn("[表情]", contents)
        self.assertEqual(len(e2["likes"]), 8)

        e3 = s.entries[2]
        self.assertEqual(e3["text"], "热水～")
        self.assertEqual(len(e3["comments"]), 5)

        e4 = s.entries[3]
        self.assertFalse(e4["complete"])                # 底部 partial

    def test_watermark_against_samples(self):
        """以第 2 条为水位重读 → 只收到第 1 条。"""
        s1 = FeedStitcher()
        for entries, orphan in self.screens:
            s1.feed(entries, orphan)
        wm_fp = s1.entries[1]["fp"]

        s2 = FeedStitcher(watermark_fp=wm_fp)
        for entries, orphan in self.screens:
            s2.feed(entries, orphan)
            if s2.hit_watermark:
                break
        self.assertTrue(s2.hit_watermark)
        self.assertEqual(len(s2.entries), 1)
        self.assertIn("今天群里好热闹", s2.entries[0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
