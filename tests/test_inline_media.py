# -*- coding: utf-8 -*-
"""test_inline_media.py — 滚动采集中内联媒体处置的离线单测（不连真机）。

2026-09-01 用户定稿：滚动采集识别到本屏完整露出的多媒体消息 → 立即
点击处置（取 URL/存图），取完校验位置再继续滑动。覆盖：
- 完整露出的链接段：入库 → 调用 handler → URL 写回（content/media_status）
- 半显媒体段（交接处残段）：本轮不入库不处置
- 红包：入库标 done，不点击（不调用 handler）
- 预算 media_max=0：入库但不动，留 media_status='' 给 media_pass 兜底
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np

from src.interaction.loop import history_collect as hc
from src.interaction.msglog import message_log as ml
from src.interaction.ports.android.perception.media_handler import MediaResult

RNG = np.random.default_rng(42)
FRAME = RNG.integers(0, 255, (2340, 1080, 3), dtype=np.uint8)  # 固定噪声屏


class FakeDev:
    def capture_bytes(self):
        return FRAME.copy()

    def back(self):
        pass


class FakeHandler:
    def __init__(self, result):
        self.result = result
        self.tasks = []

    def handle(self, task):
        self.tasks.append(task)
        return self.result


def _seg(y_top, y_bottom, seg_type="media", content=""):
    return {"factor": "头像", "type": seg_type, "content": content,
            "y_top": y_top, "y_bottom": y_bottom,
            "avatar_cand": "张三", "nickname": "张三",
            "avatar_score": 0.9, "nick_score": 0.9}


class InlineMediaTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="inline_media_")
        self.conn = ml.connect(os.path.join(self._dir, "chatlog.db"))
        self.dev = FakeDev()
        self.group = "内联测试群"

        patchers = [
            mock.patch.object(
                hc, "_classify_media_seg",
                side_effect=lambda img, sg: ("link", "[链接]", 0xABCD)),
            mock.patch(
                "src.interaction.loop.cutline_segment.segment_cutlines"),
            mock.patch(
                "src.interaction.ports.android.perception.chat_slicer"
                ".slice_chat", return_value={"messages": []}),
            mock.patch(
                "src.interaction.ports.android.perception.ocr_engine.run_ocr",
                return_value=[{"text": "内联测试群", "cy": 100}]),
            mock.patch(
                "src.interaction.ports.android.perception.page_detector"
                ".detect_pinned_bar_end", return_value=None),
            mock.patch(
                "src.interaction.ports.android.perception.page_detector"
                ".detect_input_bar_top", return_value=None),
            mock.patch.object(
                hc.RS, "save_crop",
                side_effect=lambda img, g, y0, y1, k:
                f"crops/t/{abs(hash(k))}.jpg"),
            mock.patch.object(hc.RS, "do_swipe", return_value=None),
            mock.patch("time.sleep", lambda *_a, **_k: None),
        ]
        started = [p.start() for p in patchers]
        self.addCleanup(lambda: [p.stop() for p in patchers])
        self.mock_segs = started[1]
        self.mock_ocr = started[3]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _run(self, handler=None, media_max=5):
        return hc.collect_group_history(
            self.dev, self.conn, self.group, max_rounds=1,
            stop_at_anchor=False, handle_media=True,
            media_max=media_max, media_timeout_s=60, media_handler=handler)

    def _rows(self):
        sid = ml.get_or_create_session(self.conn, self.group, True)
        return self.conn.execute(
            "SELECT content_type, content, media_status FROM messages"
            " WHERE session_id=?", (sid,)).fetchall()

    def test_visible_link_handled_inline(self):
        self.mock_segs.return_value = [_seg(400, 700)]
        handler = FakeHandler(MediaResult(
            msg_id="", msg_type="link", content="https://example.com/x",
            success=True))
        n = self._run(handler=handler)
        self.assertEqual(n, 1)
        self.assertEqual(len(handler.tasks), 1)
        self.assertEqual(handler.tasks[0].msg_type, "card")  # link→card 粗映射
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "[链接] https://example.com/x")
        self.assertEqual(rows[0]["media_status"], "done")

    def test_partial_media_not_collected(self):
        # y_bottom=2300 超出可见区（输入栏顶 2110-20）：半显段不入库不处置
        self.mock_segs.return_value = [_seg(1900, 2300)]
        handler = FakeHandler(MediaResult(
            msg_id="", msg_type="link", content="https://example.com/x",
            success=True))
        n = self._run(handler=handler)
        self.assertEqual(n, 0)
        self.assertEqual(len(handler.tasks), 0)
        self.assertEqual(len(self._rows()), 0)

    def test_red_packet_marked_done_without_click(self):
        self.mock_segs.return_value = [_seg(400, 700)]
        with mock.patch.object(hc, "_classify_media_seg",
                               side_effect=lambda img, sg:
                               ("red_packet", "[红包]", 0xABCD)):
            handler = FakeHandler(MediaResult(
                msg_id="", msg_type="red_packet", content="", success=True))
            n = self._run(handler=handler)
        self.assertEqual(n, 1)
        self.assertEqual(len(handler.tasks), 0)          # 红包不点击
        rows = self._rows()
        self.assertEqual(rows[0]["media_status"], "done")

    def test_aborts_when_not_in_chat(self):
        """采集前校验：当前页标题不含群名 → 放弃采集（防首页列表被当聊天采，
        2026-09-01 事故：首页联系人头像被采成 3 条假 [图片]）。"""
        self.mock_ocr.return_value = [{"text": "微信", "cy": 100}]
        self.mock_segs.return_value = [_seg(400, 700)]
        handler = FakeHandler(MediaResult(
            msg_id="", msg_type="link", content="https://example.com/x",
            success=True))
        n = self._run(handler=handler)
        self.assertEqual(n, 0)
        self.assertEqual(len(self._rows()), 0)
        self.assertEqual(len(handler.tasks), 0)

    def test_short_text_full_classify_against_ocr_hallucination(self):
        """≤3 字短文本必须走全量分类（防 OCR 幻读），不走卡片嫌疑复核。
        2026-09-02 回归事故：嫌疑复核替换短文本守卫后，风图发的条形图
        被 OCR 幻读成 "T T" 存成文本 → 图片丢库 → agent 没有图可看，
        拿窗口里旧链接的内容回答了「看这个图片里面是什么」。"""
        seg = _seg(400, 700, seg_type="text", content="T")
        self.mock_segs.return_value = [seg]
        handler = FakeHandler(MediaResult(
            msg_id="", msg_type="image", content="", success=True))
        with mock.patch.object(
                hc, "_classify_media_seg",
                side_effect=lambda img, sg: ("image", "[图片]", 0xABCD)), \
             mock.patch.object(
                hc, "_classify_text_suspect",
                side_effect=AssertionError("短文本不应走嫌疑复核")):
            n = self._run(handler=handler)
        self.assertEqual(n, 1)
        rows = self.conn.execute(
            "SELECT content_type, content FROM messages").fetchall()
        self.assertEqual(rows[0]["content_type"], "image")
        self.assertEqual(rows[0]["content"], "[图片]")

    def test_bottom_clipped_card_typed_as_link(self):
        """底部截断段也过卡片嫌疑复核（2026-09-02：最新消息是链接卡时
        被输入栏截断 → bottom_clipped 分支存成 text，截断文本与完整卡
        fuzzy 比不中 → 完整版永远不再采，URL 永久丢）。
        截断卡不当屏点击，标对类型留 media_status='' 给后续处置。"""
        msg = {"content_type": "text", "content": "杂谈 05 | 手撕一款颈部基模",
               "side": "other", "nickname": "风图", "y": 1900,
               "partial_bottom": True}
        with mock.patch(
                "src.interaction.ports.android.perception.chat_slicer"
                ".slice_chat", return_value={"messages": [msg]}), \
             mock.patch(
                "src.interaction.ports.android.perception.chat_slicer"
                ".classify_message",
                return_value={"state": "bottom_clipped", "y_top": 1900,
                              "y_bottom": 2320}), \
             mock.patch.object(hc, "_classify_text_suspect",
                               return_value=("link", 0xABCD)):
            self.mock_segs.return_value = []   # 裁切线分段失败 → 回退 slice_chat 路径
            n = self._run()
        self.assertEqual(n, 1)
        rows = self.conn.execute(
            "SELECT content_type, content, media_status FROM messages"
            ).fetchall()
        self.assertEqual(rows[0]["content_type"], "link")
        self.assertEqual(rows[0]["content"], "[链接]")
        self.assertEqual(rows[0]["media_status"], "")

    def test_own_segment_marked_mine_and_deduped(self):
        """factor="自己" 的段（右侧气泡）必须落成 sender="我"/is_mine=1。
        2026-09-01 事故：cutline 路径硬编码 is_mine=False，自己的回复无
        头像/昵称因子 → sender="" 匿名入库；查重先比 sender，"我"对""
        永远不等，journey 重扫把自己的回复当新消息再记一遍。"""
        seg = _seg(400, 700, seg_type="text", content="自己的回复喵")
        seg["factor"] = "自己"
        seg["avatar_cand"] = ""
        seg["nickname"] = ""
        self.mock_segs.return_value = [seg]
        n = self._run()
        self.assertEqual(n, 1)
        rows = self.conn.execute(
            "SELECT sender, is_mine FROM messages").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sender"], "我")
        self.assertEqual(rows[0]["is_mine"], 1)
        # 二次重扫同屏：sender 对齐后锚定命中，不再重复入库
        n2 = self._run()
        self.assertEqual(n2, 0)
        cnt = self.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()
        self.assertEqual(cnt["c"], 1)

    def test_budget_zero_leaves_pending(self):
        self.mock_segs.return_value = [_seg(400, 700)]
        handler = FakeHandler(MediaResult(
            msg_id="", msg_type="link", content="https://example.com/x",
            success=True))
        n = self._run(handler=handler, media_max=0)
        self.assertEqual(n, 1)                           # 仍入库
        self.assertEqual(len(handler.tasks), 0)          # 但不处置
        rows = self._rows()
        self.assertEqual(rows[0]["media_status"], "")    # 留给 media_pass 兜底


if __name__ == "__main__":
    unittest.main()
