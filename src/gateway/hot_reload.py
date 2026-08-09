# -*- coding: utf-8 -*-
"""gateway/hot_reload.py — 网关自身的热重启机制。

网关独立常驻进程后，改网关代码（src/gateway/*.py）不应该需要重启整个进程。
本模块：
  - 用 werkzeug make_server 代替 app.run()（可手动 shutdown）
  - 监视线程轮询 src/gateway/*.py 的 mtime（含 app.py 引用的兄弟模块）
  - 检测到变化 → importlib.reload 相关模块 → 重新 create_app()
    → 旧 server shutdown → 新 server 重新 bind 同端口
  - 期间 agent 子进程完全不受影响（supervisor 实例常驻内存不重载）

设计要点：
  - create_factory: 每次重建时调用，返回新 Flask app（可在闭包里捕获
    supervisor 等常驻对象——它们不随 flask 模块 reload）
  - 端口冲突处理：旧 server shutdown 后短暂等待端口释放再 bind
  - 监视范围：src/gateway/*.py（不含 __pycache__），按目录 mtime 快照对比
"""

import importlib
import logging
import os
import threading
import time

log = logging.getLogger("gateway.hot_reload")

POLL_INTERVAL = 1.5          # mtime 轮询间隔（秒）
PORT_RELEASE_WAIT = 1.0      # 旧 server 关闭后等待端口释放（秒）


def _gateway_dir():
    """src/gateway/ 目录绝对路径。"""
    return os.path.dirname(os.path.abspath(__file__))


def _snapshot(gw_dir):
    """返回 {绝对路径: mtime} 快照（只含 .py，排除 __pycache__）。"""
    snap = {}
    try:
        for name in os.listdir(gw_dir):
            if name.startswith("__") or name.endswith(".pyc"):
                continue
            path = os.path.join(gw_dir, name)
            if os.path.isfile(path) and name.endswith(".py"):
                snap[path] = os.path.getmtime(path)
    except OSError:
        pass
    return snap


def _reload_modules():
    """reload src.gateway 及其子模块（app/supervisor 除外——
    supervisor 常驻不重载，否则会丢 agent 进程句柄）。"""
    import src.gateway as pkg
    # reload 顺序：先子模块后包；app 是主要目标
    for mod_name in ("src.gateway.group_config", "src.gateway.app",
                     "src.gateway"):
        try:
            mod = importlib.import_module(mod_name)
            importlib.reload(mod)
        except Exception:  # noqa: BLE001
            log.exception("reload %s 失败", mod_name)
            raise
    log.info("gateway 模块已 reload（app/group_config）")


class HotReloadServer:
    """可热重启的网关 server。

    用法：
        server = HotReloadServer(create_factory, host, port)
        server.serve_forever()      # 阻塞；内部自动 reload
        server.shutdown()           # 停止
    """

    def __init__(self, create_factory, host="127.0.0.1", port=13014,
                 poll_interval=POLL_INTERVAL):
        """
        create_factory: () -> Flask app。每次重建时调用；可在闭包里捕获
            supervisor 等常驻对象。
        """
        self._create_factory = create_factory
        self._host = host
        self._port = port
        self._poll = poll_interval

        self._gw_dir = _gateway_dir()
        self._snap = _snapshot(self._gw_dir)
        self._stop = threading.Event()
        self._server = None          # 当前 werkzeug server
        self._lock = threading.RLock()
        self._app = None

    # ---------------------------------------------------------------- 生命周期
    def serve_forever(self):
        """阻塞运行：起 server + 监视线程，直到 shutdown()。"""
        self._start_server()
        watch = threading.Thread(target=self._watch_loop, daemon=True,
                                 name="gateway-hotreload-watch")
        watch.start()
        log.info("hot-reload server on %s:%d (watch %s)",
                 self._host, self._port, self._gw_dir)
        while not self._stop.is_set():
            self._stop.wait(0.5)

    def shutdown(self):
        self._stop.set()
        self._stop_server()

    # ---------------------------------------------------------------- 内部
    def _start_server(self):
        from werkzeug.serving import make_server
        with self._lock:
            self._app = self._create_factory()
            try:
                self._server = make_server(self._host, self._port, self._app,
                                           threaded=True)
            except OSError as e:
                log.error("bind %s:%d 失败: %s", self._host, self._port, e)
                raise
        # serve 在独立线程里跑（serve_forever 阻塞）
        t = threading.Thread(target=self._server.serve_forever, daemon=True,
                             name="gateway-werkzeug")
        t.start()
        log.info("gateway server 已启动 (pid=%s)", os.getpid())

    def _stop_server(self):
        with self._lock:
            srv = self._server
            self._server = None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:  # noqa: BLE001
                log.exception("server shutdown 异常")

    def _watch_loop(self):
        """轮询 mtime；变化 → reload 模块 → 重建 server。"""
        while not self._stop.is_set():
            self._stop.wait(self._poll)
            if self._stop.is_set():
                return
            try:
                self._maybe_reload()
            except Exception:  # noqa: BLE001
                log.exception("热重启尝试失败（保持旧 server 运行）")

    def _maybe_reload(self):
        cur = _snapshot(self._gw_dir)
        if cur == self._snap:
            return
        changed = [p for p in cur if cur.get(p) != self._snap.get(p)]
        self._snap = cur
        log.info("检测到网关代码变化: %s，热重启中…", changed)
        try:
            _reload_modules()
        except Exception:  # noqa: BLE001
            log.error("模块 reload 失败，跳过本次热重启")
            return
        # 重建 server：先关旧的（释放端口），短暂等待再起新的
        self._stop_server()
        time.sleep(PORT_RELEASE_WAIT)
        try:
            self._start_server()
        except Exception:  # noqa: BLE001
            log.exception("重建 server 失败（网关可能不可用，请手动重启网关）")
