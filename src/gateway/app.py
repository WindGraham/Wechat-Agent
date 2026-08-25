# -*- coding: utf-8 -*-
"""gateway/app.py — 网关 Web 管理面（Flask）装配。

本文件只做"装配"：创建 Flask app、挂鉴权、注册 API 蓝图。
路由实现按域拆分在 api/ 各蓝图（agent/memory/config/live），
前端页面独立在 pages/index.html——改 UI 不碰 Python，加 API 域不碰本文件。

安全：默认只绑 127.0.0.1；环境变量 WECHAT_AGENT_GATEWAY_TOKEN 设置时，
所有请求必须带 ``Authorization: Bearer <token>``。
"""

import hmac
import logging
import os

from flask import Flask, jsonify, request

log = logging.getLogger("gateway")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# 允许编辑的文件根（相对 project_root），path 参数必须落在其中之一
EDITABLE_ROOTS = ("config/prompts", "config/personas")

# /workspace/<path> 静态路由只允许图片扩展名（crop_gallery 演示页的截图产物）
_WORKSPACE_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
# /workspace/<path> 静态路由拒绝这些目录前缀（防 .env / chatlog.db / 流水外泄）
_WORKSPACE_FORBIDDEN_PREFIXES = ("chatlogs/", "runtime/", "memory/", "media/")

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


