# -*- coding: utf-8 -*-
"""test_gateway.py — src/gateway 的单测（Flask test_client，全离线）。

用 tmp 目录搭一个最小工作区副本（config/prompts、config/personas、
config/runtime.json、workspace/），不碰真实仓库文件。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gateway.app import create_app, _mask  # noqa: E402


class GatewayTestBase(unittest.TestCase):
    """搭 tmp 工作区副本并创建 test_client。"""

    token = None  # 子类覆盖以开启鉴权

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        # prompts：order.txt + system/user 各一块 + 一个未列入清单的块
        os.makedirs(os.path.join(self.root, "config", "prompts", "system"))
        os.makedirs(os.path.join(self.root, "config", "prompts", "user"))
        self._w("config/prompts/order.txt",
                "# 注释行\nsystem/persona.md\nuser/history.md\n")
        self._w("config/prompts/system/persona.md", "人设块\n")
        self._w("config/prompts/user/history.md", "历史块\n")
        self._w("config/prompts/user/task_receipt.md", "任务回执块\n")
        # personas
        os.makedirs(os.path.join(self.root, "config", "personas"))
        self._w("config/personas/default.yaml", "identity:\n  name: 测试\n")
        # runtime.json
        self._w("config/runtime.json",
                json.dumps({"paused": False, "history_size": 200,
                            "sweep_interval": [45, 90]}))
        # workspace 运行状态
        os.makedirs(os.path.join(self.root, "workspace", "runtime"))
        self._w("workspace/runtime/watermarks.json",
                json.dumps({"特高课": 42}))
        self._w("workspace/runtime/queue.json",
                json.dumps([{"kind": "notify", "session": "特高课"}]))
        task_dir = os.path.join(self.root, "workspace", "tasks",
                                "2026-08-08", "t0007_特高课_m3_整理周报")
        os.makedirs(task_dir)
        self._w(os.path.relpath(os.path.join(task_dir, "task.json"),
                                self.root),
                json.dumps({"task_id": "t0007", "session": "特高课",
                            "desc": "整理周报", "status": "running"}))

        self._old_token = os.environ.get("WECHAT_AGENT_GATEWAY_TOKEN")
        if self.token is None:
            os.environ.pop("WECHAT_AGENT_GATEWAY_TOKEN", None)
        else:
            os.environ["WECHAT_AGENT_GATEWAY_TOKEN"] = self.token
        self.app = create_app(project_root=self.root)
        self.client = self.app.test_client()

    def tearDown(self):
        if self._old_token is None:
            os.environ.pop("WECHAT_AGENT_GATEWAY_TOKEN", None)
        else:
            os.environ["WECHAT_AGENT_GATEWAY_TOKEN"] = self._old_token
        self._tmp.cleanup()

    def _w(self, rel, content):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def _r(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def auth_headers(self):
        if self.token is None:
            return {}
        return {"Authorization": "Bearer " + self.token}


class FilesApiTest(GatewayTestBase):
    def test_list_prompts_order_and_groups(self):
        r = self.client.get("/api/files?dir=prompts")
        self.assertEqual(r.status_code, 200)
        files = r.get_json()["files"]
        names = [f["name"] for f in files]
        # order.txt 置顶，随后按 order.txt 顺序，未列入清单的排末尾
        self.assertEqual(names, ["order.txt", "system/persona.md",
                                 "user/history.md", "user/task_receipt.md"])
        groups = {f["name"]: f["group"] for f in files}
        self.assertEqual(groups["system/persona.md"], "system")
        self.assertEqual(groups["user/history.md"], "user")
        self.assertEqual(groups["order.txt"], "order")
        orders = {f["name"]: f["order"] for f in files}
        self.assertEqual(orders["system/persona.md"], 1)
        self.assertIsNone(orders["user/task_receipt.md"])

    def test_list_personas(self):
        r = self.client.get("/api/files?dir=personas")
        self.assertEqual(r.status_code, 200)
        files = r.get_json()["files"]
        self.assertEqual([f["name"] for f in files], ["default.yaml"])
        self.assertEqual(files[0]["group"], "persona")

    def test_list_bad_dir(self):
        r = self.client.get("/api/files?dir=../../etc")
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/files")
        self.assertEqual(r.status_code, 400)

    def test_read_file(self):
        r = self.client.get("/api/file?path=config/prompts/system/persona.md")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["content"], "人设块\n")
        self.assertIn("mtime", j)

    def test_read_missing_file(self):
        r = self.client.get("/api/file?path=config/prompts/user/none.md")
        self.assertEqual(r.status_code, 404)

    def test_write_file(self):
        r = self.client.put(
            "/api/file?path=config/prompts/user/history.md",
            json={"content": "改过的历史块\n"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._r("config/prompts/user/history.md"),
                         "改过的历史块\n")

    def test_write_bad_body(self):
        r = self.client.put("/api/file?path=config/prompts/user/history.md",
                            json={"content": 123})
        self.assertEqual(r.status_code, 400)

    def test_path_traversal_rejected(self):
        for bad in ("../runtime.json",
                    "config/prompts/../../runtime.json",
                    "config/prompts/../../workspace/.env",
                    "/etc/passwd",
                    "config/runtime.json",        # 白名单外的合法路径也不行
                    "workspace/.env",
                    "config/prompts2/x.md"):
            r = self.client.get("/api/file", query_string={"path": bad})
            self.assertIn(r.status_code, (400, 403), bad)
            r = self.client.put("/api/file", query_string={"path": bad},
                                json={"content": "x"})
            self.assertIn(r.status_code, (400, 403), bad)


class RuntimeApiTest(GatewayTestBase):
    def test_get_runtime(self):
        r = self.client.get("/api/runtime")
        self.assertEqual(r.status_code, 200)
        cfg = r.get_json()["config"]
        self.assertEqual(cfg["history_size"], 200)
        self.assertFalse(cfg["paused"])

    def test_put_runtime_merges(self):
        r = self.client.put("/api/runtime",
                            json={"paused": True, "owner": "特高课"})
        self.assertEqual(r.status_code, 200)
        cfg = json.loads(self._r("config/runtime.json"))
        self.assertTrue(cfg["paused"])
        self.assertEqual(cfg["owner"], "特高课")
        self.assertEqual(cfg["history_size"], 200)  # 未提交的字段保留

    def test_put_runtime_validation(self):
        # 未知字段
        r = self.client.put("/api/runtime", json={"no_such": 1})
        self.assertEqual(r.status_code, 400)
        # 类型错误：int 字段给 str、interval 给非二元数组、bool 给 int
        r = self.client.put("/api/runtime", json={"history_size": "x"})
        self.assertEqual(r.status_code, 400)
        r = self.client.put("/api/runtime", json={"sweep_interval": [1, 2, 3]})
        self.assertEqual(r.status_code, 400)
        r = self.client.put("/api/runtime", json={"paused": 1})
        self.assertEqual(r.status_code, 400)
        # 校验失败不落盘
        self.assertFalse(json.loads(self._r("config/runtime.json"))["paused"])
        # 非对象 body
        r = self.client.put("/api/runtime", json=[1, 2])
        self.assertEqual(r.status_code, 400)


class EnvApiTest(GatewayTestBase):
    def test_get_env_missing_file(self):
        r = self.client.get("/api/env")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["keys"], [])

    def test_put_then_get_masked(self):
        r = self.client.put("/api/env",
                            json={"KIMI_API_KEY": "sk-abcdef123456"})
        self.assertEqual(r.status_code, 200)
        # 落盘的是原文
        self.assertEqual(self._r("workspace/.env"),
                         "KIMI_API_KEY=sk-abcdef123456\n")
        # 读回的是脱敏值（前4后2），不含原文
        r = self.client.get("/api/env")
        keys = r.get_json()["keys"]
        self.assertEqual(keys, [{"key": "KIMI_API_KEY",
                                 "masked": "sk-a****56"}])
        self.assertNotIn("sk-abcdef123456", r.get_data(as_text=True))

    def test_put_empty_value_keeps_existing(self):
        self.client.put("/api/env", json={"KIMI_API_KEY": "sk-abcdef123456"})
        r = self.client.put("/api/env", json={"KIMI_API_KEY": "",
                                              "OTHER": "xyz7890abc"})
        self.assertEqual(r.status_code, 200)
        content = self._r("workspace/.env")
        self.assertIn("KIMI_API_KEY=sk-abcdef123456", content)
        self.assertIn("OTHER=xyz7890abc", content)

    def test_put_bad_key(self):
        r = self.client.put("/api/env", json={"BAD KEY": "x"})
        self.assertEqual(r.status_code, 400)

    def test_mask_short_value(self):
        self.assertEqual(_mask("abc"), "****")
        self.assertEqual(_mask("abcdef"), "****")
        self.assertEqual(_mask("abcdefg"), "abcd****fg")


class StatusApiTest(GatewayTestBase):
    def test_status_full(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["watermarks"], {"特高课": 42})
        self.assertEqual(j["queue"][0]["kind"], "notify")
        self.assertEqual(len(j["tasks"]), 1)
        t = j["tasks"][0]
        self.assertEqual(t["task_id"], "t0007")
        self.assertEqual(t["status"], "running")
        self.assertEqual(t["date"], "2026-08-08")

    def test_status_missing_files(self):
        shutil.rmtree(os.path.join(self.root, "workspace"))
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIsNone(j["queue"])
        self.assertIsNone(j["watermarks"])
        self.assertEqual(j["tasks"], [])


class LiveApiTest(GatewayTestBase):
    """实况页 API：/api/events 与 /api/home_scan。"""

    def _write_events(self, lines):
        self._w("workspace/runtime/proxy_events.jsonl", "\n".join(lines) + "\n")

    def test_events_empty(self):
        r = self.client.get("/api/events")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "events": []})

    def test_events_data_reversed(self):
        self._write_events([
            json.dumps({"ts": 1, "type": "decision_start", "session": "A"}),
            "这不是合法 JSON",
            "",
            json.dumps({"ts": 2, "type": "route", "session": "A",
                        "blocks": ["reply"],
                        "deliveries": [{"session": "A", "ok": True}]}),
        ])
        r = self.client.get("/api/events")
        self.assertEqual(r.status_code, 200)
        events = r.get_json()["events"]
        # 倒序（最新在前），坏行/空行跳过
        self.assertEqual([e["type"] for e in events],
                         ["route", "decision_start"])
        self.assertEqual(events[0]["deliveries"][0]["ok"], True)

    def test_events_n_limit(self):
        self._write_events([json.dumps({"ts": i, "type": "tick"})
                            for i in range(5)])
        r = self.client.get("/api/events?n=2")
        events = r.get_json()["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["ts"], 4)          # 最新的在前
        # 非法 n 回退默认 50（全部 5 条）
        r = self.client.get("/api/events?n=abc")
        self.assertEqual(len(r.get_json()["events"]), 5)
        # 超大 n 截到上限 500（数据不足则全返回）
        r = self.client.get("/api/events?n=99999")
        self.assertEqual(len(r.get_json()["events"]), 5)

    def test_home_scan_missing(self):
        r = self.client.get("/api/home_scan")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "scan": None})

    def test_home_scan_ok(self):
        self._w("workspace/runtime/home_scan.json", json.dumps({
            "ts": 1723100000,
            "sessions": [{"label": "特高课", "unread_count": 3,
                          "unread_kind": "number", "mention_me": True,
                          "muted": False, "partial": False},
                         {"label": "文件传输助手", "unread_count": 0,
                          "unread_kind": None, "mention_me": False,
                          "muted": True, "partial": False}],
        }))
        r = self.client.get("/api/home_scan")
        self.assertEqual(r.status_code, 200)
        scan = r.get_json()["scan"]
        self.assertEqual(len(scan["sessions"]), 2)
        self.assertEqual(scan["sessions"][0]["unread_kind"], "number")
        self.assertTrue(scan["sessions"][1]["muted"])

    def test_home_scan_corrupt(self):
        self._w("workspace/runtime/home_scan.json", "{坏掉的")
        r = self.client.get("/api/home_scan")
        self.assertEqual(r.get_json(), {"ok": True, "scan": None})


class TokenAuthTest(GatewayTestBase):
    token = "secret-token-123"

    def test_no_token_rejected(self):
        for method, path in (("GET", "/"),
                             ("GET", "/api/files?dir=prompts"),
                             ("GET", "/api/file?path=config/prompts/order.txt"),
                             ("GET", "/api/runtime"),
                             ("GET", "/api/env"),
                             ("GET", "/api/status")):
            r = self.client.open(path, method=method)
            self.assertEqual(r.status_code, 401, path)

    def test_wrong_token_rejected(self):
        r = self.client.get("/api/status",
                            headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_right_token_ok(self):
        h = self.auth_headers()
        r = self.client.get("/api/status", headers=h)
        self.assertEqual(r.status_code, 200)
        r = self.client.put("/api/runtime", json={"paused": True}, headers=h)
        self.assertEqual(r.status_code, 200)


class NoTokenByDefaultTest(GatewayTestBase):
    def test_open_without_token(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
