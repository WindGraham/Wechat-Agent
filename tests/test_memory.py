# -*- coding: utf-8 -*-
"""决策层 memory 工具离线单测（全临时目录，不创建真实 memory 文件）。

覆盖 2026-08-10 审查修复点：
  - store.add 删除后 id 冲突（max_seq+1 不复用）
  - store.add 去重（同 key+同内容 → 更新原条目）
  - MemoryTool scope=user 缺 user 属性 → 拒绝（防写进 unnamed.json）
  - MemoryTool scope 缺省按当前会话；无会话才 global
  - source 私聊/群聊区分（is_group）
  - 别名登记 + 反查
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.decision.memory import MemoryStore, MemoryTool          # noqa: E402
from src.decision.memory.injector import MemoryInjector          # noqa: E402


class _TmpStoreMixin:
    """每个用例独立临时 memory 根（测试不碰真实 workspace/memory）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(root=self._tmp.name)
        self.tool = MemoryTool(store=self.store)

    def tearDown(self):
        self._tmp.cleanup()


class StoreIdTest(_TmpStoreMixin, unittest.TestCase):
    """add 的 id 生成：删除后不回落，杜绝 id 冲突。"""

    def test_id_monotonic_after_delete(self):
        a = self.store.add(content="事实一", scope="user", user="风图")
        b = self.store.add(content="事实二", scope="user", user="风图")
        c = self.store.add(content="事实三", scope="user", user="风图")
        ids = {a["id"], b["id"], c["id"]}
        self.assertEqual(len(ids), 3)                 # 初始互不重复
        self.assertTrue(self.store.delete(b["id"]))   # 删中间一条
        d = self.store.add(content="事实四", scope="user", user="风图")
        self.assertNotIn(d["id"], ids)                # 不复用已存在 id
        all_ids = [f["id"] for f in
                   self.store.list_scope("user", user="风图")]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_update_by_id_targets_right_entry(self):
        a = self.store.add(content="事实一", scope="user", user="风图")
        b = self.store.add(content="事实二", scope="user", user="风图")
        self.store.delete(a["id"])
        c = self.store.add(content="事实三", scope="user", user="风图")
        self.assertTrue(self.store.update(c["id"], content="事实三改"))
        rows = self.store.list_scope("user", user="风图")
        by_id = {f["id"]: f["content"] for f in rows}
        self.assertEqual(by_id[c["id"]], "事实三改")
        self.assertEqual(by_id[b["id"]], "事实二")     # b 未被误伤


class StoreDedupTest(_TmpStoreMixin, unittest.TestCase):
    """add 去重：同 key + 内容归一化相等 → 更新原条目而非新增。"""

    def test_dedup_updates_not_duplicates(self):
        e1 = self.store.add(content="风图  不喜欢 表情包", key="偏好",
                            scope="user", user="风图")
        e2 = self.store.add(content="风图不喜欢表情包", key="偏好",
                            scope="user", user="风图")
        self.assertEqual(e1["id"], e2["id"])          # 更新同一条目
        rows = self.store.list_scope("user", user="风图")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "风图不喜欢表情包")

    def test_same_content_diff_key_not_dedup(self):
        self.store.add(content="同一句", key="a", scope="user", user="风图")
        self.store.add(content="同一句", key="b", scope="user", user="风图")
        self.assertEqual(len(self.store.list_scope("user", user="风图")), 2)


class ToolGuardTest(_TmpStoreMixin, unittest.TestCase):
    """MemoryTool 的守卫与 scope/source 语义。"""

    def test_scope_user_requires_user_attr(self):
        out = self.tool.run({"name": "memory", "op": "add",
                             "scope": "user", "content": "X"})
        self.assertIn("user", out)
        self.assertIn("必须", out)
        # 未写进 unnamed.json
        rows = self.store.list_scope("user", user="")
        self.assertEqual(rows, [])

    def test_scope_defaults_to_current_session(self):
        self.tool.run({"name": "memory", "op": "add", "content": "群梗"},
                      current_session="特高课")
        self.assertEqual(len(self.store.list_scope(
            "session", session="特高课")), 1)
        self.assertEqual(self.store.list_scope("global"), [])

    def test_scope_defaults_global_without_session(self):
        self.tool.run({"name": "memory", "op": "add", "content": "通用"})
        self.assertEqual(len(self.store.list_scope("global")), 1)

    def test_source_private_chat(self):
        self.tool.run({"name": "memory", "op": "add", "content": "私密",
                       "scope": "session"},
                      current_session="风图", is_group=False)
        rows = self.store.list_scope("session", session="风图")
        self.assertEqual(rows[0]["source"], "私聊")

    def test_source_group_is_session_name(self):
        self.tool.run({"name": "memory", "op": "add", "content": "群约定",
                       "scope": "session"},
                      current_session="特高课", is_group=True)
        rows = self.store.list_scope("session", session="特高课")
        self.assertEqual(rows[0]["source"], "特高课")


class AliasTest(_TmpStoreMixin, unittest.TestCase):
    """别名登记 + 反查 resolve_user。"""

    def test_alias_resolve(self):
        self.store.add(content="喜欢短消息", scope="user", user="风图")
        self.assertTrue(self.store.add_alias("风图", "图图"))
        canonical, path = self.store.resolve_user("图图")
        self.assertEqual(canonical, "风图")
        self.assertIsNotNone(path)
        # 主昵称直接命中
        canonical2, _ = self.store.resolve_user("风图")
        self.assertEqual(canonical2, "风图")
        # 未知名返回 None
        self.assertEqual(self.store.resolve_user("不存在的人"), (None, None))


class InjectorTest(_TmpStoreMixin, unittest.TestCase):
    """注入块：无记忆返回空串；有记忆带来源标注。"""

    def _msg(self, sender, content):
        return type("M", (), {"sender": sender, "content": content,
                              "is_mine": False})()

    def test_empty_when_no_memory(self):
        inj = MemoryInjector(self.store)
        out = inj.build_memory_block("特高课", True,
                                     [self._msg("风图", "hi")], [])
        self.assertEqual(out, "")

    def test_global_and_user_blocks(self):
        self.store.add(content="主人喜欢短消息", scope="global")
        self.store.add(content="风图爱爬山", scope="user", user="风图")
        inj = MemoryInjector(self.store)
        out = inj.build_memory_block("特高课", True,
                                     [self._msg("风图", "hi")], [])
        self.assertIn("你是谁", out)                   # L0 全局块
        self.assertIn("主人喜欢短消息", out)
        self.assertIn("当前所在群", out)               # L1+L2 块
        self.assertIn("风图爱爬山", out)
        self.assertIn("来自", out)                     # 来源标注生效

    def test_user_not_present_not_injected(self):
        self.store.add(content="图图爱火锅", scope="user", user="图图")
        inj = MemoryInjector(self.store)
        out = inj.build_memory_block("特高课", True,
                                     [self._msg("风图", "hi")], [])
        self.assertNotIn("图图爱火锅", out)            # 没在场的人不注入


if __name__ == "__main__":
    unittest.main()
