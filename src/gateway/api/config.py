# -*- coding: utf-8 -*-
"""gateway/api/config.py — 运行配置/密钥 API 蓝图。

包含：/api/runtime /api/env
"""

import json
import os

from flask import Blueprint, jsonify, request

from .common import mask, parse_env, read_json


def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    runtime_fields = ctx.get("runtime_fields", {})
    runtime_interval_fields = ctx.get("runtime_interval_fields", ())
    bp = Blueprint("config_api", __name__)

    def _validate_runtime(body):
        """按字段表校验 runtime.json；返回错误信息或 None。"""
        for key, value in body.items():
            if key in runtime_interval_fields:
                if (not isinstance(value, (list, tuple)) or len(value) != 2
                        or not all(isinstance(v, (int, float))
                                   and not isinstance(v, bool)
                                   for v in value)):
                    return f"{key} must be [min, max] numbers"
                continue
            expected = runtime_fields.get(key)
            if expected is None:
                return f"unknown field: {key}"
            if isinstance(value, bool) and expected is not bool:
                return f"{key} must be {expected}"
            if not isinstance(value, expected):
                return f"{key} must be {expected}"
        return None

    @bp.route("/api/runtime", methods=["GET", "PUT"])
    def api_runtime():
        path = os.path.join(root, "config", "runtime.json")
        if request.method == "GET":
            data = read_json(path)
            return jsonify({"ok": True,
                            "config": data if isinstance(data, dict) else {}})
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False,
                            "error": "body must be a JSON object"}), 400
        err = _validate_runtime(body)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        current = read_json(path)
        if not isinstance(current, dict):
            current = {}
        current.update(body)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return jsonify({"ok": True, "config": current})

    @bp.route("/api/env", methods=["GET", "PUT"])
    def api_env():
        path = os.path.join(root, "workspace", ".env")
        if request.method == "GET":
            pairs = parse_env(path) if os.path.isfile(path) else {}
            keys = [{"key": k, "masked": mask(v)} for k, v in pairs.items()]
            return jsonify({"ok": True, "keys": keys})
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False,
                            "error": "body must be a JSON object"}), 400
        pairs = parse_env(path) if os.path.isfile(path) else {}
        for key, value in body.items():
            if not key or "=" in key or any(c.isspace() for c in key):
                return jsonify({"ok": False,
                                "error": f"bad key: {key!r}"}), 400
            if not isinstance(value, str):
                return jsonify({"ok": False,
                                "error": f"value for {key} must be string"}), 400
            if value:  # 空值 = 保持不变（脱敏展示）
                pairs[key] = value
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for k, v in pairs.items():
                f.write(f"{k}={v}\n")
        return jsonify({"ok": True, "keys": sorted(pairs)})

    return bp
