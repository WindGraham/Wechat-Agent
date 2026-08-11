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

    def test_quote_rendering(self):
        """quote 消息渲染：被引用段用 [引用「…」] 标记，AI 可分辨引用与正文。"""
        q = Message(session="特高课", is_group=True, sender="Leisure",
                    is_mine=False,
                    content="Leisure：被引用的旧消息\n这是新回复",
                    content_type="quote", seq=2)
        out = ContextBuilder.render_new_messages([q])
        self.assertIn('[引用「Leisure：被引用的旧消息」]', out)
        self.assertIn("这是新回复", out)
        # 普通 text 消息不带引用标记
        t = Message(session="特高课", is_group=True, sender="Leisure",
                    is_mine=False, content="普通消息", content_type="text",
                    seq=3)
        out2 = ContextBuilder.render_new_messages([t])
        self.assertNotIn("[引用「", out2)
        self.assertIn("普通消息", out2)

    def test_known_sessions_block(self):
        """2026-08-10 发错群事故：注入已知会话名单，供跨会话投递照抄名称。"""
        b = ContextBuilder(owner="风图")
        msgs = b.build("陈曦猫猫群", True, "新消息（1 条）",
                       [_msg(content="旧")], [_msg(content="去交流一下群发言")],
                       known_sessions=[("陈曦猫猫群", True),
                                       ("交流一下？", True),
                                       ("风图", False)])
        user = msgs[1]["content"]
        self.assertIn("已知会话", user)
        self.assertIn("交流一下？（群聊）", user)
        self.assertIn("风图（私聊）", user)
        # 不传名单时不出现该板块
        msgs2 = b.build("陈曦猫猫群", True, "新消息（1 条）",
                        [_msg(content="旧")], [_msg(content="hi")])
        self.assertNotIn("已知会话", msgs2[1]["content"])


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

    def test_cross_session_reply(self):
        """跨会话投递：reply 块 session 属性指向另一会话，消息发到目标会话。"""
        submitted = []
        proxy, _ = self._proxy(
            ['<reply session="目标群" ref="m1"><text>结果发这里</text></reply>'],
            [_msg(content="@陈曦 在吗", mentions=["陈曦"], seq=1)],
            submitted)
        proxy.notify_log_updated(LogUpdated(session="特高课", version=1,
                                            mention_hint=True))
        self.assertTrue(proxy.run_once())
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0][0], "目标群")      # 发到块指定的会话
        self.assertIn("结果发这里", submitted[0][1])

    def test_cross_session_multiple_replies(self):
        """一轮多个 reply 分发给不同会话。"""
        submitted = []
        proxy, _ = self._proxy(
            ['<reply session="群A"><text>给A</text></reply>'
             '<reply session="群B"><text>给B</text></reply>'],
            [_msg(content="@陈曦 在吗", mentions=["陈曦"], seq=1)],
            submitted)
        proxy.notify_log_updated(LogUpdated(session="特高课", version=1,
                                            mention_hint=True))
        self.assertTrue(proxy.run_once())
        self.assertEqual([s for s, _ in submitted], ["群A", "群B"])

    def test_reply_defaults_to_current_session(self):
        """reply 块无 session 属性：回落当前决策会话（不破坏原有行为）。"""
        submitted = []
        proxy, _ = self._proxy(
            ['<reply ref="m1"><text>收到</text></reply>'],
            [_msg(content="@陈曦 在吗", mentions=["陈曦"], seq=1)],
            submitted)
        proxy.notify_log_updated(LogUpdated(session="特高课", version=1,
                                            mention_hint=True))
        self.assertTrue(proxy.run_once())
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0][0], "特高课")      # 回落当前会话

    def test_aside_injection(self):
        """旁注：inject_aside 触发带注入消息的决策，LLM 能看到该消息。"""
        submitted = []
        proxy, provider = self._proxy(
            ['<reply><text>收到旁注</text></reply>'],
            [], submitted)
        self.assertTrue(proxy.inject_aside("特高课", "这条是旁注"))
        self.assertTrue(proxy.run_once())
        self.assertEqual(len(submitted), 1)
        self.assertIn("收到旁注", submitted[0][1])
        # 旁注文本出现在给 LLM 的 user prompt 里
        self.assertIn("旁注", str(provider.outputs)) if provider.outputs else None

    def test_aside_empty_ignored(self):
        """空旁注忽略。"""
        proxy, _ = self._proxy(["<silent/>"], [], [])
        self.assertFalse(proxy.inject_aside("特高课", "   "))

    def test_aside_prioritized(self):
        """旁注优先级最高（owner 级），普通事件之后也能插队处理。"""
        submitted = []
        proxy, provider = self._proxy(
            ['<reply><text>旁注先处理</text></reply>'], [], submitted)
        # 先塞一个普通 log_updated，再塞旁注
        proxy.notify_log_updated(LogUpdated(session="特高课", version=1))
        proxy.inject_aside("特高课", "高优先级旁注")
        self.assertTrue(proxy.run_once())
        self.assertIn("旁注先处理", submitted[0][1])

    def test_receipt_cross_session_reply(self):
        """任务回执路径也支持跨会话：回执里 reply 带 session 属性时
        发到块指定会话（回归：2026-08-10 简历发给 canglang 却落到
        风图——回执路径漏了跨会话，硬编码当前会话）。"""
        submitted = []
        proxy, provider = self._proxy(
            ['<reply session="canglang"><text>简历发你</text>'
             '<file path="/x/简历.pdf"/></reply>'
             '<reply session="风图"><text>办妥啦</text></reply>'],
            [], submitted)
        # 注册任务（不跑后台，直接注入 task_done）
        task = proxy._ledger.register(
            session="风图", refs=["m1"], ref_briefs=[],
            desc="发简历给canglang", deliver="reply+file")
        workdir = task["workdir"]
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, "result.txt"), "w",
                  encoding="utf-8") as f:
            f.write("DELIVERABLE:/x/简历.pdf\n成功。")
        proxy.inject_task_done(task["task_id"])
        self.assertTrue(proxy.run_once())
        # 两个 reply 应分别发到 canglang 和 风图
        sessions = [s for s, _ in submitted]
        self.assertIn("canglang", sessions)
        self.assertIn("风图", sessions)
        self.assertEqual(sessions[0], "canglang")        # 第一个块发 canglang
        self.assertIn("简历发你", submitted[0][1])

    def test_set_provider_hot_swap(self):
        """网关模型热切换（2026-08-11）：set_provider 后决策立即用新
        provider，旧 provider 不再被调用；provider_info 反映实况。"""
        submitted = []
        proxy, old = self._proxy(['<silent/>'], [_msg(content="你好")],
                                 submitted)
        new = _FakeProvider(['<reply><text>新模型回的</text></reply>'])
        new.model = "deepseek-v4-flash"
        new._url = "https://api.deepseek.com/chat/completions"
        new._token_floor = 256
        new._token_ceiling = 0
        proxy.set_provider(new)
        info = proxy.provider_info()
        self.assertEqual(info["model"], "deepseek-v4-flash")
        self.assertEqual(info["token_floor"], 256)
        proxy.notify_log_updated(LogUpdated(session="特高课", version=1))
        proxy.run_once()
        self.assertEqual(new.calls, 1)
        self.assertEqual(old.calls, 0)
        self.assertIn("新模型回的", submitted[0][1])


