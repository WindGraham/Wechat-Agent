# -*- coding: utf-8 -*-
"""gateway/api/memory.py — 记忆文件 API 蓝图。

包含：/api/memory/files（列表，新文件自动冒顶） /api/memory/file（详情）
"""

import json
import os

from flask import Blueprint, jsonify, request


def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    bp = Blueprint("memory_api", __name__)
    mem_root = os.path.join(root, "workspace", "memory")

    @bp.route("/api/memory/files")
    def api_memory_files():
        """memory 文件列表（按 mtime 倒序：新增/更新的记忆自动冒顶）。"""
        files = []
        if os.path.isdir(mem_root):
            for dirpath, _dirs, fnames in os.walk(mem_root):
                for fn in fnames:
                    if not fn.endswith(".json"):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, mem_root).replace(os.sep, "/")
                    if rel == "global.json":
                        kind = "global"
                    elif rel.startswith("users/"):
                        kind = "user"
                    elif rel.startswith("sessions/"):
                        kind = "session"
                    else:
                        kind = "other"
                    name = fn[:-5]
                    try:
                        with open(full, encoding="utf-8") as f:
                            data = json.load(f)
                        count = len(data.get("facts", [])) if isinstance(
                            data, dict) else 0
                    except Exception:  # noqa: BLE001
                        count = 0
                    files.append({"path": rel, "name": name, "kind": kind,
                                  "mtime": os.path.getmtime(full),
                                  "count": count})
        files.sort(key=lambda f: -f["mtime"])
        return jsonify({"ok": True, "files": files})

    @bp.route("/api/memory/file")
    def api_memory_file():
        """单个 memory 文件内容（含 facts 结构化展示）。"""
        rel = request.args.get("path", "")
        full = os.path.realpath(os.path.join(mem_root, rel))
        if not full.startswith(os.path.realpath(mem_root) + os.sep) \
                and full != os.path.realpath(mem_root):
            return jsonify({"ok": False, "error": "path not allowed"}), 403
        if not os.path.isfile(full):
            return jsonify({"ok": False, "error": "not found"}), 404
        try:
            with open(full, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return jsonify({"ok": False, "error": "parse failed"}), 500
        return jsonify({"ok": True, "path": rel, "data": data,
                        "mtime": os.path.getmtime(full)})

    return bp
