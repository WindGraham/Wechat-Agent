# -*- coding: utf-8 -*-
"""gateway/api/live.py — 实况/状态 API 蓝图。

包含：/api/status /api/events /api/ops /api/home_scan /api/groups
"""

import os

from flask import Blueprint, jsonify, request

from .common import list_tasks, read_json, read_jsonl_tail


def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    agent_callback_url = ctx.get("agent_callback_url") or ""
    bp = Blueprint("live_api", __name__)

    def _agent_current() -> dict:
        """从 agent 回调端口拿当前执行条目（独立网关模式）。

        失败返回 None——实况页降级为只显示队列快照（不显示"正在执行"）。"""
        if not agent_callback_url:
            return None
        try:
            import requests as _req
            r = _req.get(agent_callback_url.rstrip("/") + "/status",
                         timeout=3)
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:  # noqa: BLE001
            return None

    @bp.route("/api/status")
    def api_status():
        runtime_dir = os.path.join(root, "workspace", "runtime")
        data = {
            "ok": True,
            "queue": read_json(os.path.join(runtime_dir, "queue.json")),
            "watermarks": read_json(os.path.join(runtime_dir,
                                                 "watermarks.json")),
            "tasks": list_tasks(os.path.join(root, "workspace", "tasks")),
        }
        agent_st = _agent_current()
        if agent_st and agent_st.get("current"):
            data["current"] = agent_st["current"]
        return jsonify(data)

    @bp.route("/api/events")
    def api_events():
        """proxy_events.jsonl 尾部 n 条（倒序：最新在前）。"""
        try:
            n = int(request.args.get("n", "50"))
        except ValueError:
            n = 50
        n = max(1, min(n, 500))
        path = os.path.join(root, "workspace", "runtime",
                            "proxy_events.jsonl")
        return jsonify({"ok": True, "events": read_jsonl_tail(path, n)})

    @bp.route("/api/ops")
    def api_ops():
        """interaction_ops.jsonl 原子操作流水尾部 n 条（倒序）。"""
        try:
            n = int(request.args.get("n", "50"))
        except ValueError:
            n = 50
        n = max(1, min(n, 500))
        path = os.path.join(root, "workspace", "runtime",
                            "interaction_ops.jsonl")
        return jsonify({"ok": True, "ops": read_jsonl_tail(path, n)})

    @bp.route("/api/home_scan")
    def api_home_scan():
        """首页红点扫描快照；未产生过时 scan 为 null。"""
        path = os.path.join(root, "workspace", "runtime", "home_scan.json")
        data = read_json(path)
        return jsonify({"ok": True,
                        "scan": data if isinstance(data, dict) else None})

    @bp.route("/api/groups", methods=["GET"])
    def api_groups():
        """列出所有会话及其热情度级别。"""
        from ..group_config import LEVELS, read_level
        personas_dir = os.path.join(root, "config", "personas")
        sessions = set()
        try:
            import sqlite3
            db = os.path.join(root, "workspace", "chatlogs", "chatlog.db")
            if os.path.exists(db):
                conn = sqlite3.connect(db)
                for r in conn.execute("SELECT name FROM sessions"):
                    sessions.add(r[0])
                conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            for f in os.listdir(personas_dir):
                if f.endswith(".yaml") and f not in ("default.yaml",
                                                    "tool_group.yaml"):
                    sessions.add(f[:-5])
        except OSError:
            pass
        groups = []
        for name in sorted(sessions):
            level, extra = read_level(personas_dir, name)
            groups.append({"session": name,
                           "level": level or "normal",
                           "extra_rule": extra})
        return jsonify({"ok": True, "groups": groups,
                        "levels": {k: {"label": v["label"],
                                       "desc": v["desc"]}
                                   for k, v in LEVELS.items()}})

    @bp.route("/api/groups", methods=["PUT"])
    def api_groups_put():
        """设置某会话的热情度（写入人格卡，下次决策热生效）。"""
        from ..group_config import write_level
        body = request.get_json(silent=True) or {}
        session = (body.get("session") or "").strip()
        level = (body.get("level") or "").strip()
        extra = body.get("extra_rule", "")
        if not session:
            return jsonify({"ok": False, "error": "缺 session"}), 400
        try:
            path = write_level(os.path.join(root, "config", "personas"),
                               session, level, extra)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "path": os.path.basename(path)})

    return bp