def _load_page(name):
    """读取独立演示页（bookmark.html 等，每次读，可热改）。"""
    path = os.path.join(_PAGES_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return f"<html><body>页面文件缺失: pages/{name}</body></html>"


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

    static_folder = os.path.join(_PAGES_DIR, "static")
    app = Flask(__name__, static_folder=static_folder, static_url_path="/static")
    root = os.path.abspath(project_root or PROJECT_ROOT)
    app.config["PROJECT_ROOT"] = root

    # ------------------------------------------------------------- 鉴权
    token = os.environ.get("WECHAT_AGENT_GATEWAY_TOKEN") or None

    @app.before_request
    def _check_token():
        if token is None:
            return None
        auth = request.headers.get("Authorization", "")
        # 时序安全比较，避免普通 == 的逐字节短路（本地场景意义有限，但成本为零）
        expected = "Bearer " + token
        if not hmac.compare_digest(auth, expected):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return None

    # ------------------------------------------------------------- 页面与工作区文件路由
    @app.route("/")
    def index():
        return _load_index_html()

    @app.route("/bookmark")
    def bookmark_page():
        """书签展示页：每个群的书签全屏 + 内容区裁切（workspace/bookmarks/）。"""
        return _load_page("bookmark.html")

    @app.route("/scroll_replay")
    def scroll_replay_page():
        """滚动回放展示页：回溯采集每屏的原图/消息区/分割/重叠遮罩。"""
        return _load_page("scroll_replay.html")

    @app.route("/scroll_flow")
    def scroll_flow_page():
        """滚动连续流：两列蛇形（左偶右奇），从底部向上接，相邻屏重叠对齐。"""
        return _load_page("scroll_flow.html")

    @app.route("/api/scroll_replay")
    def api_scroll_replay():
        """滚动回放：无 ?r= 列出所有 replay；?r=<名字> 返回该 replay 的 manifest。"""
        import json as _json
        import time as _time
        rp_dir = os.path.join(root, "workspace", "replays")
        name = request.args.get("r", "")
        if name:
            p = os.path.join(rp_dir, name)
            if not os.path.isfile(os.path.join(p, "manifest.json")):
                return jsonify({"ok": False, "error": "replay 不存在"}), 404
            with open(os.path.join(p, "manifest.json"), encoding="utf-8") as f:
                m = _json.load(f)
            for s in m.get("screens", []):
                s["full"] = f"/workspace/replays/{name}/{s['full']}"
                s["crop"] = f"/workspace/replays/{name}/{s['crop']}"
                if s.get("stitch"):
                    s["stitch"] = f"/workspace/replays/{name}/{s['stitch']}"
            m["name"] = name
            return jsonify({"ok": True, "replay": m})
        replays = []
        if os.path.isdir(rp_dir):
            for n in sorted(os.listdir(rp_dir), reverse=True):
                p = os.path.join(rp_dir, n, "manifest.json")
                if not os.path.isfile(p):
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        m = _json.load(f)
                    replays.append({
                        "name": n,
                        "group": m.get("group", ""),
                        "screens": len(m.get("screens", [])),
                        "mtime": _time.strftime(
                            "%Y-%m-%d %H:%M:%S",
                            _time.localtime(os.path.getmtime(p))),
                    })
                except (OSError, ValueError):
                    continue
        return jsonify({"ok": True, "replays": replays})

    @app.route("/api/bookmarks")
    def api_bookmarks():
        """列出 workspace/bookmarks/<群>/ 下的书签展示副本（full.jpg/crop.jpg）。"""
        import json as _json
        import time as _time
        bm_dir = os.path.join(root, "workspace", "bookmarks")
        groups = []
        if os.path.isdir(bm_dir):
            for name in sorted(os.listdir(bm_dir)):
                d = os.path.join(bm_dir, name)
                if not os.path.isdir(d):
                    continue
                full = os.path.join(d, "full.jpg")
                crop = os.path.join(d, "crop.jpg")
                if not (os.path.exists(full) and os.path.exists(crop)):
                    continue
                # 裁切范围元数据（_save_anchor 写入）：供页面在整屏图上标绿框
                crop_top, crop_bottom, full_h = None, None, None
                meta_p = os.path.join(d, "meta.json")
                if os.path.exists(meta_p):
                    try:
                        with open(meta_p, encoding="utf-8") as f:
                            m = _json.load(f)
                        crop_top = m.get("crop_top")
                        crop_bottom = m.get("crop_bottom")
                        full_h = m.get("full_h")
                    except (OSError, ValueError):
                        pass
                groups.append({
                    "group": name,
                    "full": f"/workspace/bookmarks/{name}/full.jpg",
                    "crop": f"/workspace/bookmarks/{name}/crop.jpg",
                    "full_bytes": os.path.getsize(full),
                    "crop_bytes": os.path.getsize(crop),
                    "crop_top": crop_top,
                    "crop_bottom": crop_bottom,
                    "full_h": full_h,
                    "mtime": _time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        _time.localtime(os.path.getmtime(full))),
                })
        return jsonify({"ok": True, "groups": groups})

    @app.route("/workspace/<path:filename>")
    def workspace_static(filename):
        """只允许读取 workspace 下的图片（crop_gallery 演示页用）。

        历史上这里用 send_from_directory 把整个 workspace 裸挂为静态目录，
        `GET /workspace/chatlogs/chatlog.db`、`GET /workspace/.env` 可直接
        下载聊天库与密钥原文——本路由现已收紧：仅图片扩展名，且拒绝点文件、
        chatlogs/runtime/memory 等敏感子目录。
        """
        from flask import send_from_directory
        name = filename.replace("\\", "/")
        low = name.lower()
        # 仅图片扩展名
        if not low.endswith(_WORKSPACE_IMAGE_EXTS):
            return jsonify({"ok": False, "error": "not allowed"}), 404
        # 拒绝点文件段（含 .env）与敏感目录
        for seg in name.split("/"):
            if seg.startswith("."):
                return jsonify({"ok": False, "error": "not allowed"}), 404
        for prefix in _WORKSPACE_FORBIDDEN_PREFIXES:
            if name == prefix.rstrip("/") or name.startswith(prefix):
                return jsonify({"ok": False, "error": "not allowed"}), 404
        ws_dir = os.path.join(root, "workspace")
        return send_from_directory(ws_dir, name)

    # ------------------------------------------------------------- 蓝图注册
    ctx = {
        "root": root,
        "proxy": proxy,
        "supervisor": supervisor,
        "agent_callback_url": agent_callback_url,
        "reloader": reloader,
        "editable_roots": EDITABLE_ROOTS,
    }
    for bp in create_bps(ctx):
        app.register_blueprint(bp)

    return app
