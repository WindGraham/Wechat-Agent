# -*- coding: utf-8 -*-
"""决策层离线单测：xml_blocks / policy / prompt / proxy（全假对象，离线）。"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.shared.types import Message, LogUpdated                    # noqa: E402
from src.shared.xml_blocks import extract_blocks, parse_attrs       # noqa: E402
from src.decision.policy import Policy, RepliedMentionStore         # noqa: E402
from src.decision.prompt import ContextBuilder                      # noqa: E402
from src.decision.proxy import Proxy                                # noqa: E402


def _msg(sender="Leisure", content="你好", seq=1, **kw):
    return Message(session="特高课", is_group=True, sender=sender,
                   is_mine=False, content=content, content_type="text",
                   seq=seq, **kw)


# ---------------------------------------------------------------- XML 扫描
class XmlBlocksTest(unittest.TestCase):

    def test_normal_blocks(self):
        out = ('<reply session="特高课" ref="m1"><text>好</text></reply>'
               '<silent/>')
        blocks = extract_blocks(out)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].tag, "reply")
        self.assertEqual(parse_attrs(blocks[0].attrs)["session"], "特高课")
        self.assertTrue(blocks[1].self_closing)

    def test_bad_block_isolated(self):
        out = ('<reply><text>坏块没闭合'
               '<reply><text>好块</text></reply>')
        blocks = extract_blocks(out)
        valid = [b for b in blocks if b.valid]
        self.assertEqual(len(valid), 1)
        self.assertIn("好块", valid[0].inner)

    def test_unclosed_tail_dropped(self):
        blocks = extract_blocks('<reply><text>好</text></reply><task>没闭合')
        self.assertEqual(len([b for b in blocks if b.valid]), 1)

    def test_unescape(self):
        blocks = extract_blocks("<reply><text>1 &lt; 2 &amp;&amp; 3</text></reply>")
        self.assertIn("1 < 2 && 3", blocks[0].inner)


# ---------------------------------------------------------------- Policy
class PolicyTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.policy = Policy(
            owner="风图", owner_nick="陈曦",
            store=RepliedMentionStore(
                path=os.path.join(self._tmp.name, "rm.json")))

    def tearDown(self):
        self._tmp.cleanup()

    def test_at_me(self):
        self.assertTrue(self.policy.is_at_me(
            _msg(content="@陈曦 在吗", mentions=["陈曦"])))
        self.assertTrue(self.policy.is_at_me(
            _msg(content="x", mentions=["陈曦你可以调取本机的"])))   # OCR 粘连
        self.assertTrue(self.policy.is_at_me(_msg(content="@所有人 开会")))
        self.assertFalse(self.policy.is_at_me(
            _msg(content="@光阴 你好", mentions=["光阴"])))

    def test_must_reply(self):
        self.assertTrue(self.policy.must_reply("风图", False, [_msg()]))
        self.assertTrue(self.policy.must_reply(
            "特高课", True, [_msg(sender="风图")]))
        self.assertTrue(self.policy.must_reply(
            "特高课", True, [_msg(mentions=["陈曦"])]))
        self.assertFalse(self.policy.must_reply("特高课", True, [_msg()]))

    def test_unreplied_mentions_dedup(self):
        m = _msg(content="@陈曦 在吗", mentions=["陈曦"])
        self.assertEqual(len(self.policy.unreplied_mentions("特高课", [m])), 1)
        self.policy.mark_replied("特高课", m)
        self.assertEqual(self.policy.unreplied_mentions("特高课", [m]), [])


# ---------------------------------------------------------------- Prompt
class PromptTest(unittest.TestCase):

    def test_build_structure(self):
        b = ContextBuilder(owner="风图")
        msgs = b.build("特高课", True, "新消息（1 条）",
                       [_msg(content="旧消息")], [_msg(content="@陈曦 新消息")])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        user = msgs[1]["content"]
        self.assertIn("特高课", user)
        self.assertIn("m1 Leisure: @陈曦 新消息", user)
        self.assertIn("旧消息", user)
        self.assertIn("输出协议", msgs[0]["content"])

    def test_task_receipt(self):
        b = ContextBuilder(owner="风图")
        msgs = b.build_task_receipt("特高课", True,
                                    {"task_id": "t1", "ref": "m3",
                                     "ref_brief": "整理", "desc": "d",
                                     "result": "成功"},
                                    [_msg()])
        self.assertIn("任务回执", msgs[1]["content"])
        self.assertIn("t1", msgs[1]["content"])


# ---------------------------------------------------------------- Proxy
class _FakeProvider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def chat(self, messages, max_tokens=300, temperature=0.8):
        self.calls += 1
        return self.outputs.pop(0) if self.outputs else "<silent/>"

    def vision_file(self, path, prompt):
        return "一只猫"


class _FakeReader:
    def __init__(self, msgs):
        self._msgs = msgs
        self.writes = []

    def get_new_since(self, session, last_seq):
        return [m for m in self._msgs if m.seq > last_seq]

    def get_context(self, session, n=200):
        return self._msgs[-n:]

    def last_is_group(self, session):
        return True

    def update_content(self, session, sender, content, new_content):
        self.writes.append((session, sender, new_content))
        return 1


class _FakeRuntime:
    def __init__(self, **kw):
        self._d = {"max_concurrent_decisions": 1, "history_size": 50,
                   "media_convert_concurrency": 2, "owner": "风图",
                   "owner_nick": "陈曦", "tool_model": "kimi-code/k3",
                   "paused": False}
        self._d.update(kw)

    def get(self, k, d=None):
        return self._d.get(k, d)


class _FakeResult:
    ok = True
    error = None
    retryable = True
    escalation_hint = None


class ProxyTest(unittest.TestCase):

    def _proxy(self, outputs, msgs, submitted):
        provider = _FakeProvider(outputs)
        reader = _FakeReader(msgs)
        tmp = tempfile.mkdtemp()
        proxy = Proxy(provider=provider, reader=reader,
                      submit_bundle=lambda s, x: (submitted.append((s, x)),
                                                  _FakeResult())[-1],
                      runtime=_FakeRuntime(),
                      watermarks_path=os.path.join(tmp, "wm.json"),
                      tasks_root=os.path.join(tmp, "tasks"))
        return proxy, provider

    def test_reply_routed_to_bundle(self):
        submitted = []
        proxy, _ = self._proxy(
            ['<reply session="特高课" ref="m1"><text>收到</text></reply>'],
            [_msg(content="@陈曦 在吗", mentions=["陈曦"], seq=1)],
            submitted)
        proxy.notify_log_updated(LogUpdated(session="特高课", version=1,
                                            mention_hint=True))
        self.assertTrue(proxy.run_once())
        self.assertEqual(len(submitted), 1)
        self.assertIn("收到", submitted[0][1])

    def test_silent_no_bundle(self):
        submitted = []
        proxy, _ = self._proxy(["<silent/>"], [_msg(seq=1)], submitted)
        proxy.notify_log_updated(LogUpdated(session="特高课", version=1))
        proxy.run_once()
        self.assertEqual(submitted, [])

    def test_must_reply_fallback(self):
        """@我 但模型持续沉默 → 重试一次 → 兜底 bundle。"""
        submitted = []
        proxy, provider = self._proxy(
            ["<silent/>", "<silent/>"],
            [_msg(content="@陈曦 在吗", mentions=["陈曦"], seq=1)],
            submitted)
        proxy.notify_log_updated(LogUpdated(session="特高课", version=1,
                                            mention_hint=True))
        proxy.run_once()
        self.assertEqual(provider.calls, 2)          # 重试了一次
        self.assertEqual(len(submitted), 1)
        self.assertIn("在的", submitted[0][1])       # 兜底话术

    def test_media_conversion_before_prompt(self):
        """未标注多媒体先转换再决策。"""
        submitted = []
        m = _msg(content="[图片]待识别", seq=1)
        m.content_type = "multimedia"
        m.media_path = __file__           # 路径存在即可（假 provider 不看内容）
        proxy, _ = self._proxy(["<silent/>"], [m], submitted)
        proxy.notify_log_updated(LogUpdated(session="特高课", version=1))
        proxy.run_once()
        self.assertTrue(proxy._reader.writes)            # 写回了
        self.assertIn("一只猫", proxy._reader.writes[0][2])

    def test_watermark_no_reprocess(self):
        """同一批消息不会重复决策。"""
        submitted = []
        proxy, provider = self._proxy(["<silent/>"], [_msg(seq=1)], submitted)
        ev = LogUpdated(session="特高课", version=1)
        proxy.notify_log_updated(ev)
        proxy.run_once()
        proxy.notify_log_updated(LogUpdated(session="特高课", version=2))
        proxy.run_once()
        self.assertEqual(provider.calls, 1)              # 第二次无新消息直接跳过


if __name__ == "__main__":
    unittest.main()
