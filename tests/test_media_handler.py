# -*- coding: utf-8 -*-
"""test_media_handler.py — MediaHandler 纯函数/解析逻辑离线单测。

用 monkeypatch 把 media_handler.run_ocr 换成合成 OCR item（run_ocr 返回格式
dicts），避免依赖真机/真 OCR 模型。覆盖：记录页解析、页面签名判断、
URL 拼接、bbox 工具、OCR 定位、MediaResult 转写。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.interaction.ports.android.perception import media_handler as mh


def item(x0, y0, x1, y1, text, conf=0.99):
    """构造 run_ocr 格式 item（box=(x0,y0,x1,y1)）。"""
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    return {"box": (float(x0), float(y0), float(x1), float(y1)),
            "cx": cx, "cy": cy, "h": float(y1 - y0),
            "text": text.strip(), "conf": conf}


class _FakeDev:
    """最小 fake device：只暴露 MediaHandler 用到的 tap/wait/back/shell。"""
    def __init__(self):
        self.taps = []
        self.backs = 0

    def tap(self, x, y):
        self.taps.append((x, y))

    def wait_random(self, a, b):
        pass

    def back(self):
        self.backs += 1


class MediaHandlerOffline(unittest.TestCase):
    """不触真机的纯逻辑测试（run_ocr 已 mock）。"""

    def setUp(self):
        self._orig_ocr = mh.run_ocr
        self.ocr_items = []
        mh.run_ocr = lambda _img=None: list(self.ocr_items)
        self.addCleanup(self._restore_ocr)
        self.h = mh.MediaHandler(_FakeDev())

    def _restore_ocr(self):
        mh.run_ocr = self._orig_ocr

    def _set_ocr(self, items):
        self.ocr_items = list(items)

    # ------------------------------------------------------------- 纯函数

    def test_join_url_lines(self):
        self.assertEqual(
            mh._join_url_lines(["https://mp.weixin.qq.com/s/", "s9FIHjp"]),
            "https://mp.weixin.qq.com/s/s9FIHjp")
        self.assertIsNone(mh._join_url_lines(["hello 世界"]))

    def test_record_ts_re(self):
        self.assertTrue(mh._RECORD_TS_RE.match("8月13日01:08:33"))
        self.assertTrue(mh._RECORD_TS_RE.match("12月1日23:59:59"))
        self.assertFalse(mh._RECORD_TS_RE.match("01:08:33"))
        self.assertFalse(mh._RECORD_TS_RE.match("8月13日 01:08:33"))

    def test_bbox_center_rect(self):
        self.assertEqual(mh._bbox_center((156, 848, 690, 378)), (501, 1037))
        r = mh._bbox_rect((156, 848, 690, 378))
        self.assertEqual((r.x, r.y, r.w, r.h), (156, 848, 690, 378))

    def test_find_text_opt(self):
        items = [item(100, 100, 300, 140, "复制链接"), item(500, 500, 600, 540, "保存图片")]
        self.assertEqual(mh.MediaHandler._find_text_opt(items, ("复制链接",)), (200, 120))
        self.assertEqual(mh.MediaHandler._find_text_opt(items, ("不存在的", "保存图片")), (550, 520))
        self.assertIsNone(mh.MediaHandler._find_text_opt(items, ("不存在",)))

    def test_to_message_entry(self):
        r = mh.MediaResult(msg_id="m1", msg_type="link", content="http://x", raw_files=["a.png"], success=True)
        e = r.to_message_entry()
        self.assertEqual(e["content_type"], "link")
        self.assertEqual(e["content"], "http://x")
        self.assertIn("a.png", e["media_path"])

    # ------------------------------------------------------------- 记录页解析

    def test_parse_chat_record_screen(self):
        items = [
            item(60, 12, 223, 57, "22:34:47"),          # 状态栏噪声 -> 滤掉
            item(166, 933, 243, 952, "风图"),            # 消息1 发送者
            item(782, 933, 1041, 952, "8月13日01:16:15"),  # 消息1 时间戳
            item(162, 958, 364, 1019, "还没开呢"),        # 消息1 内容
            item(164, 1095, 243, 1144, "风图"),           # 消息2 发送者
            item(783, 1100, 1041, 1139, "8月13日01:16:36"),  # 消息2 时间戳
            item(167, 1152, 985, 1219, "目前接了gemini 3.1 pro preview，dsv4，"),
            item(161, 1217, 230, 1274, "k3"),            # 消息2 内容续
        ]
        self._set_ocr(items)
        recs = self.h._parse_chat_record_screen(None)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["sender"], "风图")
        self.assertEqual(recs[0]["time"], "8月13日01:16:15")
        self.assertEqual(recs[0]["content"], "还没开呢")
        self.assertEqual(recs[1]["sender"], "风图")
        self.assertIn("gemini", recs[1]["content"])
        self.assertIn("k3", recs[1]["content"])

    def test_parse_chat_record_screen_filters_top_chrome(self):
        items = [
            item(60, 12, 223, 57, "22:34:47"),
            item(372, 93, 705, 150, "群聊的聊天记录"),
            item(166, 933, 243, 952, "风图"),
            item(782, 933, 1041, 952, "8月13日01:16:15"),
            item(162, 958, 364, 1019, "还没开呢"),
        ]
        self._set_ocr(items)
        recs = self.h._parse_chat_record_screen(None)
        self.assertEqual([r["content"] for r in recs], ["还没开呢"])

    # ------------------------------------------------------------- 页面签名

    def test_detect_page_signature(self):
        cases = {
            "chat_record": "群聊的聊天记录",
            "webview": "复制链接",
            "photo_viewer": "保存图片",
            "video_viewer": "保存视频",
            "sticker_detail": "更多表情",
            "file_card": "用其他应用打开",
        }
        import numpy as np
        big_h, big_w = 2340, 1080
        dummy = np.zeros((big_h, big_w, 3), np.uint8)
        for sig, text in cases.items():
            self._set_ocr([item(60, int(0.9 * big_h), 400, int(0.9 * big_h) + 40, text)])
            self.assertEqual(self.h._detect_page_signature(dummy), sig, sig)

    # ------------------------------------------------------------- 桥接（slice -> task）

    def test_handle_label(self):
        self.assertEqual(mh._handle_label("media", {}), "media")
        self.assertEqual(mh._handle_label("card", {"sub": "chat_record"}), "card")
        self.assertEqual(mh._handle_label("card", {"sub": "file"}), "card")
        self.assertEqual(mh._handle_label("red_packet", {}), "red_packet")
        self.assertIsNone(mh._handle_label("text", {}))
        self.assertIsNone(mh._handle_label("quote", {}))
        self.assertIsNone(mh._handle_label("unknown", {}))

    def test_classify_slice_to_task(self):
        import numpy as np
        from src.interaction.ports.android.perception import media_handler as _mh
        orig = _mh.classify_segment
        # bubble_rect=[x,y,w,h]，content 给 OCR 文本
        _mh.classify_segment = lambda crop, ocr_text="": ("card", {"sub": "chat_record"})
        try:
            img = np.zeros((2340, 1080, 3), np.uint8)
            msg = {"content_type": "multimedia", "bubble_rect": [156, 848, 690, 378],
                   "content": "群聊的聊天记录", "side": "other"}
            task, label = _mh.classify_slice_to_task(img, msg, "猫猫群")
            self.assertEqual(label, "card")
            self.assertIsNotNone(task)
            self.assertEqual(task.msg_type, "card")
            self.assertEqual(task.bbox, (156, 848, 690, 378))
            self.assertEqual(task.group_name, "猫猫群")
        finally:
            _mh.classify_segment = orig

        # 文本消息 -> 跳过
        _mh.classify_segment = lambda crop, ocr_text="": ("text", {})
        try:
            task2, label2 = _mh.classify_slice_to_task(img, msg, "猫猫群")
            self.assertIsNone(task2)
        finally:
            _mh.classify_segment = orig

    def test_write_manifest(self):
        import tempfile, json, os as _os
        from src.interaction.ports.android.perception.media_handler import MediaTask, MediaResult
        d = tempfile.mkdtemp()
        task = MediaTask(msg_id="m1", msg_type="card", bbox=(1, 2, 3, 4), screen_path="", group_name="g")
        res = MediaResult(msg_id="m1", msg_type="chat_record",
                          content={"records": [{"sender": "a"}]},
                          raw_files=[_os.path.join(d, "screens", "s0.png")],
                          success=True, run_dir=d)
        self.h._write_manifest(task, res)
        man = json.load(open(_os.path.join(d, "manifest.json"), encoding="utf-8"))
        self.assertEqual(man["msg_id"], "m1")
        self.assertEqual(man["type"], "chat_record")
        self.assertEqual(man["bbox"], [1, 2, 3, 4])
        self.assertEqual(man["result"]["records"][0]["sender"], "a")
        self.assertTrue(man["success"])

    def test_media_to_entry_and_key(self):
        """realtime_scan 接入 helper：MediaResult -> message_log entry + 去重 key。"""
        from src.interaction.loop.realtime_scan import _media_to_entry, _entry_key
        from src.interaction.ports.android.perception.media_handler import MediaResult
        r = MediaResult(msg_id="m1", msg_type="link",
                        content="https://mp.weixin.qq.com/s/x",
                        raw_files=["/w/a.png", "/w/b.png"], success=True)
        m = {"side": "other", "matched_user_name": "风图", "content": "群聊的聊天记录"}
        e = _media_to_entry(r, m, "猫猫群", "17:58")
        self.assertEqual(e.sender, "风图")
        self.assertEqual(e.content_type, "link")
        self.assertEqual(e.complete, 1)
        self.assertEqual(e.content, "https://mp.weixin.qq.com/s/x")
        self.assertEqual(e.media_path, "/w/a.png;/w/b.png")
        self.assertEqual(e.time_hint, "17:58")
        self.assertIn("link", _entry_key(e))
        # dict content -> JSON 字符串
        r2 = MediaResult(msg_id="m2", msg_type="chat_record",
                         content={"records": ["a"]}, raw_files=[], success=True)
        e2 = _media_to_entry(r2, m, "g", None)
        self.assertEqual(e2.content, '{"records": ["a"]}')