# ---------------------------------------------------------------- Provider
class ProviderTest(unittest.TestCase):
    """决策层模型可配置（2026-08-11）：runtime.json 的
    decision_provider/decision_model 透传 create_provider，支持
    deepseek v4-flash/v4-pro（1M 上下文，always_thinking）一键切换。"""

    def _env_file(self, text):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        f.write(text)
        f.close()
        return f.name

    def test_factory_deepseek_v4(self):
        from src.decision.provider import create_provider
        from src.decision.provider.base import DeepSeekProvider
        path = self._env_file("DEEPSEEK_API_KEY=test-key-123\n")
        p = create_provider(prefer="deepseek", model="deepseek-v4-flash",
                            env_path=path)
        self.assertIsInstance(p, DeepSeekProvider)
        self.assertEqual(p.model, "deepseek-v4-flash")
        self.assertIn("api.deepseek.com", p._url)

    def test_factory_default_unchanged(self):
        """不传参时行为不变：有 KIMI key 走 kimi/k3。"""
        from src.decision.provider import create_provider
        from src.decision.provider.base import KimiProvider
        path = self._env_file("KIMI_API_KEY=k\nDEEPSEEK_API_KEY=d\n")
        p = create_provider(env_path=path)
        self.assertIsInstance(p, KimiProvider)
        self.assertEqual(p.model, "k3")

    def test_deepseek_min_max_tokens(self):
        """v4 是 always_thinking：reasoning 吃 max_tokens，下限 256
        （与 k3 同一个坑，防止 content 为空）。"""
        from src.decision.provider.base import DeepSeekProvider
        p = DeepSeekProvider("k", model="deepseek-v4-pro")
        captured = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        class FakeSession:
            def post(self, url, json=None, timeout=None):
                captured.update(json or {})
                return FakeResp()

        p._sess = FakeSession()
        p.chat([{"role": "user", "content": "hi"}], max_tokens=100)
        self.assertEqual(captured["max_tokens"], 256)
        self.assertEqual(captured["model"], "deepseek-v4-pro")

    def test_token_limits_clamp(self):
        """网关可热调 max_tokens 上下限：floor 抬下限、ceiling 压上限；
        传 0 保留当前值（不擦掉 provider 自带的 256 下限）。"""
        from src.decision.provider.base import DeepSeekProvider
        p = DeepSeekProvider("k", model="deepseek-v4-flash")
        captured = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        class FakeSession:
            def post(self, url, json=None, timeout=None):
                captured.update(json or {})
                return FakeResp()

        p._sess = FakeSession()
        chat = lambda mt: p.chat([{"role": "user", "content": "hi"}],
                                 max_tokens=mt)
        chat(100)
        self.assertEqual(captured["max_tokens"], 256)     # 默认下限
        p.set_token_limits(floor=512, ceiling=1024)
        chat(100)
        self.assertEqual(captured["max_tokens"], 512)     # 新下限
        chat(99999)
        self.assertEqual(captured["max_tokens"], 1024)    # 新上限
        p.set_token_limits(0, 0)                          # 0 = 保留当前值
        chat(100)
        self.assertEqual(captured["max_tokens"], 512)


if __name__ == "__main__":
    unittest.main()
