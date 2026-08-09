# -*- coding: utf-8 -*-
"""test_gateway_hotreload.py — 网关热重启健壮性单测。

覆盖"网关不坏"的关键场景（docs/GATEWAY.md §三）：
- 代码改坏（create_app 抛异常）→ 旧 server 继续服务，网关不挂
- 语法错误文件 → reload 失败，旧 server 继续服务
- 修复后再保存 → 自动恢复热重启
- 端口被占 → bind 抛 OSError 被捕获（不崩溃）
"""

import os
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gateway.hot_reload import HotReloadServer

GW_DIR = os.path.dirname(os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "gateway", "app.py")))
APP_FILE = os.path.join(GW_DIR, "app.py")
PORT = 13980


class FakeClock:
    def __init__(self):
        self.t = time.time()
    def __call__(self):
        return self.t
    def advance(self, s):
        self.t += s


def _read_orig():
    with open(APP_FILE, encoding="utf-8") as f:
        return f.read()


class HotReloadRobustnessTest(unittest.TestCase):
    """对真实 app.py 文件做热重启健壮性测试（会临时改写并恢复文件）。"""

    @classmethod
    def setUpClass(cls):
        cls.orig = _read_orig()
        cls.calls = {"n": 0}

        def factory():
            cls.calls["n"] += 1
            from src.gateway.app import create_app
            return create_app(project_root=os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))

        cls.srv = HotReloadServer(factory, host="127.0.0.1", port=PORT,
                                  poll_interval=0.2)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.srv.shutdown()
        finally:
            # 恢复原始文件，绝不让测试破坏仓库代码
            with open(APP_FILE, "w", encoding="utf-8") as f:
                f.write(cls.orig)
            time.sleep(0.3)

    def _alive(self) -> bool:
        try:
            r = urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/api/agent/status", timeout=2)
            return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _write(self, content: str):
        with open(APP_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        time.sleep(1.2)   # 等 watch 检测 + 退避窗口处理

    def test_initial_alive(self):
        self.assertTrue(self._alive(), "初始 server 应可访问")

    def test_broken_code_keeps_old_server(self):
        """改坏代码（create_app 抛异常）→ 网关不挂。"""
        bad = self.orig.replace(
            "def create_app(",
            "def create_app(\n    raise RuntimeError('intentional')\n    pass")
        self._write(bad)
        self.assertTrue(self._alive(), "代码改坏后网关不应挂")
        self._write(self.orig)
        time.sleep(1.5)
        self.assertTrue(self._alive(), "修复后应恢复")

    def test_syntax_error_keeps_old_server(self):
        """语法错误文件 → reload 失败但网关不挂。"""
        self._write("def broken(:\n")
        self.assertTrue(self._alive(), "语法错误后网关不应挂")
        self._write(self.orig)
        time.sleep(1.5)
        self.assertTrue(self._alive(), "修复后应恢复")

    def test_bind_conflict_raises_oserror(self):
        """端口被占 → _bind_server 抛 OSError（不崩溃，由调用方处理）。"""
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", PORT + 1))
        sock.listen(1)
        try:
            srv2 = HotReloadServer(lambda: None, host="127.0.0.1",
                                   port=PORT + 1, poll_interval=99)
            srv2._app = object()
            with self.assertRaises(OSError):
                srv2._bind_server()
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
