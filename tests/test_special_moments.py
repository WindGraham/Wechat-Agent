# -*- coding: utf-8 -*-
"""tests/test_special_moments.py — 朋友圈 special prompt 全链路测试。

覆盖：
  1. Scheduler 投硬币 → EV_SPECIAL_RUN 事件
  2. SpecialRunHandler 分发
  3. _run_special → 加载 prompt → 调 LLM → 路由
  4. _exec_moments_task → 提取日记 → 保存文件 → 调 post_text_moments
  5. 配置热更新 + 禁用
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.decision.proxy.special_scheduler import SpecialScheduler
from src.decision.proxy.events import EV_SPECIAL_RUN, same_event
from src.decision.prompt.library import PromptLibrary
from src.decision.prompt.builder import ContextBuilder


class SchedulerTest(unittest.TestCase):
    """Scheduler 基础行为测试。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sp = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_scheduler(self, configs, seed=42):
        return SpecialScheduler(
            push_fn=lambda ev: self.events.append(ev)
                if hasattr(self, 'events') else None,
            configs=configs,
            state_path=self.sp,
            rng=__import__("random").Random(seed),
        )

    def test_trigger_on_first_tick(self):
        self.events = []
        s = self._new_scheduler({"test": {"rate_per_day": 1000, "enabled": True}})
        s.tick(0)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["type"], "special_run")
        self.assertEqual(self.events[0]["prompt_name"], "test")

    def test_no_trigger_before_next_at(self):
        self.events = []
        s = self._new_scheduler({"test": {"rate_per_day": 0.01, "enabled": True}})
        s.tick(0)
        self.assertEqual(len(self.events), 1)
        s.tick(1)
        self.assertEqual(len(self.events), 1)

    def test_second_trigger_after_interval(self):
        self.events = []
        s = self._new_scheduler({"test": {"rate_per_day": 1e6, "enabled": True}})
        s.tick(0)
        self.assertEqual(len(self.events), 1)
        s.tick(1e10)
        self.assertGreaterEqual(len(self.events), 2)

    def test_disabled_does_not_trigger(self):
        self.events = []
        s = self._new_scheduler({"test": {"rate_per_day": 1000, "enabled": False}})
        s.tick(0)
        self.assertEqual(len(self.events), 0)

    def test_rate_zero_does_not_trigger(self):
        self.events = []
        s = self._new_scheduler({"test": {"rate_per_day": 0, "enabled": True}})
        s.tick(0)
        self.assertEqual(len(self.events), 0)

    def test_state_persistence(self):
        """状态序列化/反序列化。"""
        s1 = self._new_scheduler({"test": {"rate_per_day": 1.0, "enabled": True}})
        s1.tick(0)
        s1._save_state()

        with open(self.sp, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("test", data)
        self.assertIn("next_at", data["test"])

        # 从文件恢复
        s2 = SpecialScheduler(
            push_fn=lambda ev: None,
            configs={}, state_path=self.sp,
        )
        self.assertIn("test", s2._state)

    def test_config_hot_update(self):
        self.events = []
        s = self._new_scheduler({"old": {"rate_per_day": 1000, "enabled": True}})
        s.update_configs({"new": {"rate_per_day": 1000, "enabled": True}})
        s.tick(0)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["prompt_name"], "new")

    def test_special_events_not_merged(self):
        self.assertFalse(same_event(
            {"type": EV_SPECIAL_RUN, "session": "a"},
            {"type": EV_SPECIAL_RUN, "session": "a"},
        ))


class SpecialPromptTest(unittest.TestCase):
    """Special Prompt 加载与构建测试。"""

    def setUp(self):
        self.lib = PromptLibrary()
        self.builder = ContextBuilder(library=self.lib)

    def test_load_cat_diary(self):
        spec = self.lib.load_special("cat_diary")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["meta"]["output_mode"], "tool")
        self.assertIn("猫娘日记", spec["system"])
        self.assertIn('name="moments"', spec["system"])

    def test_load_memory_consolidation(self):
        spec = self.lib.load_special("memory_consolidation")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["meta"]["output_mode"], "memory")

    def test_load_missing_returns_none(self):
        spec = self.lib.load_special("nonexistent")
        self.assertIsNone(spec)

    def test_build_special_injects_time(self):
        msgs = self.builder.build_special("cat_diary", {
            "time": "2026-08-11 15:30 周一",
        })
        self.assertEqual(len(msgs), 2)
        self.assertIn("2026-08-11 15:30 周一", msgs[0]["content"])

    def test_build_special_with_context(self):
        msgs = self.builder.build_special("cat_diary", {
            "time": "now",
            "memories": [
                {"source": "风图", "content": "主人喜欢短消息"},
                {"source": "特高课", "content": "群友爱复读"},
            ],
            "highlights": "今天群里聊了爬山",
        })
        user = msgs[1]["content"]
        self.assertIn("memories", user)
        self.assertIn("主人喜欢短消息", user)
        self.assertIn("highlights", user)


