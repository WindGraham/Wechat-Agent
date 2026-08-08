# -*- coding: utf-8 -*-
"""test_contracts.py — 层间契约类型（src/shared/types.py）的契约测试。

校验（蓝本为 docs/CONTRACTS.md）：
1. 所有 dataclass 可实例化（必填字段给最小样例值）
2. dataclasses.asdict 后可 JSON 序列化
3. 字段集合与 CONTRACTS.md 描述一致（关键字段抽查）
"""

import dataclasses
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACTS_MD = os.path.join(PROJECT_ROOT, "docs", "CONTRACTS.md")

# 每个契约类型的最小实例化参数（只列无默认值的必填字段）
SAMPLES = {
    "Message": dict(session="特高课", is_group=True, sender="风图",
                    is_mine=False, content="在吗", content_type="text"),
    "LogUpdated": dict(session="特高课", version=1),
    "ActionResult": dict(ok=True),
    "ActionBundle": dict(session="特高课",
                         blocks_xml="<reply><text>在的</text></reply>"),
    "TaskBrief": dict(goal="整理本周记录生成 PDF"),
    "TaskResult": dict(ok=True),
    "QueueEntry": dict(kind="notify", session="特高课",
                       sources={"sweep", "notify"}),
}

# CONTRACTS.md 各节描述的关键字段（抽查）
EXPECTED_FIELDS = {
    "Message": {"session", "is_group", "sender", "is_mine", "content",
                "content_type", "mentions", "media_path", "ts", "seq",
                "msg_uid"},
    "LogUpdated": {"session", "version", "mention_hint"},
    "ActionResult": {"ok", "error", "retryable", "escalation_hint"},
    "ActionBundle": {"session", "blocks_xml", "ref"},
    "TaskBrief": {"goal", "context", "tried", "deliver"},
    "TaskResult": {"ok", "summary", "artifacts", "say_to_user",
                   "cli_session_id", "trace_path"},
    "QueueEntry": {"kind", "session", "ts", "mention", "payload",
                   "attempts", "sources"},
}


def _json_default(o):
    """QueueEntry.sources 是 set（CONTRACTS §四），JSON 时归一化为排序 list；
    其余未知类型仍抛 TypeError（保持严格）。"""
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"Object of type {o.__class__.__name__} "
                    f"is not JSON serializable")


class ContractsTest(unittest.TestCase):
    def test_all_expected_types_exist(self):
        for name in SAMPLES:
            cls = getattr(types, name, None)
            self.assertIsNotNone(cls, f"types.py 缺少契约类型 {name}")
            self.assertTrue(dataclasses.is_dataclass(cls),
                            f"{name} 不是 dataclass")

    def test_instantiable(self):
        for name, kwargs in SAMPLES.items():
            cls = getattr(types, name)
            try:
                obj = cls(**kwargs)
            except TypeError as e:
                self.fail(f"{name} 实例化失败: {e}")
            self.assertIsNotNone(obj)

    def test_json_serializable(self):
        for name, kwargs in SAMPLES.items():
            obj = getattr(types, name)(**kwargs)
            try:
                text = json.dumps(dataclasses.asdict(obj),
                                  ensure_ascii=False, default=_json_default)
            except (TypeError, ValueError) as e:
                self.fail(f"{name} asdict 后不可 JSON 序列化: {e}")
            # 往返一次，确认是纯 JSON 值
            json.loads(text)

    def test_fields_match_contracts_doc(self):
        """字段集合与 CONTRACTS.md 描述一致（EXPECTED_FIELDS 抽查）。"""
        for name, expected in EXPECTED_FIELDS.items():
            actual = {f.name for f in dataclasses.fields(
                getattr(types, name))}
            missing = expected - actual
            self.assertFalse(missing,
                             f"{name} 缺少 CONTRACTS.md 描述的字段: {missing}")

    def test_doc_mentions_all_types(self):
        """CONTRACTS.md 必须定义所有契约类型（文档先行原则）。"""
        with open(CONTRACTS_MD, encoding="utf-8") as f:
            doc = f.read()
        for name in SAMPLES:
            self.assertIn(name + ":", doc,
                          f"CONTRACTS.md 未定义契约类型 {name}")

    def test_message_defaults(self):
        """Message 默认值兜底（CONTRACTS §一：mentions 可空、media_path 可 None）。"""
        m = types.Message(session="s", is_group=False, sender="我",
                          is_mine=True, content="hi", content_type="text")
        self.assertEqual(m.mentions, [])
        self.assertIsNone(m.media_path)
        self.assertEqual(m.ts, 0.0)
        self.assertEqual(m.seq, 0)
        self.assertEqual(m.msg_uid, "")

    def test_action_bundle_ref_optional(self):
        """ActionBundle.ref 可为 None（CONTRACTS §二）。"""
        b = types.ActionBundle(session="s", blocks_xml="<silent/>")
        self.assertIsNone(b.ref)
        b2 = types.ActionBundle(session="s", blocks_xml="<silent/>", ref="m3")
        self.assertEqual(b2.ref, "m3")


if __name__ == "__main__":
    unittest.main()
