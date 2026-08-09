# -*- coding: utf-8 -*-
"""gateway/supervisor.py — AgentSupervisor：agent 子进程管家。

网关独立常驻进程后，agent（python -m src.main）作为其子进程被管理。
本模块负责：
  - spawn：拉起 agent 子进程（stdout/stderr → logs/agent.log，轮转）
  - monitor：轮询子进程存活，崩溃标记状态（不自动重启，等手动）
  - stop / restart：优雅停止（SIGTERM → 超时 SIGKILL）+ 日志轮转重启
  - status：running/stopped/crashed + pid + 启动时间 + 退出码
  - 日志轮转逻辑与旧 restart.sh 一致（保留最近 5 份历史日志）

设计要点：
  - 本模块**不依赖 flask**，只依赖标准库 + subprocess——热重启 flask 模块时
    本模块实例常驻内存，agent 进程句柄不丢
  - 线程安全：所有状态变更加锁；monitor 线程与 API 线程并发访问安全
"""

import logging
import os
import shutil
import signal
import subprocess
import threading
import time

log = logging.getLogger("gateway.supervisor")

# 默认值（可在 AgentSupervisor 构造时覆盖，供测试注入假路径）
DEFAULT_PYTHON = os.path.expanduser("~/.venvs/wechat-agent/bin/python")
DEFAULT_MAIN = ["-m", "src.main"]

# 日志轮转：保留最近 KEEP_LOG_ROTATIONS 份历史
KEEP_LOG_ROTATIONS = 5
# 优雅停止等待秒数，超时 SIGKILL
STOP_GRACE_S = 10
# monitor 轮询间隔
MONITOR_POLL_S = 2.0


