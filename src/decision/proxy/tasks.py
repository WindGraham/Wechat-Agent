# -*- coding: utf-8 -*-
"""proxy/tasks.py — 任务台账：subprocess 全生命周期登记。

一任务一目录：workspace/tasks/<日期>/<task_id>_<会话>_<refs>_<描述>/
task.json 是事实源，Proxy 重启后可从目录重建。
"""

import json
import logging
import os
import re
import time

log = logging.getLogger("decision.proxy.tasks")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
TASKS_ROOT = os.path.join(PROJECT_ROOT, "workspace", "tasks")


def _slug(s: str, n: int = 20) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s or "")
    return s[:n] or "task"


class TaskLedger:
    """任务台账：登记 task_id → 会话/消息归属/描述/状态。"""

    def __init__(self, tasks_root=TASKS_ROOT, clock=time.time):
        self._root = tasks_root
        self._clock = clock
        self._seq = 0
        self._tasks = {}            # task_id -> dict
        os.makedirs(tasks_root, exist_ok=True)
        self._rebuild()

    def _rebuild(self):
        """从目录重建台账（task.json 是事实源）。上次进程死掉时 running
        状态的任务永远等不到回执——标 interrupted，防止被当成还在执行。"""
        for day in os.listdir(self._root):
            day_dir = os.path.join(self._root, day)
            if not os.path.isdir(day_dir):
                continue
            for dirname in os.listdir(day_dir):
                path = os.path.join(day_dir, dirname, "task.json")
                try:
                    with open(path, encoding="utf-8") as f:
                        task = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                if task.get("status") == "running":
                    task["status"] = "interrupted"
                    try:
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(task, f, ensure_ascii=False, indent=2)
                    except OSError:
                        pass
                self._tasks[task["task_id"]] = task
        if self._tasks:
            log.info("ledger rebuilt: %d task(s) from disk", len(self._tasks))

    def find_similar(self, session: str, desc: str, within_s: int = 600):
        """同会话、描述相似（归一化后互为子串）、且仍在执行或 within_s
        内才完成的任务——用于委派前去重（2026-08-08 海报重复委派实测）。"""
        import re as _re
        nd = _re.sub(r"\W+", "", desc or "")
        if not nd:
            return None
        now = self._clock()
        for t in self._tasks.values():
            if t["session"] != session:
                continue
            if t["status"] == "running":
                pass
            elif t["status"] == "done" and t.get("finished_at") \
                    and now - t["finished_at"] <= within_s:
                pass
            else:
                continue
            nt = _re.sub(r"\W+", "", t.get("desc") or "")
            if nt and (nt in nd or nd in nt):
                return t
        return None

    def register(self, session: str, refs: list, ref_briefs: list,
                 desc: str, deliver: str) -> dict:
        """登记新任务，建目录，写 task.json。返回 task 记录。"""
        self._seq += 1
        task_id = f"t{int(self._clock()) % 100000:05d}{self._seq:02d}"
        day = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
        dirname = f"{task_id}_{_slug(session)}_{'+'.join(refs) or 'na'}_{_slug(desc)}"
        workdir = os.path.join(self._root, day, dirname)
        os.makedirs(os.path.join(workdir, "files"), exist_ok=True)

        task = {
            "task_id": task_id, "session": session, "refs": refs,
            "ref_briefs": ref_briefs, "desc": desc, "deliver": deliver,
            "status": "running", "started_at": self._clock(),
            "finished_at": None, "cli_session_id": "", "workdir": workdir,
        }
        self._tasks[task_id] = task
        self._save(task)
        log.info("task registered: %s session=%s refs=%s", task_id, session, refs)
        return task

    def finish(self, task_id: str, ok: bool, cli_session_id: str = ""):
        task = self._tasks.get(task_id)
        if not task:
            return
        task["status"] = "done" if ok else "failed"
        task["finished_at"] = self._clock()
        task["cli_session_id"] = cli_session_id
        self._save(task)
        log.info("task %s: %s", task_id, task["status"])

    def get(self, task_id: str):
        return self._tasks.get(task_id)

    def running(self) -> list:
        return [t for t in self._tasks.values() if t["status"] == "running"]

    def running_for(self, session: str) -> list:
        """某会话执行中的任务（prompt 实时板块用）。"""
        return [t for t in self.running() if t["session"] == session]

    def _save(self, task: dict):
        try:
            path = os.path.join(task["workdir"], "task.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False, indent=2)
        except OSError:
            log.exception("task.json 写入失败: %s", task.get("task_id"))