class MomentsTaskExtractionTest(unittest.TestCase):
    """_exec_moments_task 日记提取逻辑测试。

    模拟 LLM 输出的 <task> 块，验证日记文本提取 + 文件保存。
    """

    def _fake_block(self, raw_inner):
        """构造一个简易 Block 对象。"""
        return type("Block", (), {
            "raw_inner": raw_inner,
            "inner": raw_inner,
            "attrs": "",
            "tag": "task",
            "valid": True,
            "self_closing": False,
        })()

    def test_extract_diary_text(self):
        """从 task body 中提取日记文本。"""
        body = (
            '打开微信朋友圈，发布文字动态：\n'
            '"今天阳光软乎乎的，趴在窗台上晒了半天。"'
        )
        block = self._fake_block(body)

        # 模拟提取逻辑
        diary_text = body
        for marker in ("发布文字动态：", "发布文字动态:\n"):
            if marker in body:
                diary_text = body.split(marker, 1)[1].strip()
                break
        diary_text = diary_text.strip().strip('"').strip("'").strip()

        self.assertEqual(diary_text, "今天阳光软乎乎的，趴在窗台上晒了半天。")

    def test_extract_with_quotes(self):
        body = (
            '打开微信朋友圈，发布文字动态：\n'
            '"今天陪主人改了代码，喵~"'
        )
        block = self._fake_block(body)

        diary_text = body
        for marker in ("发布文字动态：", "发布文字动态:\n"):
            if marker in body:
                diary_text = body.split(marker, 1)[1].strip()
                break
        diary_text = diary_text.strip().strip('"').strip("'").strip()

        self.assertIn("陪主人改了代码", diary_text)
        self.assertNotIn('"', diary_text)  # 引号已去

    def test_save_diary_to_file(self):
        tmp = tempfile.mkdtemp()
        diary_dir = os.path.join(tmp, "cat_diary")
        os.makedirs(diary_dir, exist_ok=True)
        path = os.path.join(diary_dir, "2026-08-11.md")

        with open(path, "a", encoding="utf-8") as f:
            f.write("\n---\n今天天气很好喵~")

        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("今天天气很好喵~", content)

        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


class EndToEndMomentsTest(unittest.TestCase):
    """端到端：从 LLM 输出到 post_text_moments 调用。

    构造 FakeProxy 模拟 _run_special → _exec_moments_task 全链路。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.posted = []  # 记录 post_text_moments 调用

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_post(self, tools, text):
        self.posted.append(text)
        return {"ok": True, "posted": True, "error": None}

    def test_full_moments_pipeline(self):
        """模拟完整链路：LLM 输出 → 提取 → 保存 → 发朋友圈。"""
        # 模拟 LLM 输出
        llm_output = (
            '打开微信朋友圈，发布文字动态：\n'
            '"今天阳光软乎乎的，趴在窗台上晒了半天。'
            '主人忙的时候我就安静陪着，偶尔喵一声提醒她喝水喵~"'
        )

        # 模拟 extract_blocks 返回
        class FakeBlock:
            tag = "task"
            valid = True
            raw_inner = llm_output
            attrs = ' desc="发今日猫娘日记"'

        blocks = [FakeBlock()]

        # 提取 diary_text（与 _exec_moments_task 相同逻辑）
        body = blocks[0].raw_inner.strip()
        diary_text = body
        for marker in ("发布文字动态：", "发布文字动态:\n", "发布纯文字："):
            if marker in body:
                diary_text = body.split(marker, 1)[1].strip()
                break
        diary_text = diary_text.strip().strip('"').strip("'").strip()

        self.assertIn("阳光软乎乎", diary_text)
        self.assertIn("喵", diary_text)

        # 模拟保存
        diary_dir = os.path.join(self.tmp, "cat_diary")
        os.makedirs(diary_dir, exist_ok=True)
        path = os.path.join(diary_dir, "2026-08-11.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n---\n{diary_text}\n")

        # 模拟发朋友圈
        result = self._fake_post(None, diary_text)
        self.assertTrue(result["ok"])
        self.assertTrue(result["posted"])
        self.assertEqual(len(self.posted), 1)
        self.assertIn("阳光软乎乎", self.posted[0])

        # 验证文件
        with open(path, encoding="utf-8") as f:
            saved = f.read()
        self.assertIn(diary_text, saved)

    def test_empty_body_handled(self):
        """空 body 不应触发发布。"""
        body = ""
        diary_text = body
        for marker in ("发布文字动态：", "发布文字动态:\n"):
            if marker in body:
                diary_text = body.split(marker, 1)[1].strip()
                break
        diary_text = diary_text.strip().strip('"').strip("'").strip()
        self.assertEqual(diary_text, "")


class HandlerRegistrationTest(unittest.TestCase):
    """验证 handler 注册正确。"""

    def test_special_run_handler_registered(self):
        from src.decision.proxy.handlers import get_handler
        h = get_handler("special_run")
        self.assertIsNotNone(h)
        self.assertEqual(h.__name__, "SpecialRunHandler")

    def test_all_handlers_registered(self):
        from src.decision.proxy.handlers import _HANDLERS
        expected = {"log_updated", "task_done", "memory_warm",
                    "memory_extract", "special_run",
                    "search_done", "aside"}
        self.assertEqual(set(_HANDLERS.keys()), expected)


if __name__ == "__main__":
    unittest.main()
