# -*- coding: utf-8 -*-
"""gateway/app.py — 网关 Web 管理面（Flask）装配。

本文件只做"装配"：创建 Flask app、挂鉴权、注册 API 蓝图。
路由实现按域拆分在 api/ 各蓝图（agent/memory/config/live），
前端页面独立在 pages/index.html——改 UI 不碰 Python，加 API 域不碰本文件。

安全：默认只绑 127.0.0.1；环境变量 WECHAT_AGENT_GATEWAY_TOKEN 设置时，
所有请求必须带 ``Authorization: Bearer <token>``。
"""

import logging
import os

from flask import Flask, jsonify, request

log = logging.getLogger("gateway")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# 允许编辑的文件根（相对 project_root），path 参数必须落在其中之一
EDITABLE_ROOTS = ("config/prompts", "config/personas")

# runtime.json 字段表（CONTRACTS.md §五）：name -> 校验函数
_RUNTIME_FIELDS = {
    "max_concurrent_decisions": int,
    "media_convert_concurrency": int,
    "history_size": int,
    "action_max_attempts": int,
    "task_retention_days": int,
    "paused": bool,
    "muted_until": (int, float),
    "owner": str,
    "owner_nick": str,
    "tool_model": str,
}
_RUNTIME_INTERVAL_FIELDS = ("sweep_interval", "notify_interval")

# 前端页面（独立文件 pages/index.html）
_PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages")


def _load_index_html():
    """读取前端页面（每次读，开发时可热改页面文件）。"""
    path = os.path.join(_PAGES_DIR, "index.html")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "<html><body>页面文件缺失: pages/index.html</body></html>"


INDEX_HTML = _load_index_html()


def create_app(project_root=None, proxy=None, supervisor=None,
               agent_callback_url=None, reloader=None):
    """Flask 应用工厂。project_root 指向仓库根（含 config/ 与 workspace/），
    缺省取仓库根；测试传 tmp 目录副本。

    - proxy 注入后开放 /api/task_done（进程内任务完成回执注入，内嵌模式）
    - supervisor 注入后开放 /api/agent/*（agent 子进程管理，独立网关模式）
    - agent_callback_url：独立网关模式下 /api/task_done 转发目标
    - reloader：HotReloadServer 实例，开放 /api/gateway/reload
    """
    from .api import create_bps

    app = Flask(__name__)
    root = os.path.abspath(project_root or PROJECT_ROOT)
    app.config["PROJECT_ROOT"] = root

    # ------------------------------------------------------------- 鉴权
    token = os.environ.get("WECHAT_AGENT_GATEWAY_TOKEN") or None

    @app.before_request
    def _check_token():
        if token is None:
            return None
        auth = request.headers.get("Authorization", "")
        if auth != "Bearer " + token:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return None

    # ------------------------------------------------------------- 页面
    @app.route("/")
    def index():
        return _load_index_html()

    # ------------------------------------------------------------- 蓝图注册
    ctx = {
        "root": root,
        "proxy": proxy,
        "supervisor": supervisor,
        "agent_callback_url": agent_callback_url,
        "reloader": reloader,
        "editable_roots": EDITABLE_ROOTS,
        "runtime_fields": _RUNTIME_FIELDS,
        "runtime_interval_fields": _RUNTIME_INTERVAL_FIELDS,
    }
    for bp in create_bps(ctx):
        app.register_blueprint(bp)

    return app
