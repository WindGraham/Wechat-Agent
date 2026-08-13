# -*- coding: utf-8 -*-
"""gateway/api/common.py — 蓝图共享工具（helpers）。

从 app.py 拆出：所有 API 蓝图共用的文件读写/校验函数。
"""

import json
import os


def read_json(path):
    """读 JSON 文件；不存在/解析失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_jsonl_tail(path, n):
    """读 jsonl 尾部 n 条（倒序：最新在前）；不存在返回 []，坏行跳过。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    events = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
        if len(events) >= n:
            break
    return events


def list_prompts(root):
    """config/prompts/ 下的块文件：order.txt 顺序在前（标注分组与序号）。"""
    base = os.path.join(root, "config", "prompts")
    order_names = []
    order_path = os.path.join(base, "order.txt")
    if os.path.isfile(order_path):
        with open(order_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    order_names.append(line)

    files = []
    if os.path.isfile(order_path):
        files.append({"path": "config/prompts/order.txt", "name": "order.txt",
                      "group": "order", "order": 0,
                      "mtime": os.path.getmtime(order_path)})
    seen = set()
    for i, name in enumerate(order_names):
        full = os.path.join(base, name)
        if os.path.isfile(full):
            seen.add(os.path.normpath(name))
            files.append({"path": f"config/prompts/{name}", "name": name,
                          "group": name.split("/")[0], "order": i + 1,
                          "mtime": os.path.getmtime(full)})
    if os.path.isdir(base):
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, fn), base)
                rel = rel.replace(os.sep, "/")
                if rel == "order.txt" or os.path.normpath(rel) in seen:
                    continue
                full = os.path.join(base, rel)
                files.append({"path": f"config/prompts/{rel}", "name": rel,
                              "group": rel.split("/")[0], "order": None,
                              "mtime": os.path.getmtime(full)})
    return files


def list_personas(root):
    """config/personas/ 下的人格卡文件。"""
    base = os.path.join(root, "config", "personas")
    files = []
    if os.path.isdir(base):
        for fn in sorted(os.listdir(base)):
            full = os.path.join(base, fn)
            if os.path.isfile(full):
                files.append({"path": f"config/personas/{fn}", "name": fn,
                              "group": "persona", "order": None,
                              "mtime": os.path.getmtime(full)})
    return files


def list_tasks(tasks_dir):
    """workspace/tasks/<日期>/<任务目录>/task.json 的 status 摘要。"""
    tasks = []
    if not os.path.isdir(tasks_dir):
        return tasks
    for date in sorted(os.listdir(tasks_dir), reverse=True):
        date_dir = os.path.join(tasks_dir, date)
        if not os.path.isdir(date_dir):
            continue
        for name in sorted(os.listdir(date_dir)):
            ledger = read_json(os.path.join(date_dir, name, "task.json"))
            if not isinstance(ledger, dict):
                continue
            tasks.append({
                "date": date, "dir": name,
                "task_id": ledger.get("task_id", ""),
                "session": ledger.get("session", ""),
                "desc": ledger.get("desc", ""),
                "status": ledger.get("status", "unknown"),
                "started_at": ledger.get("started_at"),
                "finished_at": ledger.get("finished_at"),
            })
    return tasks


def list_sessions(root):
    """chatlog.db sessions 表里的会话名（去重排序）。失败返回空列表。"""
    sessions = set()
    try:
        import sqlite3
        db = os.path.join(root, "workspace", "chatlogs", "chatlog.db")
        if os.path.exists(db):
            conn = sqlite3.connect(db)
            try:
                for r in conn.execute("SELECT name FROM sessions"):
                    sessions.add(r[0])
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        pass
    return sorted(sessions)


def list_group_sessions(root):
    """会话名 = chatlog.db sessions + personas 卡（排除默认/工具卡）。

    /api/groups 与 /api/session_config 的历史实现各复制了一遍 SQLite
    查询，这里统一；personas 卡也代表一个「被配置过热度的群」。
    """
    sessions = set(list_sessions(root))
    personas_dir = os.path.join(root, "config", "personas")
    try:
        for f in os.listdir(personas_dir):
            if f.endswith(".yaml") and f not in ("default.yaml",
                                                 "tool_group.yaml"):
                sessions.add(f[:-5])
    except OSError:
        pass
    return sorted(sessions)


def parse_env(path):
    """解析 KEY=VALUE 行（忽略注释与空行），保持出现顺序。"""
    pairs = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
    return pairs


def mask(value):
    """脱敏：只显示前 4 位 + 后 2 位；太短则全隐。"""
    if len(value) <= 6:
        return "****"
    return value[:4] + "****" + value[-2:]


def resolve_editable(rel_path, root, editable_roots):
    """把相对路径限定在可编辑根内；越界返回 None。"""
    if not rel_path or os.path.isabs(rel_path):
        return None
    full = os.path.realpath(os.path.join(root, rel_path))
    for editable in editable_roots:
        base = os.path.realpath(os.path.join(root, editable))
        if full == base or full.startswith(base + os.sep):
            return full
    return None
