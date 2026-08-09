# -*- coding: utf-8 -*-
"""gateway/app.py — 网关 Web 管理面（Flask）。

按 docs/GATEWAY.md：网关只"读文件/写文件 + 读状态接口"，
不调用任何层的函数；配置变更只落文件，各层按 mtime 热读。

- Prompt 编辑器：config/prompts/（按 order.txt 顺序，标注 system/user 分组）
  与 config/personas/（yaml 人格卡）的查看/编辑
- 运行配置：config/runtime.json 表单化读写（字段按 CONTRACTS.md §五 白名单）
- 密钥：workspace/.env 查看（脱敏：前4后2）与更新；只落本机文件
- 状态页：workspace/runtime/ 的 queue.json / watermarks.json 与 tasks/ 台账摘要
- 实况页（默认首页）：时序队列 + home_scan.json 首页红点快照 +
  proxy_events.jsonl 决策事件流水（prompt/llm_output 可展开全文）

安全：默认只绑 127.0.0.1；环境变量 WECHAT_AGENT_GATEWAY_TOKEN 设置时，
所有请求必须带 ``Authorization: Bearer <token>``。
路径参数限定在 config/prompts、config/personas 白名单内，防目录穿越。
"""

import json
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


def create_app(project_root=None, proxy=None, supervisor=None,
               agent_callback_url=None, reloader=None):
    """Flask 应用工厂。project_root 指向仓库根（含 config/ 与 workspace/），
    缺省取仓库根；测试传 tmp 目录副本。

    - proxy 注入后开放 /api/task_done（进程内任务完成回执注入，内嵌模式）
    - supervisor 注入后开放 /api/agent/*（agent 子进程管理，独立网关模式）
    - agent_callback_url：独立网关模式下 /api/task_done 转发目标
      （agent 侧回调端口，默认 http://127.0.0.1:13015/task_done）
    - reloader：HotReloadServer 实例，注入后开放 /api/gateway/reload
      （手动"刷新网关"：只重载网关代码，不影响 agent）
    """
    app = Flask(__name__)
    root = os.path.abspath(project_root or PROJECT_ROOT)
    app.config["PROJECT_ROOT"] = root
    app.config["PROXY"] = proxy
    app.config["SUPERVISOR"] = supervisor
    app.config["AGENT_CALLBACK_URL"] = agent_callback_url
    app.config["RELOADER"] = reloader

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

    # ------------------------------------------------------------- 工具
    def _resolve_editable(rel_path):
        """把相对路径限定在 EDITABLE_ROOTS 内；越界返回 None。"""
        if not rel_path or os.path.isabs(rel_path):
            return None
        full = os.path.realpath(os.path.join(root, rel_path))
        for editable in EDITABLE_ROOTS:
            base = os.path.realpath(os.path.join(root, editable))
            if full == base or full.startswith(base + os.sep):
                return full
        return None

    # ------------------------------------------------------------- 页面
    @app.route("/")
    def index():
        return INDEX_HTML

    # -------------------------------------------------- 任务回执注入
    @app.route("/api/task_done", methods=["POST"])
    def api_task_done():
        """进程外完成的任务打入回执事件（让 agent 走正常回执流程自己交付）。
        body: {"task_id": "t..."}

        双模式：
        - 内嵌模式（proxy 已注入）：直接调 proxy.inject_task_done
        - 独立网关模式：转发到 agent 侧回调端口（agent_callback_url）
        """
        task_id = (request.get_json(silent=True) or {}).get("task_id", "")
        if not task_id:
            return jsonify({"ok": False, "error": "缺 task_id"}), 400
        p = app.config.get("PROXY")
        if p is not None:
            return jsonify({"ok": bool(p.inject_task_done(task_id))})
        url = app.config.get("AGENT_CALLBACK_URL")
        if not url:
            return jsonify({"ok": False, "error": "agent 未运行/未接线"}), 503
        try:
            import requests as _req
            r = _req.post(url.rstrip("/") + "/task_done",
                          json={"task_id": task_id}, timeout=5)
            if r.status_code == 200:
                return jsonify(r.json())
            return jsonify({"ok": False,
                            "error": f"agent 回调失败 HTTP {r.status_code}"}), 502
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False,
                            "error": f"agent 回调异常: {type(e).__name__}"}), 502

    # -------------------------------------------------- agent 子进程管理
    @app.route("/api/agent/status")
    def api_agent_status():
        """agent 子进程状态（独立网关模式；内嵌模式返回 embedded）。"""
        sup = app.config.get("SUPERVISOR")
        if sup is None:
            return jsonify({"ok": True, "mode": "embedded"})
        return jsonify({"ok": True, "mode": "supervised",
                        "agent": sup.status()})

    @app.route("/api/agent/start", methods=["POST"])
    def api_agent_start():
        sup = app.config.get("SUPERVISOR")
        if sup is None:
            return jsonify({"ok": False, "error": "网关未以独立模式运行"}), 400
        ok = sup.start()
        return jsonify({"ok": ok, "agent": sup.status()})

    @app.route("/api/agent/stop", methods=["POST"])
    def api_agent_stop():
        sup = app.config.get("SUPERVISOR")
        if sup is None:
            return jsonify({"ok": False, "error": "网关未以独立模式运行"}), 400
        sup.stop()
        return jsonify({"ok": True, "agent": sup.status()})

    @app.route("/api/agent/restart", methods=["POST"])
    def api_agent_restart():
        sup = app.config.get("SUPERVISOR")
        if sup is None:
            return jsonify({"ok": False, "error": "网关未以独立模式运行"}), 400
        ok = sup.restart()
        return jsonify({"ok": ok, "agent": sup.status()})

    @app.route("/api/agent/logs")
    def api_agent_logs():
        """agent.log 尾部 n 行（控制台页展示）。"""
        sup = app.config.get("SUPERVISOR")
        if sup is None:
            return jsonify({"ok": False, "error": "网关未以独立模式运行"}), 400
        try:
            n = int(request.args.get("n", "200"))
        except ValueError:
            n = 200
        return jsonify({"ok": True, "log": sup.logs_tail(n)})

    # -------------------------------------------------- 网关自身刷新（热重启）
    @app.route("/api/gateway/reload", methods=["POST"])
    def api_gateway_reload():
        """手动刷新网关（只重载网关代码，不影响 agent）。

        与"重启网关进程"的区别：本接口不重启进程、不关 agent——
        只 reload src/gateway/*.py 并热切换 server（先验证再切换，
        代码有问题时保持旧 server 继续服务）。
        """
        reloader = app.config.get("RELOADER")
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

    # ------------------------------------------------------------- 文件列表
    @app.route("/api/files")
    def api_files():
        which = request.args.get("dir", "")
        if which == "prompts":
            return jsonify({"ok": True, "files": _list_prompts(root)})
        if which == "personas":
            return jsonify({"ok": True, "files": _list_personas(root)})
        return jsonify({"ok": False,
                        "error": "dir must be prompts|personas"}), 400

    # ------------------------------------------------------------- 文件读写
    @app.route("/api/file", methods=["GET", "PUT"])
    def api_file():
        rel = request.args.get("path", "")
        full = _resolve_editable(rel)
        if full is None:
            return jsonify({"ok": False, "error": "path not allowed"}), 403
        if request.method == "GET":
            if not os.path.isfile(full):
                return jsonify({"ok": False, "error": "not found"}), 404
            with open(full, "r", encoding="utf-8") as f:
                content = f.read()
            return jsonify({"ok": True, "path": rel, "content": content,
                            "mtime": os.path.getmtime(full)})
        # PUT：保存即写文件，mtime 变化即热生效，无需通知任何人
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

    # ------------------------------------------------------------- runtime
    @app.route("/api/runtime", methods=["GET", "PUT"])
    def api_runtime():
        path = os.path.join(root, "config", "runtime.json")
        if request.method == "GET":
            data = _read_json(path)
            return jsonify({"ok": True,
                            "config": data if isinstance(data, dict) else {}})
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False,
                            "error": "body must be a JSON object"}), 400
        err = _validate_runtime(body)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        current = _read_json(path)
        if not isinstance(current, dict):
            current = {}
        current.update(body)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return jsonify({"ok": True, "config": current})

    # ------------------------------------------------------------- .env
    @app.route("/api/env", methods=["GET", "PUT"])
    def api_env():
        path = os.path.join(root, "workspace", ".env")
        if request.method == "GET":
            pairs = _parse_env(path) if os.path.isfile(path) else {}
            keys = [{"key": k, "masked": _mask(v)} for k, v in pairs.items()]
            return jsonify({"ok": True, "keys": keys})
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False,
                            "error": "body must be a JSON object"}), 400
        pairs = _parse_env(path) if os.path.isfile(path) else {}
        for key, value in body.items():
            if not key or "=" in key or any(c.isspace() for c in key):
                return jsonify({"ok": False,
                                "error": f"bad key: {key!r}"}), 400
            if not isinstance(value, str):
                return jsonify({"ok": False,
                                "error": f"value for {key} must be string"}), 400
            if value:  # 空值 = 保持不变（界面上脱敏展示，只填要改的）
                pairs[key] = value
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for k, v in pairs.items():
                f.write(f"{k}={v}\n")
        return jsonify({"ok": True, "keys": sorted(pairs)})

    # ------------------------------------------------------------- 状态
    @app.route("/api/status")
    def api_status():
        runtime_dir = os.path.join(root, "workspace", "runtime")
        return jsonify({
            "ok": True,
            "queue": _read_json(os.path.join(runtime_dir, "queue.json")),
            "watermarks": _read_json(os.path.join(runtime_dir,
                                                  "watermarks.json")),
            "tasks": _list_tasks(os.path.join(root, "workspace", "tasks")),
        })

    # ------------------------------------------------------------- 实况
    @app.route("/api/events")
    def api_events():
        """proxy_events.jsonl 尾部 n 条（倒序：最新在前）。"""
        try:
            n = int(request.args.get("n", "50"))
        except ValueError:
            n = 50
        n = max(1, min(n, 500))
        path = os.path.join(root, "workspace", "runtime",
                            "proxy_events.jsonl")
        return jsonify({"ok": True, "events": _read_jsonl_tail(path, n)})

    @app.route("/api/ops")
    def api_ops():
        """interaction_ops.jsonl 原子操作流水尾部 n 条（倒序）。"""
        try:
            n = int(request.args.get("n", "50"))
        except ValueError:
            n = 50
        n = max(1, min(n, 500))
        path = os.path.join(root, "workspace", "runtime",
                            "interaction_ops.jsonl")
        return jsonify({"ok": True, "ops": _read_jsonl_tail(path, n)})

    @app.route("/api/home_scan")
    def api_home_scan():
        """首页红点扫描快照；未产生过时 scan 为 null。"""
        path = os.path.join(root, "workspace", "runtime", "home_scan.json")
        data = _read_json(path)
        return jsonify({"ok": True,
                        "scan": data if isinstance(data, dict) else None})

    # ------------------------------------------------------------- 群聊配置
    @app.route("/api/groups", methods=["GET"])
    def api_groups():
        """列出所有会话及其热情度级别。"""
        from .group_config import LEVELS, read_level
        personas_dir = os.path.join(root, "config", "personas")
        sessions = set()
        # 来源 1：消息库里的会话
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
        # 来源 2：已有人格卡
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

    @app.route("/api/groups", methods=["PUT"])
    def api_groups_put():
        """设置某会话的热情度（写入人格卡，下次决策热生效）。"""
        from .group_config import write_level
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

    return app


