# -*- coding: utf-8 -*-
"""proxy/cli_backend.py — CLI 后端适配（工具层调用）。

CLIBackend 接口 + KimiCodeCLI 实现（kimi -p 无头 + stream-json 解析）。
新增框架只需实现同一接口注册进来。
"""

import json
import logging
import os
import subprocess
import threading
import time

from ...shared.types import TaskResult

log = logging.getLogger("decision.proxy.cli")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
TASKS_ROOT = os.path.join(PROJECT_ROOT, "workspace", "tasks")


class CLIBackend:
    """工具层 CLI 后端接口。"""
    name = "base"

    def run(self, brief: str, workdir: str, model: str = None,
            timeout_s: int = 600) -> TaskResult:
        raise NotImplementedError


class KimiCodeCLI(CLIBackend):
    """Kimi Code CLI 无头调用。

    返回解析规则（实测 v0.34.0）：stdout JSONL 逐行——
    最后一条带 content 的 assistant 消息 = 产出；meta.session_id 供追问；
    退出码 0 = 成功。思考/进度在 stderr，丢弃。
    """

    name = "kimi-code"

    def __init__(self, cli_path: str = "kimi",
                 default_model: str = "kimi-code/k3"):
        self._cli = cli_path
        self._default_model = default_model

    def run(self, brief: str, workdir: str, model: str = None,
            timeout_s: int = 600, cli_session_id: str = "") -> TaskResult:
        cmd = [self._cli]
        if cli_session_id:
            cmd += ["-r", cli_session_id]
        cmd += ["-m", model or self._default_model,
                "-p", brief, "--output-format", "stream-json"]

        os.makedirs(workdir, exist_ok=True)
        trace_path = os.path.join(workdir, "trace.jsonl")
        result_text, sid = "", cli_session_id
        trace_lines = []
        try:
            proc = subprocess.Popen(
                cmd, cwd=workdir,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True)
        except OSError as e:
            return TaskResult(ok=False, summary=f"CLI 启动失败: {e}")

        deadline = time.time() + timeout_s
        timed_out = False
        # 读出行线程 + 队列：进程不吐行时 select 式等待也要能判超时
        # （旧实现 for line in proc.stdout 在子进程静默挂起时永远阻塞，
        # 超时形同虚设——2026-08-08 五个任务卡 running 的教训）
        import queue as _queue
        q = _queue.Queue()

        def _pump():
            for ln in proc.stdout:
                q.put(ln)
            q.put(None)

        threading.Thread(target=_pump, daemon=True).start()
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    proc.kill()
                    timed_out = True
                    break
                try:
                    line = q.get(timeout=min(remaining, 5.0))
                except _queue.Empty:
                    continue
                if line is None:               # EOF：进程输出关闭
                    break
                trace_lines.append(line)
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("role") == "assistant" and msg.get("content"):
                    result_text = msg["content"]
                elif msg.get("role") == "meta" and msg.get("session_id"):
                    sid = msg["session_id"]
        finally:
            try:
                with open(trace_path, "w", encoding="utf-8") as f:
                    f.writelines(trace_lines)
            except OSError:
                pass
        rc = proc.wait()

        if timed_out:
            return TaskResult(ok=False, summary="任务超时",
                              cli_session_id=sid, trace_path=trace_path)
        ok = rc == 0 and bool(result_text)
        return TaskResult(ok=ok, summary=result_text,
                          cli_session_id=sid, trace_path=trace_path)


# ------------------------------------------------------------------ 注册表
_BACKENDS = {KimiCodeCLI.name: KimiCodeCLI}


def get_backend(name: str = "kimi-code", **kw) -> CLIBackend:
    return _BACKENDS[name](**kw)


def register_backend(cls):
    _BACKENDS[cls.name] = cls
