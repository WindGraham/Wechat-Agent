# -*- coding: utf-8 -*-
"""test_home_parser.py — 首页解析器离线单测（合成角标几何，不触真机）。

覆盖（2026-08-10 摸鱼酱幻影未读死循环事故）：
- 头像里的大块红色内容（橙红帽 130x81/area3261）不得误判为数字角标
- 真红点/真数字圈的尺寸窗口不受影响
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.interaction.ports.android.perception.home_parser import _item_badge
from src.interaction.ports.android.perception import layout_consts as LC

ROW_Y0 = 200          # 虚构行顶
ZONE_CX = 150         # 角标区中心 x（BADGE_ZONE_X0..X1 = 100..200）


def blob(w, h, area, cy=None):
    """构造 (x, y, w, h, area) 连通域，中心落在角标区 + 行窗口内。"""
    cx = ZONE_CX
    cy = cy if cy is not None else ROW_Y0 + 40
    return (int(cx - w / 2), int(cy - h / 2), w, h, area)


class TestItemBadgeSizeGuard(unittest.TestCase):
    def test_oversized_red_blob_rejected(self):
        """头像大块红色（橙红帽）：不得当数字角标（幻影未读根因）。"""
        badges = [blob(130, 81, 3261)]
        unread, kind = _item_badge(None, [], badges, ROW_Y0)
        self.assertEqual((unread, kind), (0, None))

    def test_oversized_by_width_only(self):
        badges = [blob(120, 40, 1800)]     # 面积正常但超宽（头像红围巾类）
        unread, kind = _item_badge(None, [], badges, ROW_Y0)
        self.assertEqual((unread, kind), (0, None))

    def test_real_dot_still_detected(self):
        badges = [blob(27, 27, 570)]       # 标定红点 ≈27x27 area~570
        unread, kind = _item_badge(None, [], badges, ROW_Y0)
        self.assertEqual((unread, kind), (-1, "dot"))

    def test_tiny_noise_ignored(self):
        badges = [blob(10, 10, 90)]
        unread, kind = _item_badge(None, [], badges, ROW_Y0)
        self.assertEqual((unread, kind), (0, None))

    def test_real_number_badge_with_digit(self):
        """真数字圈（w68 area2400）+ OCR 命中数字 → 正常读出未读数。"""
        badges = [blob(68, 44, 2400)]
        ocr = [{"text": "3", "cx": ZONE_CX, "cy": ROW_Y0 + 40}]
        unread, kind = _item_badge(None, ocr, badges, ROW_Y0)
        self.assertEqual((unread, kind), (3, "number"))


if __name__ == "__main__":
    unittest.main()
