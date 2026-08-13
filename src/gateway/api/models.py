# -*- coding: utf-8 -*-
"""gateway/api/models.py — 模型目录 API 蓝图（中心配置器的唯一读取入口）。

所有「选模型」的地方（全局决策模型、每会话模型）都从这里拿清单，
前端不再各自硬编码 DM_MODELS，后端不再各自硬编码 AVAILABLE_MODELS。

路由：
  /api/models    GET 目录 + 当前全局选择 + 每会话选择（聚合，前端一次拿全）
"""

import json
import os

from flask import Blueprint, jsonify

from src.shared.model_catalog import (
    DEFAULT_MODELS, DEFAULT_PROVIDER, MODEL_CATALOG, VALID_PROVIDERS)

from ..runtime_schema import read_runtime

SESSION_CONFIG_REL = os.path.join("workspace", "runtime", "session_config.json")


def _read_session_config(root):
    path = os.path.join(root, SESSION_CONFIG_REL)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    bp = Blueprint("models_api", __name__)

    @bp.route("/api/models", methods=["GET"])
    def api_models():
        rt = read_runtime(root)
        provider = rt.get("decision_provider") or DEFAULT_PROVIDER
        model = rt.get("decision_model") or DEFAULT_MODELS.get(provider, "")
        return jsonify({
            "ok": True,
            "catalog": MODEL_CATALOG,
            "defaults": DEFAULT_MODELS,
            "default_provider": DEFAULT_PROVIDER,
            "valid_providers": list(VALID_PROVIDERS),
            "global": {
                "provider": provider,
                "model": model,
                "token_floor": rt.get("decision_token_floor", 0),
                "token_ceiling": rt.get("decision_token_ceiling", 0),
            },
            "sessions": _read_session_config(root),
        })

    return bp
