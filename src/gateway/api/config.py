# -*- coding: utf-8 -*-
"""gateway/api/config.py — 运行配置/密钥 API 蓝图。

包含：/api/runtime /api/env
"""

import os

from flask import Blueprint, jsonify, request

from ..runtime_schema import read_runtime, validate_runtime, write_runtime
from .common import mask, parse_env


def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    bp = Blueprint("config_api", __name__)

    @bp.route("/api/runtime", methods=["GET", "PUT"])
    def api_runtime():
        if request.method == "GET":
            return jsonify({"ok": True, "config": read_runtime(root)})
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False,
                            "error": "body must be a JSON object"}), 400
        err = validate_runtime(body)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        current = read_runtime(root)
        current.update(body)
        write_runtime(root, current)
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
            if value is None:
                # null = 删除该键（此前空值被当"保持不变"，删不掉 key）
                pairs.pop(key, None)
                continue
            if not isinstance(value, str):
                return jsonify({"ok": False,
                                "error": f"value for {key} must be string"}), 400
            # 拒绝换行/回车：否则 value 里带 \n 可伪造额外 KEY=VAL 行注入 .env
            if "\n" in value or "\r" in value:
                return jsonify({"ok": False,
                                "error": f"value for {key} 不能含换行符"}), 400
            if value:  # 空值 = 保持不变（脱敏展示）
                pairs[key] = value
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for k, v in pairs.items():
                f.write(f"{k}={v}\n")
        return jsonify({"ok": True, "keys": sorted(pairs)})

    return bp
