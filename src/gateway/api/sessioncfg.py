# -*- coding: utf-8 -*-
"""gateway/api/sessioncfg.py — 每会话决策模型/提示配置 API 蓝图。

存储：workspace/runtime/session_config.json（proxy 热读，改完即生效）
格式：{"<会话名>": {"provider": "kimi|deepseek|gemini", "model": "...",
                   "include_memory": bool, "include_history": bool,
                   "goal": "..."}}

路由：
  /api/session_config            GET 全量 + 可用模型清单
  /api/session_config            PUT 更新/清除某会话配置
"""

import json
import os

from flask import Blueprint, jsonify, request

from src.shared.model_catalog import (
    MODEL_CATALOG, VALID_PROVIDERS, is_valid_model)

from .common import list_sessions

CFG_REL = os.path.join("workspace", "runtime", "session_config.json")

# 可用 provider → 模型候选：收敛到 shared 单一事实来源（不再各自硬编码）
AVAILABLE_MODELS = MODEL_CATALOG

FIELDS = ("provider", "model", "include_memory", "include_history", "goal")


def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    bp = Blueprint("sessioncfg_api", __name__)

    def _path():
        return os.path.join(root, CFG_REL)

    def _read() -> dict:
        try:
            with open(_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(data: dict):
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

    @bp.route("/api/session_config", methods=["GET"])
    def api_session_config_get():
        # 会话名单：chatlog.db sessions 表（与 /api/groups 共用 list_sessions）
        return jsonify({
            "ok": True,
            "sessions": list_sessions(root),
            "configs": _read(),
            "available": AVAILABLE_MODELS,
        })

    @bp.route("/api/session_config", methods=["PUT"])
    def api_session_config_put():
        body = request.get_json(silent=True) or {}
        session = (body.get("session") or "").strip()
        if not session:
            return jsonify({"ok": False, "error": "缺 session"}), 400
        cfg = body.get("config")
        data = _read()
        if cfg is None or cfg == {}:
            # 清空该会话配置（回退全局）
            data.pop(session, None)
            _write(data)
            return jsonify({"ok": True, "session": session, "cleared": True})
        if not isinstance(cfg, dict):
            return jsonify({"ok": False, "error": "config 必须是对象"}), 400
        provider = (cfg.get("provider") or "").strip()
        model = (cfg.get("model") or "").strip()
        if provider and provider not in VALID_PROVIDERS:
            return jsonify({"ok": False,
                            "error": f"provider 必须是 "
                                     f"{'|'.join(VALID_PROVIDERS)}"}), 400
        if provider and not model:
            return jsonify({"ok": False, "error": "选了 provider 必须填 model"}), 400
        if provider and not is_valid_model(provider, model):
            return jsonify({"ok": False,
                            "error": f"model 不在 {provider} 目录里"}), 400
        clean = {}
        for k in FIELDS:
            if k in cfg:
                clean[k] = cfg[k]
        data[session] = clean
        _write(data)
        return jsonify({"ok": True, "session": session, "config": clean})

    return bp