# ---------------------------------------------------------------- 列表逻辑
def _read_json(path):
    """读 JSON 文件；不存在/解析失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_jsonl_tail(path, n):
    """读 jsonl 尾部 n 条（倒序：最新在前）；不存在返回 []，坏行跳过。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    events = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
        if len(events) >= n:
            break
    return events


def _list_prompts(root):
    """config/prompts/ 下的块文件：order.txt 顺序在前（标注分组与序号），
    未列入清单的文件排在末尾（order=None）。"""
    base = os.path.join(root, "config", "prompts")
    order_names = []
    order_path = os.path.join(base, "order.txt")
    if os.path.isfile(order_path):
        with open(order_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    order_names.append(line)

    files = []
    if os.path.isfile(order_path):
        files.append({"path": "config/prompts/order.txt", "name": "order.txt",
                      "group": "order", "order": 0,
                      "mtime": os.path.getmtime(order_path)})
    seen = set()
    for i, name in enumerate(order_names):
        full = os.path.join(base, name)
        if os.path.isfile(full):
            seen.add(os.path.normpath(name))
            files.append({"path": f"config/prompts/{name}", "name": name,
                          "group": name.split("/")[0], "order": i + 1,
                          "mtime": os.path.getmtime(full)})
    if os.path.isdir(base):
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, fn), base)
                rel = rel.replace(os.sep, "/")
                if rel == "order.txt" or os.path.normpath(rel) in seen:
                    continue
                full = os.path.join(base, rel)
                files.append({"path": f"config/prompts/{rel}", "name": rel,
                              "group": rel.split("/")[0], "order": None,
                              "mtime": os.path.getmtime(full)})
    return files


def _list_personas(root):
    base = os.path.join(root, "config", "personas")
    files = []
    if os.path.isdir(base):
        for fn in sorted(os.listdir(base)):
            full = os.path.join(base, fn)
            if os.path.isfile(full):
                files.append({"path": f"config/personas/{fn}", "name": fn,
                              "group": "persona", "order": None,
                              "mtime": os.path.getmtime(full)})
    return files