class AgentSupervisor:
    """管理单个 agent 子进程的生命周期。"""

    def __init__(self, python=DEFAULT_PYTHON, main_args=None,
                 workspace=None, config=None, logs_dir=None,
                 clock=time.time, sleep=time.sleep):
        self._python = python
        self._main_args = list(main_args if main_args is not None
                               else DEFAULT_MAIN)
        self._workspace = workspace      # None = 不传 --workspace
        self._config = config            # None = 不传 --config
        self._logs_dir = logs_dir or self._default_logs_dir()
        self._clock = clock
        self._sleep = sleep

        self._proc = None                # subprocess.Popen or None
        self._lock = threading.RLock()
        self._stop_monitor = threading.Event()
        self._monitor_thread = None
        self._last_exit = None           # {"code", "ts", "signal"}
        self._started_at = None          # 本次启动 epoch
        self._last_log_rotate = None     # 最近一次日志轮转文件名

        os.makedirs(self._logs_dir, exist_ok=True)

    # ---------------------------------------------------------------- 路径
    @staticmethod
    def _default_logs_dir():
        """仓库根 logs/ 目录（与 restart.sh 一致）。"""
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        return os.path.join(root, "logs")

    @property
    def log_path(self):
        return os.path.join(self._logs_dir, "agent.log")

    # ---------------------------------------------------------------- 公开 API
    def start(self, extra_args=None):
        """拉起 agent 子进程。已运行则返回 False。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                log.warning("agent 已在运行 (pid=%s)，忽略 start", self._proc.pid)
                return False
            self._rotate_log()
            cmd = [self._python] + self._main_args
            if self._workspace:
                cmd += ["--workspace", self._workspace]
            if self._config:
                cmd += ["--config", self._config]
            if extra_args:
                cmd += list(extra_args)
            log.info("agent start: %s", " ".join(cmd))
            try:
                f = open(self.log_path, "ab")
            except OSError as e:
                log.error("无法打开日志文件 %s: %s", self.log_path, e)
                return False
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=f, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                f.close()
                log.error("agent spawn 失败: %s", e)
                self._proc = None
                return False
            # f 由子进程继承，父进程侧关闭
            f.close()
            self._started_at = self._clock()
            self._last_exit = None
            self._ensure_monitor()
            return True

    def stop(self, grace_s=STOP_GRACE_S):
        """优雅停止：SIGTERM → 等待 grace_s → SIGKILL。返回是否停掉。"""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                return True
            pid = proc.pid
        log.info("agent stop: pid=%d (grace=%ds)", pid, grace_s)
        try:
            # start_new_session=True → 杀整个进程组（含 agent 的子任务）
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as e:
            log.warning("SIGTERM 失败: %s", e)
        deadline = self._clock() + grace_s
        while self._clock() < deadline:
            if proc.poll() is not None:
                break
            self._sleep(0.2)
        if proc.poll() is None:
            log.warning("agent 未在 %ds 内退出，SIGKILL", grace_s)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=5)
        with self._lock:
            self._proc = None
            self._monitor_stop()
        return True

    def restart(self):
        """轮转日志重启（等价旧 restart.sh）。返回 True 表示已拉起。"""
        self.stop()
        return self.start()

    def status(self) -> dict:
        """当前状态。running/stopped/crashed + pid + 启动时间 + 退出信息。
        退出码 0 视为正常结束（stopped），非 0 才标 crashed。"""
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                return {"state": "running", "pid": proc.pid,
                        "started_at": self._started_at}
            if self._last_exit is not None:
                state = "crashed" if self._last_exit.get("code") else "stopped"
                return {"state": state, "pid": None,
                        "started_at": self._started_at,
                        "exit": self._last_exit}
            return {"state": "stopped", "pid": None,
                    "started_at": None, "exit": self._last_exit}

    def logs_tail(self, n: int = 200) -> str:
        """agent.log 尾部 n 行（网关状态页展示）。"""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 65536))   # 先截 64KB 再按行
                data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    # ---------------------------------------------------------------- 内部
    def _rotate_log(self):
        """agent.log → agent.log.<MMDD-HHMMSS>，只保留最近 5 份。
        与旧 restart.sh 语义一致（直接覆盖会冲掉现场，2026-08-09 教训）。"""
        path = self.log_path
        if not os.path.exists(path):
            return
        stamp = time.strftime("%m%d-%H%M%S")
        rotated = f"{path}.{stamp}"
        try:
            os.replace(path, rotated)
            self._last_log_rotate = rotated
        except OSError as e:
            log.warning("日志轮转失败: %s", e)
            return
        # 清理超出保留份数的历史
        try:
            old = sorted(
                (p for p in os.listdir(self._logs_dir)
                 if p.startswith("agent.log.")),
                reverse=True)
            for name in old[KEEP_LOG_ROTATIONS:]:
                os.remove(os.path.join(self._logs_dir, name))
        except OSError:
            pass
        log.info("日志轮转: %s", rotated)

    def _ensure_monitor(self):
        """确保 monitor 线程在跑（进程退出时记录退出码状态）。"""
        if self._monitor_thread is not None and \
                self._monitor_thread.is_alive():
            return
        self._stop_monitor.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True,
            name="agent-supervisor-monitor")
        self._monitor_thread.start()

    def _monitor_loop(self):
        """轮询子进程；退出非 0 记 crashed。不自动重启（等手动）。"""
        while not self._stop_monitor.is_set():
            with self._lock:
                proc = self._proc
            if proc is None:
                return
            rc = proc.poll()
            if rc is not None:
                with self._lock:
                    self._last_exit = {"code": rc, "ts": self._clock()}
                    self._proc = None
                log.warning("agent 退出，code=%s", rc)
                return
            self._stop_monitor.wait(MONITOR_POLL_S)

    def _monitor_stop(self):
        self._stop_monitor.set()
        t = self._monitor_thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._monitor_thread = None

    # ---------------------------------------------------------------- 自测
    def __del__(self):
        try:
            self._monitor_stop()
        except Exception:  # noqa: BLE001
            pass
