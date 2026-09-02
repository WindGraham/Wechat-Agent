# -*- coding: utf-8 -*-
"""test_media_classifier.py — classify_segment 真图回归（workspace/crops 留档）。

2026-08-27 深色链接卡被误判 text（seq65 事故）后新增 embedded_thumb
检测；2026-09-01 全量 crops 回归又修掉 4 个误检（深色木纹照片/
特朗普表情包被当成"卡片缩略图"）。本文件把这些真图固化为回归用例。

图片在 workspace/crops/（gitignore，本地留档），缺失时整套 skip。
"""

import os
import unittest

import cv2

from src.interaction.ports.android.perception.media_classifier import (
    classify_segment)

CROPS = "workspace/crops"

# 真卡片（深色主题链接/视频/音乐卡）→ 必须判 card/embedded_thumb
CARDS = [
    "交流一下_2026秋_/2a7cb09d39933a3a.jpg",   # AI寒武纪公众号卡（事故原图）
    "特高课/084048e36b84e3de.jpg",             # 财闻公众号卡
    "218/012b01d532ff5687.jpg",                # 哔哩哔哩视频卡
    "YOUSAOBI/1352195c921671f2.jpg",           # 哔哩哔哩音乐卡
    "陈曦猫猫群/2d5ef49c478cbee6.jpg",         # 知乎卡
    "陈曦猫猫群/4f8a6ebf476690cb.jpg",         # 新智元苹果换帅卡（2026-09-02 漏检）
    "陈曦猫猫群/9c3e1956151e7837.jpg",         # 无文册睡眠障碍卡（深色缩略图贴脸灰背景）
]

# 曾误检为 embedded_thumb 的非卡片 → 必须不判 card。
# 已知遗留边界：这些深色纹理图（木纹/深色西装）仍会落 text——
# 伪气泡与真短文本气泡无法用纯形状区分，生产侧有 OCR 文本兜底，
# 暂不为此加面积/填充率门槛（真一字气泡也很小，误伤更亏）。
NOT_CARDS = [
    "交流一下_/06b7bd79f655f0ee.jpg",          # 华为水杯照片（深色木纹伪气泡）
    "陈曦猫猫群/61f3612f9b233f74.jpg",         # 特朗普表情包（动图帧）
    "陈曦猫猫群/6b91ad967deb9134.jpg",         # 同上
    "陈曦猫猫群/76ec5774c03406cc.jpg",         # 同上
]


@unittest.skipUnless(os.path.isdir(CROPS), "workspace/crops 留档不存在")
class ClassifySegmentRealCropsTest(unittest.TestCase):
    def _classify(self, rel):
        img = cv2.imread(os.path.join(CROPS, rel))
        if img is None:
            self.skipTest(f"缺图 {rel}")
        return classify_segment(img)

    def test_real_cards_detected(self):
        for rel in CARDS:
            label, detail = self._classify(rel)
            self.assertEqual(label, "card", rel)
            self.assertEqual(detail.get("reason"), "embedded_thumb", rel)

    def test_false_positives_not_cards(self):
        for rel in NOT_CARDS:
            label, detail = self._classify(rel)
            self.assertNotEqual(
                (label, detail.get("reason")), ("card", "embedded_thumb"), rel)

    def test_green_self_bubble_is_text(self):
        """自己绿气泡+右侧头像不得判媒体（2026-09-02 修复后纠正：
        该裁图曾被旧分类器误判 media 入库成 [图片]）。"""
        label, _ = self._classify("陈曦猫猫群/39d567855fe8655c.jpg")
        self.assertEqual(label, "text")


# 文本段「卡片嫌疑复核」：伪装成文本的卡片必须抓出，正常文本必须维持
SUSPECT_CARDS = [
    "陈曦猫猫群/4f8a6ebf476690cb.jpg",   # 新智元链接卡（两行标题）
    "陈曦猫猫群/9c3e1956151e7837.jpg",   # 无文册链接卡（深色缩略图贴脸灰背景）
]
SUSPECT_TEXTS = [
    "交流一下_2026秋_/ccef6749bc289bf6.jpg",  # 「26条新消息」胶囊压气泡
    "交流一下_2026秋_/9e5e7fd3b6aa1646.jpg",  # 「有人@我」胶囊
    "特高课/7319867342613528.jpg",            # 「有人@我」胶囊
    "特高课/d09a7f712f439fc8.jpg",            # 15 行长文本+胶囊粘连 bg 块
    "陈曦猫猫群/9d79ca57aa40c8af.jpg",        # 自己绿气泡+右侧头像
    "陈曦猫猫群/bd585c511f255ece.jpg",        # 同上
]


@unittest.skipUnless(os.path.isdir(CROPS), "workspace/crops 留档不存在")
class ClassifyTextSuspectTest(unittest.TestCase):
    def _suspect(self, rel, ocr=""):
        from src.interaction.ports.android.perception.media_classifier import (
            classify_text_suspect)
        img = cv2.imread(os.path.join(CROPS, rel))
        if img is None:
            self.skipTest(f"缺图 {rel}")
        return classify_text_suspect(img, ocr)

    def test_disguised_cards_caught(self):
        for rel in SUSPECT_CARDS:
            label, _ = self._suspect(rel)
            self.assertEqual(label, "card", rel)

    def test_normal_texts_stay_text(self):
        for rel in SUSPECT_TEXTS:
            label, _ = self._suspect(rel)
            self.assertEqual(label, "text", rel)


if __name__ == "__main__":
    unittest.main()
