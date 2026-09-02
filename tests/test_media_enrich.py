# -*- coding: utf-8 -*-
"""test_media_enrich.py — prompt 多媒体增强的离线单测。

覆盖：
- collect_media：链接 URL 提取/去重/限量/时间序；图片挑本地存在的 media_path
- build_link_block：成功拼装【链接内容】块；抓取失败跳过；全失败空串
- enrich：runtime 键控制（crawl 开关、限量）
- attach_images：user content 转 OpenAI 数组；无图原样返回
"""

import os
import shutil
import tempfile
import unittest

import cv2
import numpy as np

from src.decision.proxy import media_enrich as me


class _Msg:
    def __init__(self, content="", content_type="text", media_path="", seq=0):
        self.content = content
        self.content_type = content_type
        self.media_path = media_path
        self.seq = seq
        self.sender = "张三"
        self.is_mine = False


def _rt(d):
    return lambda k, default=None: d.get(k, default)


class CollectMediaTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="media_enrich_")
        self.img = os.path.join(self._dir, "a.jpg")
        cv2.imwrite(self.img, np.full((100, 200, 3), 128, dtype=np.uint8))

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_url_extract_dedup_limit_order(self):
        msgs = [
            _Msg("[链接] https://a.com/1", "link", seq=1),
            _Msg("[链接] https://a.com/2", "link", seq=2),
            _Msg("[链接] https://a.com/1", "link", seq=3),   # 重复 URL
            _Msg("[链接] https://a.com/3", "link", seq=4),
        ]
        urls, _ = me.collect_media(msgs, [], link_limit=2)
        # 最新优先 + URL 去重：seq4(a.com/3)、seq3(a.com/1) 为最新两条唯一链接，
        # seq1 的旧重复不占位；截断后恢复时间序
        self.assertEqual(urls, ["https://a.com/1", "https://a.com/3"])

    def test_image_local_only(self):
        msgs = [
            _Msg("[图片]", "image", media_path=self.img, seq=1),
            _Msg("[图片]", "image", media_path="/nonexistent/x.jpg", seq=2),
            _Msg("[表情包]", "sticker", media_path=self.img, seq=3),
        ]
        _, imgs = me.collect_media(msgs, [], image_limit=4)
        # 不存在的文件被跳过；两条指向同一文件 → 去重后只有 1 张
        # （最新优先：seq3 的表情包先入，标签取它）
        self.assertEqual(imgs, [(self.img, "张三发的表情包")])

    def test_non_link_types_ignored(self):
        msgs = [_Msg("看看 https://a.com 这个", "text", seq=1)]
        urls, _ = me.collect_media(msgs, [])
        self.assertEqual(urls, [])          # text 里的 URL 不算链接消息


class BuildLinkBlockTest(unittest.TestCase):
    def test_block_assembled(self):
        block = me.build_link_block(
            ["https://a.com/1", "https://b.com/2"],
            fetch_fn=lambda u, max_chars=3000: f"正文 of {u}")
        self.assertIn("【链接内容】", block)
        self.assertIn("🔗 https://a.com/1\n正文 of https://a.com/1", block)
        self.assertIn("正文 of https://b.com/2", block)

    def test_partial_failure_skipped(self):
        def fetch(u, max_chars=3000):
            return None if "bad" in u else "正文"
        block = me.build_link_block(["https://bad.com", "https://ok.com"],
                                    fetch_fn=fetch)
        self.assertNotIn("bad.com", block)
        self.assertIn("ok.com", block)

    def test_all_failed_empty(self):
        block = me.build_link_block(["https://x.com"],
                                    fetch_fn=lambda u, max_chars=3000: None)
        self.assertEqual(block, "")


class EnrichTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="media_enrich_")
        self.img = os.path.join(self._dir, "b.jpg")
        cv2.imwrite(self.img, np.full((50, 50, 3), 200, dtype=np.uint8))

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_crawl_disabled(self):
        msgs = [_Msg("[链接] https://a.com/1", "link", seq=1)]
        tail, _ = me.enrich(msgs, [], _rt({"prompt_crawl_links": False}))
        self.assertEqual(tail, "")

    def test_images_collected_regardless_of_crawl(self):
        msgs = [_Msg("[图片]", "image", media_path=self.img, seq=1)]
        _, imgs = me.enrich(msgs, [], _rt({"prompt_crawl_links": False}))
        self.assertEqual(imgs, [(self.img, "张三发的图片")])


class AttachImagesTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="media_enrich_")
        self.img = os.path.join(self._dir, "c.jpg")
        cv2.imwrite(self.img, np.full((1200, 900, 3), 90, dtype=np.uint8))

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_content_array(self):
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "文本"}]
        out, n = me.attach_images(messages, [self.img])
        self.assertEqual(n, 1)
        content = out[1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "文本"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"]
                        .startswith("data:image/jpeg;base64,"))
        # 原 messages 不被改
        self.assertEqual(messages[1]["content"], "文本")

    def test_no_image_passthrough(self):
        messages = [{"role": "user", "content": "文本"}]
        out, n = me.attach_images(messages, ["/nonexistent.jpg"])
        self.assertEqual(n, 0)
        self.assertEqual(out[0]["content"], "文本")

    def test_labeled_images_get_caption_blocks(self):
        """(path, label) 输入：每张图前插图注文本块（防多图混淆新旧）。"""
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "文本"}]
        out, n = me.attach_images(messages, [(self.img, "张三发的图片")])
        self.assertEqual(n, 1)
        content = out[1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "文本"})
        self.assertEqual(content[1],
                         {"type": "text", "text": "【图1：张三发的图片】"})
        self.assertEqual(content[2]["type"], "image_url")

    def test_downscale(self):
        b64 = me.encode_image_b64(self.img, max_px=600)
        import base64
        raw = base64.b64decode(b64)
        arr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        self.assertLessEqual(max(arr.shape[:2]), 600)


if __name__ == "__main__":
    unittest.main()
