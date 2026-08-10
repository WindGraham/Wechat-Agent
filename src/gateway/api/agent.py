# -*- coding: utf-8 -*-
"""gateway/api/agent.py — agent 管理与文件 API 蓝图。

包含：/api/task_done /api/agent/* /api/gateway/reload /api/files
"""

import os

from flask import Blueprint, jsonify, request

from .common import list_personas, list_prompts, resolve_editable


def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    proxy = ctx.get("proxy")
    supervisor = ctx.get("supervisor")
    agent_callback_url = ctx.get("agent_callback_url")
    reloader = ctx.get("reloader")
    editable_roots = ctx.get("editable_roots", ())

    bp = Blueprint("agent_api", __name__)

    @bp.route("/api/task_done", methods=["POST"])
    def api_task_done():
        """进程外任务完成注入（内嵌直调 / 独立转发 agent 回调）。"""
        task_id = (request.get_json(silent=True) or {}).get("task_id", "")
        if not task_id:
            return jsonify({"ok": False, "error": "缺 task_id"}), 400
        if proxy is not None:
            return jsonify({"ok": bool(proxy.inject_task_done(task_id))})
        if not agent_callback_url:
            return jsonify({"ok": False, "error": "agent 未运行/未接线"}), 503
        try:
            import requests as _req
            r = _req.post(agent_callback_url.rstrip("/") + "/task_done",
                          json={"task_id": task_id}, timeout=5)
            if r.status_code == 200:
                return jsonify(r.json())
            return jsonify({"ok": False,
                            "error": f"agent 回调失败 HTTP {r.status_code}"}), 502
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False,
                            "error": f"agent 回调异常: {type(e).__name__}"}), 502

    @bp.route("/api/agent/status")
    def api_agent_status():
        if supervisor is None:
            return jsonify({"ok": True, "mode": "embedded"})
        return jsonify({"ok": True, "mode": "supervised",
                        "agent": supervisor.status()})

    @bp.route("/api/agent/start", methods=["POST"])
    def api_agent_start():
        if supervisor is None:
            return jsonify({"ok": False, "error": "网关未以独立模式运行"}), 400
        ok = supervisor.start()
        return jsonify({"ok": ok, "agent": supervisor.status()})

    @bp.route("/api/agent/stop", methods=["POST"])
    def api_agent_stop():
        if supervisor is None:
            return jsonify({"ok": False, "error": "网关未以独立模式运行"}), 400
        supervisor.stop()
        return jsonify({"ok": True, "agent": supervisor.status()})

    @bp.route("/api/agent/restart", methods=["POST"])
    def api_agent_restart():
        if supervisor is None:
            return jsonify({"ok": False, "error": "网关未以独立模式运行"}), 400
        ok = supervisor.restart()
        return jsonify({"ok": ok, "agent": supervisor.status()})

    @bp.route("/api/agent/logs")
    def api_agent_logs():
        if supervisor is None:
            return jsonify({"ok": False, "error": "网关未以独立模式运行"}), 400
        try:
            n = int(request.args.get("n", "200"))
        except ValueError:
            n = 200
        return jsonify({"ok": True, "log": supervisor.logs_tail(n)})

    @bp.route("/api/gateway/reload", methods=["POST"])
    def api_gateway_reload():
        """手动刷新网关（只重载网关代码，不影响 agent）。"""
        if reloader is None:
            return jsonify({"ok": False,
                            "error": "网关未注入热重启能力（内嵌模式？）"}), 400
        try:
            r = reloader.reload_now()
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False,
                            "error": f"刷新异常: {type(e).__name__}"}), 500
        status = 200 if r.get("ok") else 400
        return jsonify(r), status

    @bp.route("/api/files")
    def api_files():
        which = request.args.get("dir", "")
        if which == "prompts":
            return jsonify({"ok": True, "files": list_prompts(root)})
        if which == "personas":
            return jsonify({"ok": True, "files": list_personas(root)})
        return jsonify({"ok": False,
                        "error": "dir must be prompts|personas"}), 400

    @bp.route("/api/file", methods=["GET", "PUT"])
    def api_file():
        rel = request.args.get("path", "")
        full = resolve_editable(rel, root, editable_roots)
        if full is None:
            return jsonify({"ok": False, "error": "path not allowed"}), 403
        if request.method == "GET":
            if not os.path.isfile(full):
                return jsonify({"ok": False, "error": "not found"}), 404
            with open(full, "r", encoding="utf-8") as f:
                content = f.read()
            return jsonify({"ok": True, "path": rel, "content": content,
                            "mtime": os.path.getmtime(full)})
        body = request.get_json(silent=True) or {}
        content = body.get("content")
        if not isinstance(content, str):
            return jsonify({"ok": False,
                            "error": "content must be a string"}), 400
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"ok": True, "path": rel,
                        "mtime": os.path.getmtime(full)})

    return bp