def _list_tasks(tasks_dir):
    """workspace/tasks/<日期>/<任务目录>/task.json 的 status 摘要。"""
    tasks = []
    if not os.path.isdir(tasks_dir):
        return tasks
    for date in sorted(os.listdir(tasks_dir), reverse=True):
        date_dir = os.path.join(tasks_dir, date)
        if not os.path.isdir(date_dir):
            continue
        for name in sorted(os.listdir(date_dir)):
            ledger = _read_json(os.path.join(date_dir, name, "task.json"))
            if not isinstance(ledger, dict):
                continue
            tasks.append({
                "date": date,
                "dir": name,
                "task_id": ledger.get("task_id", ""),
                "session": ledger.get("session", ""),
                "desc": ledger.get("desc", ""),
                "status": ledger.get("status", "unknown"),
                "started_at": ledger.get("started_at"),
                "finished_at": ledger.get("finished_at"),
            })
    return tasks


# ---------------------------------------------------------------- .env
def _parse_env(path):
    """解析 KEY=VALUE 行（忽略注释与空行），保持出现顺序。"""
    pairs = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
    return pairs


def _mask(value):
    """脱敏：只显示前 4 位 + 后 2 位；太短则全隐。"""
    if len(value) <= 6:
        return "****"
    return value[:4] + "****" + value[-2:]


# ---------------------------------------------------------------- runtime
def _validate_runtime(body):
    """按 CONTRACTS §五 校验 runtime.json 字段；返回错误信息或 None。"""
    for key, value in body.items():
        if key in _RUNTIME_INTERVAL_FIELDS:
            if (not isinstance(value, (list, tuple)) or len(value) != 2
                    or not all(isinstance(v, (int, float))
                               and not isinstance(v, bool) for v in value)):
                return f"{key} must be [min, max] numbers"
            continue
        expected = _RUNTIME_FIELDS.get(key)
        if expected is None:
            return f"unknown field: {key}"
        if isinstance(value, bool) and expected is not bool:
            return f"{key} must be {expected}"
        if not isinstance(value, expected):
            return f"{key} must be {expected}"
    return None


