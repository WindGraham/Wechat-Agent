# -*- coding: utf-8 -*-
"""tests/test_roster_matcher_flow.py — 验证双因子匹配与花名册打标控制模块"""

import json
import os
import shutil
import tempfile
import cv2
import numpy as np

from src.interaction.ports.android.perception.roster_matcher import (
    RosterMatcher, check_group_manifest, update_group_manifest, ROSTERS_DIR, MANIFEST_PATH
)

def test_manifest_and_matcher():
    # 测试 manifest
    update_group_manifest("测试群聊", "completed", 50)
    assert check_group_manifest("测试群聊") is True
    print("✅ Manifest 打标校验成功")

    # 测试已有“交流一下？”群聊
    rm = RosterMatcher("交流一下？")
    assert len(rm.member_profiles) > 0
    assert len(rm.avatar_templates) > 0
    print(f"✅ 花名册加载成功，成员数: {len(rm.member_profiles)}，模板数: {len(rm.avatar_templates)}")

    # 模拟双因子匹配
    tmpl = rm.avatar_templates.get("JY君")
    if tmpl:
        crop_img = tmpl["bgr"].copy()
        # 正确 OCR
        ok, name, info = rm.match_dual_factor(crop_img, "JY")
        assert ok is True
        assert name == "JY君"
        print("✅ 双因子正向校验通过 (正确头像 + 正确OCR)")

        # 错误 OCR
        ok_err, _, _ = rm.match_dual_factor(crop_img, "未知路人昵称")
        assert ok_err is False
        print("✅ 双因子反向校验通过 (正确头像 + 错误OCR -> 拒绝匹配)")

if __name__ == "__main__":
    test_manifest_and_matcher()
    print("\n所有测试全量通过！")
