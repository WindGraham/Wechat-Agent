# -*- coding: utf-8 -*-
"""gateway/hot_reload.py — 网关自身的热重启机制（健壮版）。

网关独立常驻进程后，改网关代码（src/gateway/*.py）不应该需要重启整个进程。
本模块：
  - 用 werkzeug make_server 代替 app.run()（可手动 shutdown）
  - 监视线程轮询 src/gateway/*.py 的 mtime（含 app.py 引用的兄弟模块）
  - 检测到变化 → importlib.reload 相关模块 → 重新 create_app()
    → 旧 server shutdown → 新 server 重新 bind 同端口
  - 期间 agent 子进程完全不受影响（supervisor 实例常驻内存不重载）

**健壮性设计（网关常驻不坏的关键）**：
  1. 先验证再切换：reload 后先在内存里 create_app() 试建 + 试 bind，
     全部成功才停旧起新——**代码改坏了网关也不会挂，旧 server 继续服务**
  2. reload 失败不更新 mtime 快照：持续重试直到成功（改错的文件存好后
     再修，网关会在下次检测时自动恢复）
  3. 端口占用重试：启动 bind 失败时等待重试（可能是旧进程正在退出），
     超时才退出并给出清晰提示
  4. 线程守护：watch/werkzeug 线程意外死亡自动重启
"""

import importlib
import logging
import os
import threading
import time

log = logging.getLogger("gateway.hot_reload")

POLL_INTERVAL = 1.5          # mtime 轮询间隔（秒）
PORT_RELEASE_WAIT = 1.0      # 旧 server 关闭后等待端口释放（秒）
PORT_RETRY_S = 30            # 启动 bind 失败后的总重试窗口（秒）
PORT_RETRY_INTERVAL = 3.0    # 端口重试间隔（秒）
FAIL_BACKOFF_S = 5.0         # reload 失败后的重试退避（秒，防日志刷屏）


def _gateway_dir():
    """src/gateway/ 目录绝对路径。"""
    return os.path.dirname(os.path.abspath(__file__))


def _snapshot(gw_dir):
    """返回 {绝对路径: mtime} 快照（.py，排除 __pycache__）。

    递归扫 api/ 等子目录——蓝图拆文件后，改 api/agent.py 等必须
    触发热重载（2026-08-10 修复：/api/aside 不生效的根因）。"""
    snap = {}
    for root, dirs, files in os.walk(gw_dir):
        dirs[:] = [d for d in dirs
                   if d != "__pycache__" and not d.startswith("__")]
        for name in files:
            if not name.endswith(".py") or name.endswith(".pyc"):
                continue
            path = os.path.join(root, name)
            try:
                snap[path] = os.path.getmtime(path)
            except OSError:
                pass
    return snap
    return snap


def _reload_modules():
    """reload src.gateway 及其子模块（app/supervisor 除外——
    supervisor 常驻不重载，否则会丢 agent 进程句柄）。

    返回 True 表示全部成功；任一失败抛异常（调用方决定是否回滚快照）。
    """
    import src.gateway as pkg
    # reload 顺序：先子模块后包；app 是主要目标
    for mod_name in ("src.gateway.group_config", "src.gateway.api.agent",
                     "src.gateway.api.live", "src.gateway.api.memory",
                     "src.gateway.api.config", "src.gateway.app",
                     "src.gateway"):
        try:
            mod = importlib.import_module(mod_name)
            importlib.reload(mod)
        except Exception:  # noqa: BLE001
            log.exception("reload %s 失败", mod_name)
            raise
    log.info("gateway 模块已 reload（app/group_config/api.*）")


