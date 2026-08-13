# -*- coding: utf-8 -*-
"""gateway/api/agent.py — agent 管理与文件 API 蓝图。

包含：/api/task_done /api/agent/* /api/gateway/reload /api/files
"""

import os

from flask import Blueprint, jsonify, request

from src.shared.model_catalog import VALID_PROVIDERS, is_valid_model

from ..runtime_schema import MODEL_FIELDS, read_runtime, write_runtime
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

    @bp.route("/api/aside", methods=["POST"])
    def api_aside():
        """旁注：向指定会话注入一条消息，触发一次带该消息的决策
        （网关 UI → agent proxy 的直接输入通道，2026-08-10 用户要求）。"""
        body = request.get_json(silent=True) or {}
        session = (body.get("session") or "").strip()
        text = (body.get("text") or "").strip()
        if not session or not text:
            return jsonify({"ok": False, "error": "缺 session/text"}), 400
        if proxy is not None:
            return jsonify({"ok": bool(proxy.inject_aside(
                session, text, sender=body.get("sender") or None))})
        if not agent_callback_url:
            return jsonify({"ok": False, "error": "agent 未运行/未接线"}), 503
        try:
            import requests as _req
            r = _req.post(agent_callback_url.rstrip("/") + "/aside",
                          json={"session": session, "text": text,
                                "sender": body.get("sender") or None},
                          timeout=5)
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
        file = request.args.get("file", "")
        if file:
            log = supervisor.logs_tail_file(file, n)
        else:
            log = supervisor.logs_tail(n)
        return jsonify({"ok": True, "log": log})

    @bp.route("/api/agent/logs_files")
    def api_agent_logs_files():
        """日志文件清单（当前 + 历史轮转），供控制台切换查看重启前的日志。"""
        if supervisor is None:
            return jsonify({"ok": False, "error": "网关未以独立模式运行"}), 400
        return jsonify({"ok": True, "files": supervisor.logs_list()})

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

    @bp.route("/api/workspace")
    def api_workspace():
        """工作区目录浏览（只读）：递归列 dir 下的文件（含子目录）。

        用于网关"文件"页浏览 workspace/ config/ docs/ 全部内容。
        安全：只读（不提供写）；路径限定在 root 内，防目录穿越。"""
        rel = request.args.get("dir", "")
        # 只允许浏览这些顶层（防越权看系统文件）
        allowed_prefixes = ("workspace", "config", "docs")
        rel = rel.strip().strip("/")
        if rel and not rel.startswith(allowed_prefixes):
            return jsonify({"ok": False,
                            "error": "dir must be under workspace|config|docs"}), 400
        base = os.path.realpath(root)
        full = os.path.realpath(os.path.join(root, rel)) if rel else base
        if full != base and not full.startswith(base + os.sep):
            return jsonify({"ok": False, "error": "path not allowed"}), 403
        if not os.path.isdir(full):
            return jsonify({"ok": False, "error": "not a directory"}), 404
        items = []
        for name in sorted(os.listdir(full)):
            p = os.path.join(full, name)
            if name.startswith(".") and name not in (".env",):
                continue
            try:
                st = os.stat(p)
                items.append({
                    "name": name,
                    "is_dir": os.path.isdir(p),
                    "size": st.st_size if os.path.isfile(p) else None,
                    "mtime": st.st_mtime,
                })
            except OSError:
                continue
        return jsonify({"ok": True, "dir": rel or ".", "items": items})

    @bp.route("/api/workspace_file", methods=["GET"])
    def api_workspace_file():
        """读工作区文本文件（只读）。与 /api/workspace 同源白名单：
        只允许 workspace|config|docs 三根，且限定在 root 内（防目录穿越
        与越权读 src/、tools/ 等仓库内其它文件）。"""
        rel = request.args.get("path", "")
        rel = rel.strip().strip("/")
        if not rel:
            return jsonify({"ok": False, "error": "缺 path"}), 400
        # 与 /api/workspace 列表一致：只允许这三类顶层目录
        if not rel.startswith(("workspace", "config", "docs")):
            return jsonify({"ok": False, "error": "path not allowed"}), 403
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(os.path.realpath(root) + os.sep):
            return jsonify({"ok": False, "error": "path not allowed"}), 403
        if not os.path.isfile(full):
            return jsonify({"ok": False, "error": "not found"}), 404
        try:
            with open(full, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return jsonify({"ok": False,
                            "error": f"读取失败: {type(e).__name__}"}), 400
        return jsonify({"ok": True, "path": rel, "content": content,
                        "mtime": os.path.getmtime(full)})

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

    # ---------------------------------------------------------- 决策模型切换
    # 字段表/读写已收敛到 runtime_schema.py（与 /api/runtime 同源，不再各写一份）

    def _agent_model_call(method, body=None):
        """转发到 agent 回调端口 /decision_model；返回 (resp_dict, error)。"""
        if not agent_callback_url:
            return None, "agent 未运行/未接线"
        try:
            import requests as _req
            url = agent_callback_url.rstrip("/") + "/decision_model"
            if method == "GET":
                r = _req.get(url, timeout=5)
            else:
                r = _req.post(url, json=body, timeout=8)
            if r.status_code == 200:
                return r.json(), None
            try:
                return None, r.json().get("error") or f"HTTP {r.status_code}"
            except ValueError:
                return None, f"agent 回调失败 HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            return None, f"agent 不可达: {type(e).__name__}"

    @bp.route("/api/decision_model", methods=["GET", "POST"])
    def api_decision_model():
        """决策模型切换（2026-08-11 用户要求）。

        GET：runtime.json 里的持久配置 + agent 实况（live，agent 在线时）。
        POST：校验 → 持久化 runtime.json（重启后仍生效）→ 转发 agent
        热切换（不重启）；agent 不在线则只持久化，标注重启后生效。
        """
        if request.method == "GET":
            cfg = {k: read_runtime(root).get(k) for k in MODEL_FIELDS}
            live, _err = _agent_model_call("GET")
            return jsonify({"ok": True, "config": cfg,
                            "live": (live or {}).get("provider"),
                            "agent_online": live is not None})

        body = request.get_json(silent=True)
        if body is None:
            # 容错：客户端没设 Content-Type 时手动解析原始 body
            # （2026-08-11 网关 UI 实测踩坑：get_json 静默吞成 None，
            #  报"provider 必须是 kimi|deepseek"误导排查）
            import json as _json
            try:
                body = _json.loads(request.get_data(as_text=True) or "")
            except ValueError:
                body = None
        if not isinstance(body, dict):
            body = {}
        provider = (body.get("provider") or "").strip()
        model = (body.get("model") or "").strip()
        try:
            floor = int(body.get("token_floor") or 0)
            ceiling = int(body.get("token_ceiling") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": "token_floor/ceiling 必须是整数"}), 400
        if provider not in VALID_PROVIDERS:
            return jsonify({"ok": False,
                            "error": f"provider 必须是 {'|'.join(VALID_PROVIDERS)}"}), 400
        if not model:
            return jsonify({"ok": False, "error": "缺 model"}), 400
        if not is_valid_model(provider, model):
            return jsonify({"ok": False,
                            "error": f"model 不在 {provider} 目录里（见 model_catalog）"}), 400
        if floor < 0 or ceiling < 0:
            return jsonify({"ok": False,
                            "error": "token 上下限不能为负"}), 400
        if ceiling and floor and ceiling < floor:
            return jsonify({"ok": False,
                            "error": "token_ceiling 不能小于 token_floor"}), 400

        # 1. 持久化（重启后生效）
        cfg = read_runtime(root)
        cfg.update({"decision_provider": provider,
                    "decision_model": model,
                    "decision_token_floor": floor,
                    "decision_token_ceiling": ceiling})
        write_runtime(root, cfg)

        # 2. 热切换（不重启 agent）
        live, err = _agent_model_call("POST", {
            "provider": provider, "model": model,
            "token_floor": floor, "token_ceiling": ceiling})
        if err:
            return jsonify({"ok": True, "applied": False,
                            "note": f"已保存到 runtime.json；热切换未生效"
                                    f"（{err}），agent 重启后应用"})
        return jsonify({"ok": True, "applied": True,
                        "live": live.get("provider")})

    return bp