# ---------------------------------------------------------------- 页面
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Wechat-Agent 网关</title>
<style>
  /* ============ Cohere design system (DESIGN.md) ============ */
  :root {
    --primary: #17171c;        /* cohere near-black */
    --ink: #212121;
    --canvas: #ffffff;
    --stone: #eeece7;          /* soft warm neutral */
    --pale-green: #edfce9;
    --pale-blue: #f1f5ff;
    --hairline: #d9d9dd;
    --border-light: #e5e7eb;
    --card-border: #f2f2f2;
    --muted: #93939f;
    --slate: #75758a;
    --body-muted: #616161;
    --action-blue: #1863dc;
    --focus-blue: #4c6ee6;
    --coral: #ff7759;
    --coral-soft: #ffad9b;
    --deep-green: #003c33;
    --form-focus: #9b60aa;
    --error: #b30000;
    --r-xs: 4px; --r-sm: 8px; --r-md: 16px; --r-lg: 22px; --r-pill: 32px;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    font-family: 'Unica77', 'Inter', -apple-system, 'PingFang SC',
                 'Microsoft YaHei', sans-serif;
    margin: 0; background: var(--canvas); color: var(--ink);
    font-size: 15px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-thumb { background: #d4d4d8; border-radius: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }

  /* ---------- 布局：左侧深色侧边栏 + 右侧内容区 ---------- */
  .app { display: flex; height: 100vh; overflow: hidden; }
  .sidebar {
    width: 236px; min-width: 236px; background: var(--primary);
    color: #fff; display: flex; flex-direction: column;
    padding: 20px 12px; overflow-y: auto;
  }
  .brand { padding: 4px 12px 20px; border-bottom: 1px solid rgba(255,255,255,.08); }
  .brand .logo { font-size: 17px; font-weight: 600; letter-spacing: -.3px; }
  .brand .sub { font-size: 11px; color: var(--muted); margin-top: 2px;
                text-transform: uppercase; letter-spacing: .12em; }
  .nav { flex: 1; padding: 16px 0; }
  .nav-item {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 12px; margin-bottom: 2px;
    color: rgba(255,255,255,.72); font-size: 14px;
    border-radius: var(--r-sm); cursor: pointer; border: none;
    background: transparent; width: 100%; text-align: left;
    font-family: inherit; transition: background .15s, color .15s;
  }
  .nav-item:hover { background: rgba(255,255,255,.08); color: #fff; }
  .nav-item.active { background: #fff; color: var(--primary); font-weight: 600; }
  .nav-item .ico { width: 18px; text-align: center; font-size: 14px; opacity: .85; }
  .nav-foot { padding: 12px; border-top: 1px solid rgba(255,255,255,.08);
              font-size: 11px; color: var(--muted); }

  /* ---------- 主内容区 ---------- */
  .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 28px; border-bottom: 1px solid var(--border-light);
    background: var(--canvas);
  }
  .topbar h1 { font-size: 15px; font-weight: 500; margin: 0; letter-spacing: .2px; }
  .topbar .page { font-size: 13px; color: var(--muted); }
  .content { flex: 1; overflow: hidden; position: relative; }

  .pane {
    position: absolute; inset: 0; overflow-y: auto; padding: 24px 28px;
    display: none;
  }
  .pane.active { display: block; }

  /* ---------- 模块化卡片（块内可滚动） ---------- */
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .grid2 .span2 { grid-column: 1 / -1; }
  @media (max-width: 1100px) { .grid2 { grid-template-columns: 1fr; } }

  .card {
    background: var(--canvas); border: 1px solid var(--border-light);
    border-radius: var(--r-md); padding: 18px 20px;
    display: flex; flex-direction: column; min-height: 0;
  }
  .card > h2 {
    font-size: 11px; font-weight: 600; margin: 0 0 12px;
    color: var(--slate); text-transform: uppercase;
    letter-spacing: .14em; display: flex; align-items: center;
    justify-content: space-between;
  }
  .card .scroll {
    overflow-y: auto; min-height: 120px; flex: 1;
  }
  .card .scroll.tall { max-height: 46vh; }
  .card .scroll.mid { max-height: 34vh; }

  /* ---------- 按钮（药丸，Cohere button-primary） ---------- */
  .btn {
    font-family: inherit; font-size: 13px; font-weight: 500;
    padding: 8px 20px; border-radius: var(--r-pill); cursor: pointer;
    border: 1px solid var(--primary); background: var(--primary); color: #fff;
    transition: opacity .15s, background .15s; line-height: 1.6;
  }
  .btn:hover { opacity: .85; }
  .btn.ghost { background: transparent; color: var(--primary); }
  .btn.ghost:hover { background: rgba(23,23,28,.06); opacity: 1; }
  .btn.danger { background: #fff; color: var(--error);
                border-color: var(--error); }
  .btn.danger:hover { background: var(--error); color: #fff; opacity: 1; }
  .btn.sm { padding: 5px 14px; font-size: 12px; }

  /* ---------- 表单 ---------- */
  input[type=text], input[type=number], select, textarea {
    font-family: inherit; font-size: 13px; color: var(--ink);
    background: var(--canvas); border: 1px solid var(--hairline);
    border-radius: var(--r-xs); padding: 7px 10px; outline: none;
    transition: border-color .15s, box-shadow .15s;
  }
  input:focus, select:focus, textarea:focus {
    border-color: var(--form-focus);
    box-shadow: 0 0 0 3px rgba(155,96,170,.15);
  }
  input[type=text], input[type=number] { width: 100%; max-width: 340px; }
  input[type=checkbox] { accent-color: var(--primary); width: 16px; height: 16px; }

  /* ---------- 表格（research-table 风格：细线分隔） ---------- */
  table { border-collapse: collapse; width: 100%; }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: .1em;
       color: var(--slate); font-weight: 600; text-align: left;
       padding: 8px 10px; border-bottom: 1px solid var(--hairline); }
  td { padding: 8px 10px; font-size: 13px; border-bottom: 1px solid var(--card-border);
       vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr.action td { background: rgba(255,119,89,.07); }
  tr.unread td { color: var(--error); font-weight: 500; }

  /* ---------- 状态徽章 ---------- */
  .badge { display: inline-block; padding: 2px 10px; border-radius: var(--r-pill);
           font-size: 11px; font-weight: 600; letter-spacing: .04em; }
  .badge.running { background: var(--pale-green); color: var(--deep-green); }
  .badge.stopped { background: var(--stone); color: var(--body-muted); }
  .badge.crashed { background: #fdecec; color: var(--error); }
  .badge.coral { background: var(--coral-soft); color: #5a1f10; }

  /* ---------- 事件流 / 日志 ---------- */
  pre { margin: 0; font-family: 'SF Mono', ui-monospace, Menlo, Consolas,
                     monospace; font-size: 12px; line-height: 1.55;
        white-space: pre-wrap; word-break: break-all; }
  .log-pre { background: var(--primary); color: #d7d7dc; border-radius: var(--r-sm);
             padding: 14px; }
  .ev { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 12px;
        padding: 5px 0; border-bottom: 1px solid var(--card-border); }
  .ev .ts { color: var(--muted); margin-right: 10px; }
  .ev .etype { display: inline-block; min-width: 120px; color: var(--action-blue);
               font-weight: 600; }
  .ev details summary { cursor: pointer; list-style: none; }
  .ev details summary::-webkit-details-marker { display: none; }
  .ev details summary .etype::before { content: "▸ "; color: var(--muted); }
  .ev details[open] summary .etype::before { content: "▾ "; }
  .ev pre { white-space: pre-wrap; word-break: break-all; margin: 6px 0 2px;
            max-height: 340px; overflow: auto; background: var(--stone);
            border-radius: var(--r-xs); padding: 10px; color: var(--ink); }

  .dim { color: var(--muted); }
  .ok { color: var(--deep-green); }
  .warn { color: #b45309; }

  /* ---------- 编辑器页（prompts / personas） ---------- */
  .editor-wrap { display: flex; gap: 20px; height: 100%; }
  .filelist {
    width: 300px; min-width: 300px; background: var(--canvas);
    border: 1px solid var(--border-light); border-radius: var(--r-md);
    overflow-y: auto; padding: 10px;
  }
  .filelist .f {
    padding: 8px 10px; cursor: pointer; border-radius: var(--r-sm);
    font-size: 13px; display: flex; align-items: center; gap: 8px;
    border-bottom: 1px solid var(--card-border);
  }
  .filelist .f:hover { background: var(--stone); }
  .filelist .f.active { background: var(--pale-blue); }
  .filelist .tag { font-size: 10px; color: #fff; border-radius: var(--r-pill);
                   padding: 1px 8px; text-transform: uppercase;
                   letter-spacing: .06em; }
  .tag.system { background: #5b6472; } .tag.user { background: #8a5a9e; }
  .tag.persona { background: #3d7a6f; } .tag.order { background: #6b7280; }
  .editor {
    flex: 1; display: flex; flex-direction: column; min-width: 0;
    background: var(--canvas); border: 1px solid var(--border-light);
    border-radius: var(--r-md); overflow: hidden;
  }
  .editor .bar { display: flex; align-items: center; gap: 12px;
                 padding: 10px 14px; border-bottom: 1px solid var(--border-light); }
  .editor .bar #cur { font-size: 12px; color: var(--slate); flex: 1;
                      font-family: ui-monospace, monospace; overflow: hidden;
                      text-overflow: ellipsis; white-space: nowrap; }
  .editor textarea {
    flex: 1; border: none; border-radius: 0; resize: none;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 13px;
    line-height: 1.6; padding: 16px; background: var(--canvas);
  }
  #dirty { color: #b45309; font-weight: 600; font-size: 12px; }
  #msg, #rt_msg, #env_msg, #agent_msg { font-size: 12px; color: var(--deep-green); }

  /* 实况页专用小卡 */
  .stat-chip { display: inline-flex; align-items: center; gap: 6px;
               background: var(--stone); border-radius: var(--r-pill);
               padding: 4px 12px; font-size: 12px; color: var(--body-muted); }
  .chip-row { display: flex; gap: 8px; flex-wrap: wrap; }

  /* ---------- 卡片 hover 微交互（Cohere: 边框微亮，无阴影） ---------- */
  .card { transition: border-color .2s; }
  .card:hover { border-color: var(--hairline); }

  /* ---------- 侧边栏 agent 状态灯 ---------- */
  .agent-dot { display: inline-block; width: 8px; height: 8px;
               border-radius: 50%; margin-right: 6px; vertical-align: middle;
               background: var(--muted); }
  .agent-dot.running { background: #34d399; box-shadow: 0 0 6px rgba(52,211,153,.6); }
  .agent-dot.stopped { background: var(--muted); }
  .agent-dot.crashed { background: var(--error); box-shadow: 0 0 6px rgba(179,0,0,.5); }
  .agent-line { display: flex; align-items: center; justify-content: space-between;
                font-size: 11px; color: var(--muted); }
  .agent-line b { color: rgba(255,255,255,.85); font-weight: 500; }

  /* ---------- toast 提示 ---------- */
  #toast { position: fixed; top: 18px; right: 24px; z-index: 9999;
           display: flex; flex-direction: column; gap: 8px; }
  .toast-item { background: var(--primary); color: #fff; border-radius: var(--r-sm);
                padding: 10px 16px; font-size: 13px; box-shadow: 0 4px 16px
                rgba(0,0,0,.18); opacity: 0; transform: translateY(-6px);
                transition: opacity .25s, transform .25s; max-width: 360px;
                word-break: break-all; }
  .toast-item.show { opacity: 1; transform: translateY(0); }
  .toast-item.ok { background: var(--deep-green); }
  .toast-item.err { background: var(--error); }

  /* ---------- 空状态 ---------- */
  .empty { text-align: center; padding: 28px 12px; color: var(--muted);
           font-size: 13px; }
  .empty::before { content: "—"; display: block; font-size: 20px;
                   margin-bottom: 6px; color: var(--hairline); }

  /* ---------- 加载态 ---------- */
  .loading { color: var(--muted); font-size: 12px; padding: 12px 0;
             display: flex; align-items: center; gap: 8px; }
  .spinner { width: 12px; height: 12px; border: 2px solid var(--hairline);
             border-top-color: var(--primary); border-radius: 50%;
             animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="app">
  <!-- 左侧深色侧边栏：导航 -->
  <aside class="sidebar">
    <div class="brand">
      <div class="logo">Wechat-Agent</div>
      <div class="sub">Control Plane</div>
    </div>
    <nav class="nav" id="nav">
      <button class="nav-item" data-tab="live" onclick="showTab('live')">
        <span class="ico">◉</span>实况
      </button>
      <button class="nav-item" data-tab="console" onclick="showTab('console')">
        <span class="ico">▶</span>控制台
      </button>
      <button class="nav-item" data-tab="prompts" onclick="showTab('prompts')">
        <span class="ico">✎</span>Prompt 编辑
      </button>
      <button class="nav-item" data-tab="personas" onclick="showTab('personas')">
        <span class="ico">♟</span>人格卡
      </button>
      <button class="nav-item" data-tab="groups" onclick="showTab('groups')">
        <span class="ico">☰</span>群聊配置
      </button>
      <button class="nav-item" data-tab="runtime" onclick="showTab('runtime')">
        <span class="ico">⚙</span>运行配置
      </button>
      <button class="nav-item" data-tab="env" onclick="showTab('env')">
        <span class="ico">🔑</span>密钥
      </button>
      <button class="nav-item" data-tab="status" onclick="showTab('status')">
        <span class="ico">▤</span>状态
      </button>
    </nav>
    <div class="nav-foot">
      <div class="agent-line"><span><span class="agent-dot" id="nav-agent-dot"></span><b id="nav-agent-state">—</b></span></div>
      <div style="margin-top:6px">gateway · 13014</div>
    </div>
  </aside>

  <!-- 右侧主内容区 -->
  <div class="main">
    <div class="topbar">
      <h1 id="page-title">实况</h1>
      <div class="page" id="page-sub"></div>
    </div>
    <div class="content">

      <!-- 实况：模块化卡片 -->
      <div class="pane" id="pane-live">
        <div class="grid2">
          <div class="card"><h2>时序队列</h2>
            <div class="scroll mid" id="live-queue"></div></div>
          <div class="card"><h2>首页红点</h2>
            <div class="scroll mid" id="live-home"></div></div>
          <div class="card span2"><h2>Proxy 流水</h2>
            <div class="scroll tall" id="live-events"></div></div>
          <div class="card span2"><h2>原子操作</h2>
            <div class="scroll tall" id="live-ops"></div></div>
        </div>
      </div>

      <!-- 控制台：agent 管理 -->
      <div class="pane" id="pane-console">
        <div class="grid2">
          <div class="card"><h2>Agent 控制</h2>
            <div id="console-status"></div></div>
          <div class="card"><h2>Agent 日志 <span class="badge coral">尾部 200 行</span></h2>
            <div class="scroll tall" id="console-log"></div></div>
        </div>
      </div>

      <!-- Prompt / 人格卡 编辑器 -->
      <div class="pane" id="pane-editor" style="padding:0">
        <div class="editor-wrap" style="height:100%">
          <div class="filelist" id="filelist"></div>
          <div class="editor">
            <div class="bar">
              <span id="cur">未选择文件</span>
              <button class="btn sm" onclick="saveFile()">保存</button>
              <span id="dirty" style="display:none">● 未保存</span>
              <span id="msg"></span>
            </div>
            <textarea id="ta" oninput="markDirty()"
              placeholder="左侧选择文件开始编辑"></textarea>
          </div>
        </div>
      </div>

      <!-- 群聊配置 -->
      <div class="pane" id="pane-groups"></div>
      <!-- 运行配置 -->
      <div class="pane" id="pane-runtime"></div>
      <!-- 密钥 -->
      <div class="pane" id="pane-env"></div>
      <!-- 状态 -->
      <div class="pane" id="pane-status"></div>

    </div>
  </div>
</div>
<div id="toast"></div>
<script>
let curPath = null, savedContent = "", dirty = false, curTab = "live";

function api(path, opts) {
  return fetch(path, opts).then(r => r.json().then(j => {
    if (!r.ok) throw new Error(j.error || r.status);
    return j;
  }));
}
function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;');
}
// toast 提示（替代 alert）
function toast(msg, kind) {
  const t = document.createElement('div');
  t.className = 'toast-item ' + (kind || '');
  t.textContent = msg;
  document.getElementById('toast').appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.remove(), 300);
  }, 3200);
}
// 侧边栏 agent 状态灯（5s 轮询）
function refreshNavAgent() {
  api('/api/agent/status').then(j => {
    const state = (j.agent || {}).state || (j.mode === 'embedded' ? 'embedded' : 'stopped');
    const dot = document.getElementById('nav-agent-dot');
    const txt = document.getElementById('nav-agent-state');
    if (!dot || !txt) return;
    dot.className = 'agent-dot ' + (state === 'running' ? 'running'
                                    : state === 'crashed' ? 'crashed' : 'stopped');
    txt.textContent = state === 'running' ? 'Agent 运行中'
                     : state === 'crashed' ? 'Agent 崩溃'
                     : state === 'embedded' ? '内嵌模式' : 'Agent 未启动';
  }).catch(() => {});
}
setInterval(refreshNavAgent, 5000);
refreshNavAgent();
function markDirty() {
  dirty = (document.getElementById('ta').value !== savedContent);
  document.getElementById('dirty').style.display = dirty ? 'inline' : 'none';
}
window.onbeforeunload = () => dirty ? '有未保存的修改' : null;

function showTab(name) {
  curTab = name;
  const editing = (name === 'prompts' || name === 'personas');
  const titles = {live:'实况', console:'控制台', prompts:'Prompt 编辑',
                  personas:'人格卡', groups:'群聊配置', runtime:'运行配置',
                  env:'密钥', status:'状态'};
  const subs = {live:'队列 · 红点 · 决策流水', console:'Agent 进程管理',
                prompts:'输出协议 / 工具说明 / 模板', personas:'各会话态度卡',
                groups:'每群热情度', runtime:'runtime.json 热生效',
                env:'workspace/.env 脱敏', status:'队列 / 水位 / 任务台账'};
  document.getElementById('page-title').textContent = titles[name] || name;
  document.getElementById('page-sub').textContent = subs[name] || '';
  // 所有 pane 隐藏，只显示当前
  for (const p of ['live', 'console', 'groups', 'runtime', 'env', 'status'])
    document.getElementById('pane-' + p).classList.toggle('active',
                                                          p === name);
  // 编辑器页共用 pane-editor
  document.getElementById('pane-editor').classList.toggle('active', editing);
  // 侧边栏高亮
  for (const b of document.querySelectorAll('.nav-item'))
    b.classList.toggle('active', b.dataset.tab === name);
  if (editing) loadFiles(name);
  if (name === 'live') loadLive();
  if (name === 'console') loadConsole();
  if (name === 'groups') loadGroups();
  if (name === 'runtime') loadRuntime();
  if (name === 'env') loadEnv();
  if (name === 'status') loadStatus();
}

// ------------------------------------------------------------- 控制台（agent 管理）
let AGENT_TIMER = null;
function loadConsole() {
  api('/api/agent/status').then(j => {
    if (j.mode === 'embedded') {
      document.getElementById('console-status').innerHTML =
        '<div class="empty">网关以内嵌模式运行（随 agent 主进程）</div>';
      document.getElementById('console-log').innerHTML =
        '<div class="empty">日志由 agent 进程输出</div>';
      return;
    }
    const a = j.agent || {};
    const state = a.state || 'unknown';
    const pid = a.pid ? ('PID ' + a.pid) : '-';
    const st = a.started_at ? new Date(a.started_at * 1000).toLocaleTimeString()
                            : '-';
    const exit = a.exit ? ('退出码 ' + a.exit.code +
                            (a.exit.ts ? ' @ ' + new Date(a.exit.ts * 1000)
                             .toLocaleTimeString() : '')) : '';
    document.getElementById('console-status').innerHTML =
      '<div class="chip-row" style="margin-bottom:14px">' +
      '<span class="badge ' + state + '">' + state + '</span>' +
      '<span class="stat-chip">' + pid + '</span>' +
      '<span class="stat-chip">启动 ' + st + '</span>' +
      (exit ? '<span class="stat-chip">' + esc(exit) + '</span>' : '') +
      '</div>' +
      '<div class="dim" style="font-size:11px;text-transform:uppercase;' +
      'letter-spacing:.1em;margin:10px 0 6px">Agent 进程控制</div>' +
      '<div style="display:flex;gap:10px">' +
      '<button class="btn" onclick="agentAction(\'start\')">启动</button>' +
      '<button class="btn ghost" onclick="agentAction(\'stop\')">停止</button>' +
      '<button class="btn ghost" onclick="agentAction(\'restart\')">重启</button>' +
      '</div>' +
      '<div class="dim" style="font-size:11px;text-transform:uppercase;' +
      'letter-spacing:.1em;margin:18px 0 6px">网关自身（不影响 agent）</div>' +
      '<div style="display:flex;gap:10px;align-items:center">' +
      '<button class="btn ghost" onclick="gatewayReload()">刷新网关</button>' +
      '<span class="dim" style="font-size:12px">只重载网关代码，agent 不受影响</span>' +
      '</div>' +
      '<div style="margin-top:12px"><span id="agent_msg"></span></div>';
    document.getElementById('console-log').innerHTML =
      '<pre class="log-pre">' + esc(j.log || '(无日志)') + '</pre>';
    // 运行中自动刷新日志
    if (AGENT_TIMER) clearInterval(AGENT_TIMER);
    if (state === 'running') {
      AGENT_TIMER = setInterval(() => {
        api('/api/agent/logs?n=200').then(j => {
          const el = document.querySelector('#console-log pre');
          if (el && j.log) el.textContent = j.log;
        }).catch(() => {});
      }, 3000);
    }
  }).catch(e => toast(e.message, 'err'));
}
function agentAction(action) {
  api('/api/agent/' + action, {method: 'POST'}).then(j => {
    if (j.ok) toast(action === 'start' ? 'Agent 已启动'
                : action === 'stop' ? 'Agent 已停止'
                : 'Agent 已重启', 'ok');
    else toast('操作失败：' + (j.error || '未知错误'), 'err');
    loadConsole();
    refreshNavAgent();
  }).catch(e => toast(e.message, 'err'));
}

// 手动刷新网关：只重载网关代码，不影响 agent（进程不动）
function gatewayReload() {
  toast('正在刷新网关…');
  api('/api/gateway/reload', {method: 'POST'}).then(j => {
    if (j.ok) {
      toast(j.reloaded ? '网关已刷新（agent 不受影响）'
                       : '网关刷新完成', 'ok');
      if (j.changed) console.log('变更文件:', j.changed);
    } else {
      toast('刷新失败，保持旧网关服务：' + (j.error || '未知错误'), 'err');
    }
  }).catch(e => toast(e.message, 'err'));
}

let GROUPS = [], LEVELS = {};
function loadGroups() {
  api('/api/groups').then(j => {
    GROUPS = j.groups; LEVELS = j.levels;
    let h = '<div class="card"><h2>群聊热情度</h2>' +
      '<p class="dim" style="margin:0 0 12px">改动写入对应会话的人格卡，下一次决策即生效，无需重启。</p>' +
      '<div class="scroll tall"><table><tr><th>会话</th><th>热情度</th><th>补充规则</th><th></th></tr>';
    for (const g of GROUPS) {
      let opts = '';
      for (const [k, v] of Object.entries(LEVELS))
        opts += '<option value="' + k + '"' +
                (g.level === k ? ' selected' : '') + '>' +
                v.label + '（' + v.desc + '）</option>';
      h += '<tr><td>' + g.session + '</td><td><select id="lv-' +
           encodeURIComponent(g.session) + '">' + opts + '</select></td>' +
           '<td><input id="ex-' + encodeURIComponent(g.session) +
           '" value="' + (g.extra_rule || '').replace(/"/g, '&quot;') +
           '" style="width:220px" placeholder="选填，如：少发表情包"/></td>' +
           '<td><button class="btn sm" data-sess="' + encodeURIComponent(g.session) +
           '" onclick="setLevel(decodeURIComponent(this.dataset.sess))">保存</button></td></tr>';
    }
    h += '</table></div></div>';
    document.getElementById('pane-groups').innerHTML = h;
  });
}
function setLevel(session) {
  const key = encodeURIComponent(session);
  const level = document.getElementById('lv-' + key).value;
  const extra = document.getElementById('ex-' + key).value;
  fetch('/api/groups', {method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session, level, extra_rule: extra})})
    .then(r => r.json()).then(j => {
      if (!j.ok) toast(j.error || '保存失败', 'err');
      loadGroups();
    }).catch(e => toast(e.message, 'err'));
}

function loadFiles(which) {
  api('/api/files?dir=' + which).then(j => {
    const sb = document.getElementById('filelist');
    sb.innerHTML = '';
    for (const f of j.files) {
      const d = document.createElement('div');
      d.className = 'f' + (f.path === curPath ? ' active' : '');
      const ord = f.order ? ('#' + f.order + ' ') : '';
      d.innerHTML = '<span class="tag ' + f.group + '">' + f.group +
                    '</span>' + ord + esc(f.name);
      d.onclick = () => openFile(f.path);
      sb.appendChild(d);
    }
  }).catch(e => toast(e.message, 'err'));
}

function openFile(path) {
  if (dirty && !confirm('当前文件有未保存修改，确定切换？')) return;
  api('/api/file?path=' + encodeURIComponent(path)).then(j => {
    curPath = path;
    savedContent = j.content;
    document.getElementById('ta').value = j.content;
    document.getElementById('cur').textContent = path;
    markDirty();
  }).catch(e => toast(e.message, 'err'));
}

function saveFile() {
  if (!curPath) return;
  api('/api/file?path=' + encodeURIComponent(curPath), {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({content: document.getElementById('ta').value})
  }).then(() => {
    savedContent = document.getElementById('ta').value;
    markDirty();
    flash('已保存（热生效）');
  }).catch(e => toast(e.message, 'err'));
}

function flash(t) {
  const m = document.getElementById('msg');
  m.textContent = t;
  setTimeout(() => m.textContent = '', 2000);
}

const RUNTIME_SCHEMA = [
  ['max_concurrent_decisions', 'int'], ['media_convert_concurrency', 'int'],
  ['history_size', 'int'], ['sweep_interval', 'interval'],
  ['notify_interval', 'interval'], ['paused', 'bool'],
  ['muted_until', 'float'], ['owner', 'str'], ['owner_nick', 'str'],
  ['action_max_attempts', 'int'], ['task_retention_days', 'int'],
  ['tool_model', 'str'],
];

function loadRuntime() {
  api('/api/runtime').then(j => {
    const p = document.getElementById('pane-runtime');
    let h = '<div class="card"><h2>运行配置 <span class="badge coral">mtime 热生效</span></h2>' +
            '<div class="scroll tall"><table>';
    for (const [k, t] of RUNTIME_SCHEMA) {
      const v = j.config[k];
      h += '<tr><td style="width:40%">' + k + '</td><td>';
      if (t === 'bool')
        h += '<input type="checkbox" id="rt_' + k + '"' +
             (v ? ' checked' : '') + '>';
      else if (t === 'interval')
        h += '<input type="number" id="rt_' + k + '_0" style="width:80px" ' +
             'value="' + (v ? v[0] : '') + '"> ~ ' +
             '<input type="number" id="rt_' + k + '_1" style="width:80px" ' +
             'value="' + (v ? v[1] : '') + '">';
      else if (t === 'str')
        h += '<input type="text" id="rt_' + k + '" value="' +
             esc(v ?? '') + '">';
      else
        h += '<input type="number" id="rt_' + k + '" value="' +
             (v ?? '') + '">';
      h += '</td></tr>';
    }
    h += '</table></div><div style="margin-top:14px">' +
         '<button class="btn" onclick="saveRuntime()">保存</button>' +
         ' <span id="rt_msg"></span></div></div>';
    p.innerHTML = h;
  }).catch(e => toast(e.message, 'err'));
}

function saveRuntime() {
  const body = {};
  for (const [k, t] of RUNTIME_SCHEMA) {
    if (t === 'bool')
      body[k] = document.getElementById('rt_' + k).checked;
    else if (t === 'interval')
      body[k] = [Number(document.getElementById('rt_' + k + '_0').value),
                 Number(document.getElementById('rt_' + k + '_1').value)];
    else if (t === 'str')
      body[k] = document.getElementById('rt_' + k).value;
    else
      body[k] = Number(document.getElementById('rt_' + k).value);
  }
  api('/api/runtime', {method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(() => document.getElementById('rt_msg').textContent = '已保存')
    .catch(e => toast(e.message, 'err'));
}

function loadEnv() {
  api('/api/env').then(j => {
    const p = document.getElementById('pane-env');
    let h = '<div class="card"><h2>密钥 <span class="badge coral">脱敏</span></h2>' +
            '<p class="dim" style="margin:0 0 12px">workspace/.env，只落本机文件，编辑即生效。</p>' +
            '<div class="scroll tall"><table><tr><th>键</th><th>当前值</th>' +
            '<th>新值（留空=不变）</th></tr>';
    for (const item of j.keys)
      h += '<tr><td>' + esc(item.key) + '</td><td>' + esc(item.masked) +
           '</td><td><input type="text" data-envkey="' + esc(item.key) +
           '"></td></tr>';
    h += '<tr><td><input type="text" id="env_newkey" ' +
         'placeholder="NEW_KEY"></td><td></td>' +
         '<td><input type="text" id="env_newval"></td></tr>';
    h += '</table></div><div style="margin-top:14px">' +
         '<button class="btn" onclick="saveEnv()">保存</button>' +
         ' <span id="env_msg"></span></div></div>';
    p.innerHTML = h;
  }).catch(e => toast(e.message, 'err'));
}

function saveEnv() {
  const body = {};
  for (const inp of document.querySelectorAll('[data-envkey]'))
    if (inp.value) body[inp.dataset.envkey] = inp.value;
  const nk = document.getElementById('env_newkey').value.trim();
  const nv = document.getElementById('env_newval').value;
  if (nk && nv) body[nk] = nv;
  api('/api/env', {method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(() => { loadEnv(); }).catch(e => toast(e.message, 'err'));
}

function loadStatus() {
  api('/api/status').then(j => {
    const p = document.getElementById('pane-status');
    const block = (title, obj) => '<div class="card"><h2>' + title +
      '</h2><div class="scroll tall">' +
      (obj == null ? '<p class="dim">暂无数据</p>'
                   : '<pre>' + esc(JSON.stringify(obj, null, 2)) + '</pre>') +
      '</div></div>';
    let h = '<div class="grid2">' +
            '<div class="card"><h2>时序队列（出队顺序）</h2>' +
            '<div class="scroll tall">' + queueTable(j.queue) + '</div></div>' +
            block('水位（watermarks.json）', j.watermarks) +
            '</div>';
    h += '<div class="card" style="margin-top:20px"><h2>任务台账（tasks/）</h2>' +
         '<div class="scroll tall">';
    if (!j.tasks.length) h += '<p class="dim">暂无数据</p>';
    else {
      h += '<table><tr><th>日期</th><th>task_id</th><th>会话</th>' +
           '<th>描述</th><th>状态</th></tr>';
      for (const t of j.tasks)
        h += '<tr><td>' + esc(t.date) + '</td><td>' + esc(t.task_id) +
             '</td><td>' + esc(t.session) + '</td><td>' + esc(t.desc) +
             '</td><td>' + esc(t.status) + '</td></tr>';
      h += '</table>';
    }
    h += '</div></div>';
    p.innerHTML = h;
  }).catch(e => toast(e.message, 'err'));
}

function queueTable(queue) {
  if (!queue || !queue.length) return '<p class="dim">队列为空</p>';
  let h = '<table><tr><th>#</th><th>类型</th><th>会话</th><th>@我</th>' +
          '<th>已试</th><th>来源</th><th>入队时间</th><th>内容摘要</th></tr>';
  for (const e of queue) {
    const ts = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : '-';
    const kind = e.kind === 'action' ? '行动' : '通知';
    h += '<tr class="' + (e.kind === 'action' ? 'action' : '') + '"><td>' +
         e.order + '</td><td>' + kind + '</td><td>' + esc(e.session) +
         '</td><td>' + (e.mention ? '⚠️' : '') + '</td><td>' + e.attempts +
         '</td><td>' + esc((e.sources || []).join(',')) + '</td><td>' + ts +
         '</td><td>' + esc(e.payload_brief || '') + '</td></tr>';
  }
  return h + '</table>';
}

// ------------------------------------------------------------- 实况页
function loadLive() {
  // 有展开的事件详情（正在读 prompt/输出）时，只冻结事件流，
  // 队列/红点照常刷新
  const eventsFrozen =
    !!document.querySelector('#live-events details[open]');
  Promise.all([
    api('/api/status').catch(() => null),
    api('/api/home_scan').catch(() => null),
    eventsFrozen ? Promise.resolve(null)
                 : api('/api/events?n=80').catch(() => null),
    api('/api/ops?n=40').catch(() => null),
  ]).then(([st, hs, ev, ops]) => {
    // 统计 chips（卡片标题栏）
    const q = st && st.queue || [];
    const scan = hs && hs.scan;
    const hot = scan && scan.sessions
      ? scan.sessions.filter(s => s.unread_count > 0 || s.unread_kind === 'dot'
                                  || s.mention_me).length : 0;
    const chips = (n, label) =>
      '<span class="stat-chip">' + n + ' ' + label + '</span>';
    const qHead = document.querySelector('#pane-live .card:nth-child(1) h2');
    const hHead = document.querySelector('#pane-live .card:nth-child(2) h2');
    if (qHead) qHead.innerHTML = '时序队列 ' + chips(q.length, '条');
    if (hHead) hHead.innerHTML = '首页红点 ' +
      chips(scan ? (scan.sessions || []).length : 0, '会话') +
      chips(hot, '有未读');

    const qEl = document.getElementById('live-queue');
    const hEl = document.getElementById('live-home');
    if (!q.length) qEl.innerHTML = '<div class="empty">队列为空，无待办</div>';
    else qEl.innerHTML = queueTable(q);
    if (!scan || !scan.sessions || !scan.sessions.length)
      hEl.innerHTML = '<div class="empty">暂无数据（等待下一轮扫描）</div>';
    else hEl.innerHTML = renderHome(scan);
    if (ev) {
      document.getElementById('live-events').innerHTML =
        ev.events && ev.events.length ? renderEvents(ev.events)
                                      : '<div class="empty">暂无决策事件</div>';
    }
    if (ops) {
      document.getElementById('live-ops').innerHTML =
        ops.ops && ops.ops.length ? renderOps(ops.ops)
                                  : '<div class="empty">暂无原子操作</div>';
    }
  });
}

function renderHome(scan) {
  if (!scan || !scan.sessions || !scan.sessions.length)
    return '<p class="dim">暂无数据（等待下一轮扫描）</p>';
  const ts = scan.ts ? new Date(scan.ts * 1000).toLocaleTimeString() : '-';
  let h = '<p class="dim">更新于 ' + ts + '</p><table><tr><th>会话</th>' +
          '<th>未读</th><th>@我</th><th>免打扰</th></tr>';
  for (const s of scan.sessions) {
    const unread = s.unread_count > 0 ? String(s.unread_count)
                 : (s.unread_kind === 'dot' ? '红点' : '');
    const hot = s.unread_count > 0 || s.unread_kind === 'dot' || s.mention_me;
    h += '<tr class="' + (hot ? 'unread' : '') + '"><td>' + esc(s.label) +
         (s.partial ? ' <span class="dim">(残缺)</span>' : '') + '</td><td>' +
         unread + '</td><td>' + (s.mention_me ? '⚠️' : '') + '</td><td>' +
         (s.muted ? '🔕' : '') + '</td></tr>';
  }
  return h + '</table>';
}

function eventSummary(e) {
  switch (e.type) {
    case 'decision_start':
      return (e.session || '') + ' trigger=' + (e.trigger || '') +
             ' 新消息=' + e.new_messages;
    case 'prompt':
      return (e.session || '') + ' round=' + e.round;
    case 'llm_output':
      return (e.session || '') + ' round=' + e.round + ' ' +
             String(e.output || '').slice(0, 80).replace(/\\n/g, ' ');
    case 'route':
      return (e.session || '') + ' 块=[' + (e.blocks || []).join(',') +
             '] 投递=' + (e.deliveries || [])
               .map(d => d.session + ':' + (d.ok ? 'ok' : 'fail')).join(',');
    case 'media_convert':
      return (e.session || '') + ' 成功 ' + e.ok + '/' + e.total;
    case 'task_start':
      return (e.task_id || '') + ' ' + (e.session || '') + ' ' +
             (e.desc || '');
    case 'task_done':
      return (e.task_id || '') + ' ' + (e.session || '') + ' ' +
             (e.desc || '') + ' ' + (e.ok ? '成功' : '失败');
    case 'decision_end':
      return (e.session || '') + ' 回复=' + (e.replied ? '是' : '否') +
             ' ' + e.elapsed_ms + 'ms';
    default:
      return JSON.stringify(e).slice(0, 120);
  }
}

function renderOps(ops) {
  if (!ops.length) return '<p class="dim">暂无数据</p>';
  let h = '';
  for (const o of ops) {
    const ts = new Date(o.ts * 1000).toLocaleTimeString();
    const detail = Object.entries(o)
      .filter(([k]) => !['ts', 'op'].includes(k))
      .map(([k, v]) => k + '=' + v).join(' ');
    h += '<div class="ev"><span class="ts">' + ts + '</span> <b>' + esc(o.op) +
         '</b> <span class="dim">' + esc(detail) + '</span></div>';
  }
  return h;
}

function renderEvents(events) {
  if (!events || !events.length) return '<p class="dim">暂无事件</p>';
  let h = '';
  for (const e of events) {
    const ts = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : '-';
    const head = '<span class="ts">' + ts + '</span>' +
                 '<span class="etype">' + esc(e.type) + '</span>' +
                 esc(eventSummary(e));
    if (e.type === 'prompt' || e.type === 'llm_output') {
      const full = e.type === 'prompt'
        ? '[system]\\n' + (e.system || '') + '\\n\\n[user]\\n' + (e.user || '')
        : (e.output || '');
      h += '<div class="ev"><details><summary>' + head + '</summary><pre>' +
           esc(full) + '</pre></details></div>';
    } else {
      h += '<div class="ev">' + head + '</div>';
    }
  }
  return h;
}

// 实况页每 3 秒自动刷新（仅实况标签页激活时）
setInterval(() => { if (curTab === 'live') loadLive(); }, 3000);

showTab('live');
</script>
</body>
</html>
"""