class HotReloadServer:
    """可热重启的网关 server（崩溃不挂：先验证再切换 + 失败回滚）。

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
        self._lock = threading.RLock()
        self._server = None          # 当前 werkzeug server
        self._server_thread = None   # serve_forever 所在线程
        self._app = None
        self._fail_until = 0.0       # reload 失败退避截止（epoch）

    # ---------------------------------------------------------------- 生命周期
    def serve_forever(self):
        """阻塞运行：起 server + 监视线程，直到 shutdown()。

        - 端口被占：重试 PORT_RETRY_S 秒（可能旧进程正在退出），仍失败
          打印清晰提示后退出（供 systemd 拉起/用户排查）
        - watch 线程死亡：自动重启
        """
        if not self._start_server_with_retry():
            log.error("网关启动失败：端口 %s:%d 无法绑定（可能被其他程序占用）。"
                      "请先停止占用进程（如旧 agent：pkill -f src.main）后重试。",
                      self._host, self._port)
            return False
        watch = self._spawn_watch()
        log.info("hot-reload server on %s:%d (watch %s)",
                 self._host, self._port, self._gw_dir)
        while not self._stop.is_set():
            # watch 线程守护：意外死亡则重启
            if watch is not None and not watch.is_alive():
                log.warning("watch 线程死亡，重启")
                watch = self._spawn_watch()
            # werkzeug 线程守护：意外死亡则重建 server
            if self._server_thread is not None \
                    and not self._server_thread.is_alive() \
                    and self._server is not None:
                log.warning("server 线程死亡，重建")
                self._stop_server()
                if not self._start_server_with_retry():
                    log.error("server 重建失败，网关不可用")
                    break
            self._stop.wait(0.5)
        return True

    def shutdown(self):
        self._stop.set()
        self._stop_server()

    # ---------------------------------------------------------------- 手动刷新
    def reload_now(self) -> dict:
        """手动触发热重启（网关"刷新网关"按钮）。

        与自动检测的区别：
          - 强制忽略 mtime 快照，每次都执行完整 reload 流程（即使文件没变）
          - 同样走"先验证再切换"：代码有问题时保持旧 server，返回错误
          - 不影响 agent 子进程（supervisor 常驻不重载）

        返回 {"ok": bool, "reloaded": bool, "error": str|None}
          - reloaded=False 且 ok=True：无可重载的代码变更（文件没变）
          - ok=False：代码有问题，保持旧 server 继续服务
        """
        import src.gateway as pkg
        now = time.time()
        cur = _snapshot(self._gw_dir)
        changed = [p for p in cur if cur.get(p) != self._snap.get(p)]
        try:
            _reload_modules()
        except Exception as e:  # noqa: BLE001
            log.error("手动刷新失败（模块 reload 出错，保持旧代码服务）: %s", e)
            self._fail_until = now + FAIL_BACKOFF_S
            return {"ok": False, "reloaded": False,
                    "error": f"模块 reload 失败: {type(e).__name__}"}
        try:
            self._build_app()
        except Exception as e:  # noqa: BLE001
            log.exception("手动刷新失败（新代码 create_app 出错，保持旧 server）")
            self._fail_until = now + FAIL_BACKOFF_S
            return {"ok": False, "reloaded": False,
                    "error": f"create_app 失败: {type(e).__name__}"}
        try:
            self._verify_bind()      # 临时端口验证（不碰真实端口）
        except Exception as e:  # noqa: BLE001
            log.error("手动刷新失败（新代码无法 serve，保持旧 server）: %s", e)
            self._fail_until = now + FAIL_BACKOFF_S
            return {"ok": False, "reloaded": False,
                    "error": f"新代码 serve 验证失败: {type(e).__name__}"}
        # 全部验证通过 → 停旧起新（热切换，agent 不受影响）
        self._stop_server()
        time.sleep(PORT_RELEASE_WAIT)
        if not self._start_server_with_retry():
            log.error("手动刷新：真实端口重建失败，尝试用旧 factory 恢复")
            self._snap = _snapshot(self._gw_dir)
            self._fail_until = now + FAIL_BACKOFF_S
            return {"ok": False, "reloaded": False,
                    "error": f"端口 {self._port} 重建失败"}

        self._snap = cur
        if not changed:
            return {"ok": True, "reloaded": True,
                    "error": None, "note": "代码无变更，已强制重载"}
        return {"ok": True, "reloaded": True,
                "error": None, "changed": changed}

    # ---------------------------------------------------------------- 内部
    def _spawn_watch(self):
        t = threading.Thread(target=self._watch_loop, daemon=True,
                             name="gateway-hotreload-watch")
        t.start()
        return t

    def _verify_bind(self):
        """用临时端口验证当前 self._app 能被 werkzeug 正常 bind+serve。
        不碰真实端口（真实端口被旧 server 占着，预绑定必然失败——2026-08-10
        实测教训）。返回 None；失败抛异常。"""
        from werkzeug.serving import make_server
        probe = make_server(self._host, 0, self._app, threaded=True)
        probe.server_close()   # 只验证构造+临时 bind，随即释放

    def _build_app(self):
        """创建 app（不 bind 端口）。失败抛异常——用于热重启前验证。"""
        with self._lock:
            self._app = self._create_factory()
        return self._app

    def _bind_server(self):
        """基于 self._app 创建 werkzeug server（bind 端口）。
        失败抛 OSError——注意 werkzeug make_server 在端口被占时调 sys.exit(1)
        （抛 SystemExit），必须转成 OSError 让调用方统一处理，否则网关会静默退出。
        """
        from werkzeug.serving import make_server
        try:
            with self._lock:
                srv = make_server(self._host, self._port, self._app,
                                  threaded=True)
                self._server = srv
            return srv
        except SystemExit as e:
            raise OSError(f"端口 {self._port} 被占用（werkzeug 退出码 {e.code}）") \
                from None

    def _start_server_with_retry(self) -> bool:
        """创建 app + bind 端口；bind 失败重试 PORT_RETRY_S 秒。"""
        try:
            self._build_app()
        except Exception:  # noqa: BLE001
            log.exception("create_app 失败（初始代码有问题？）")
            return False
        deadline = time.time() + PORT_RETRY_S
        while True:
            try:
                srv = self._bind_server()
                break
            except OSError as e:
                if time.time() >= deadline:
                    log.error("端口 %d 绑定失败: %s", self._port, e)
                    return False
                log.warning("端口 %d 被占，%.0fs 后重试…（可能旧进程正在退出）",
                            self._port, PORT_RETRY_INTERVAL)
                self._stop.wait(PORT_RETRY_INTERVAL)
                if self._stop.is_set():
                    return False
        t = threading.Thread(target=srv.serve_forever, daemon=True,
                             name="gateway-werkzeug")
        t.start()
        self._server_thread = t
        log.info("gateway server 已启动 (pid=%s) bind %s:%d",
                 os.getpid(), self._host, self._port)
        return True

    def _stop_server(self):
        with self._lock:
            srv = self._server
            self._server = None
        self._server_thread = None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:  # noqa: BLE001
                log.exception("server shutdown 异常")

    def _watch_loop(self):
        """轮询 mtime；变化 → reload 模块 → 重建 server。

        健壮性：
        - reload 失败 → 不更新快照，下次继续重试（改坏的文件修好后自动恢复）
        - 重建失败 → 旧 server 继续服务（先验证再切换），网关不挂
        """
        while not self._stop.is_set():
            self._stop.wait(self._poll)
            if self._stop.is_set():
                return
            try:
                self._maybe_reload()
            except Exception:  # noqa: BLE001
                log.exception("热重启尝试异常（保持旧 server 运行）")

    def _maybe_reload(self):
        cur = _snapshot(self._gw_dir)
        if cur == self._snap:
            return
        changed = [p for p in cur if cur.get(p) != self._snap.get(p)]

        # 退避：上次失败后 FAIL_BACKOFF_S 内不重试（防改坏文件时日志刷屏）
        now = time.time()
        if now < self._fail_until:
            return
        self._snap = cur      # 先记快照，避免退避期内重复判断

        # 1. reload 模块（失败 → 保持旧 server，退避后自动重试）
        try:
            _reload_modules()
        except Exception:  # noqa: BLE001
            log.error("模块 reload 失败，保持旧代码继续服务，%.0fs 后自动重试: %s",
                      FAIL_BACKOFF_S, changed)
            self._fail_until = now + FAIL_BACKOFF_S
            return
        # 2. 先验证新代码能 create_app
        try:
            self._build_app()
        except Exception:  # noqa: BLE001
            log.exception("新代码 create_app 失败，保持旧 server 继续服务。"
                          "请修复后再次保存文件触发重载")
            self._fail_until = now + FAIL_BACKOFF_S
            return
        # 3. 临时端口验证新代码能 serve（不碰真实端口）
        try:
            self._verify_bind()
        except Exception:  # noqa: BLE001
            log.error("新代码 serve 验证失败，保持旧 server 继续服务", exc_info=True)
            self._fail_until = now + FAIL_BACKOFF_S
            return
        # 4. 全部验证通过 → 停旧起新
        log.info("检测到网关代码变化: %s，热重启中…", changed)
        self._stop_server()
        time.sleep(PORT_RELEASE_WAIT)
        if not self._start_server_with_retry():
            log.error("热重启：真实端口重建失败（网关可能不可用），"
                      "请检查端口占用或重启网关")
            self._fail_until = now + FAIL_BACKOFF_S
