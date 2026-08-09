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


def create_app(project_root=None, proxy=None):
    """Flask 应用工厂。project_root 指向仓库根（含 config/ 与 workspace/），
    缺省取仓库根；测试传 tmp 目录副本。main.py 可用线程挂载本工厂产物。
    proxy 注入后开放 /api/task_done（进程外任务完成回执注入）。"""
    app = Flask(__name__)
    root = os.path.abspath(project_root or PROJECT_ROOT)
    app.config["PROJECT_ROOT"] = root
    app.config["PROXY"] = proxy

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
        body: {"task_id": "t..."}"""
        p = app.config.get("PROXY")
        if p is None:
            return jsonify({"ok": False, "error": "proxy 未接线"}), 503
        task_id = (request.get_json(silent=True) or {}).get("task_id", "")
        if not task_id:
            return jsonify({"ok": False, "error": "缺 task_id"}), 400
        return jsonify({"ok": bool(p.inject_task_done(task_id))})

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
  :root { --bg:#14181d; --panel:#1c2128; --border:#30363d; --fg:#c9d1d9;
          --dim:#8b949e; --accent:#1f6feb; --warn:#d29922; --bad:#f85149; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", "PingFang SC", sans-serif;
         margin: 0; background: var(--bg); color: var(--fg); }
  header { background: var(--panel); border-bottom: 1px solid var(--border);
           padding: 8px 16px; }
  header h1 { font-size: 16px; margin: 0; display: inline-block; }
  nav { display: inline-block; margin-left: 24px; }
  nav button { margin-right: 6px; padding: 4px 14px; background: #21262d;
               color: var(--fg); border: 1px solid var(--border);
               border-radius: 6px; cursor: pointer; }
  nav button:hover { border-color: var(--dim); }
  nav button.active { background: var(--accent); border-color: var(--accent);
                      color: #fff; }
  main { display: flex; height: calc(100vh - 40px); }
  #sidebar { width: 280px; overflow: auto; border-right: 1px solid var(--border);
             padding: 8px; }
  #sidebar .f { padding: 3px 6px; cursor: pointer; border-radius: 4px;
                font-size: 13px; }
  #sidebar .f:hover { background: #21262d; }
  #sidebar .f.active { background: #1f3355; }
  #sidebar .tag { font-size: 11px; color: #fff; border-radius: 3px;
                  padding: 0 4px; margin-right: 4px; }
  .tag.system { background: #587; } .tag.user { background: #975; }
  .tag.persona { background: #759; } .tag.order { background: #555; }
  #editor { flex: 1; display: flex; flex-direction: column; padding: 8px; }
  #editor textarea { flex: 1; font-family: monospace; font-size: 13px;
                     width: 100%; background: #0d1117; color: var(--fg);
                     border: 1px solid var(--border); border-radius: 6px;
                     padding: 8px; }
  #bar { padding: 6px 0; }
  #bar button, .pane button { background: #21262d; color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px; padding: 4px 14px;
    cursor: pointer; }
  #dirty { color: var(--warn); font-weight: bold; margin-left: 12px; }
  .pane { flex: 1; overflow: auto; padding: 12px; display: none; }
  .card { background: var(--panel); border: 1px solid var(--border);
          border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; }
  .card h2 { font-size: 14px; margin: 0 0 8px; color: var(--dim);
             font-weight: 600; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border-bottom: 1px solid var(--border); padding: 4px 8px;
           font-size: 13px; text-align: left; }
  th { color: var(--dim); font-weight: 600; }
  tr.action td { background: rgba(210, 153, 34, .12); }
  tr.unread td { color: var(--bad); }
  tr.unread td:first-child { font-weight: 600; }
  input[type=text], input[type=number] { width: 220px; background: #0d1117;
    color: var(--fg); border: 1px solid var(--border); border-radius: 4px;
    padding: 3px 6px; }
  pre { background: #0d1117; border: 1px solid var(--border);
        border-radius: 6px; padding: 8px; font-size: 12px; }
  #msg { margin-left: 12px; color: #3fb950; }
  .dim { color: var(--dim); }
  .ev { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 12px; padding: 3px 0; border-bottom: 1px solid #21262d; }
  .ev .ts { color: var(--dim); margin-right: 8px; }
  .ev .etype { display: inline-block; min-width: 110px; color: #79c0ff; }
  .ev details summary { cursor: pointer; list-style: none; }
  .ev details summary::-webkit-details-marker { display: none; }
  .ev details summary .etype::before { content: "▸ "; color: var(--dim); }
  .ev details[open] summary .etype::before { content: "▾ "; }
  .ev pre { white-space: pre-wrap; word-break: break-all; margin: 4px 0;
            max-height: 420px; overflow: auto; }
</style>
</head>
<body>
<header>
  <h1>Wechat-Agent 网关</h1>
  <nav>
    <button data-tab="live" onclick="showTab('live')">实况</button>
    <button data-tab="prompts" onclick="showTab('prompts')">Prompt 编辑</button>
    <button data-tab="personas" onclick="showTab('personas')">人格卡</button>
    <button data-tab="groups" onclick="showTab('groups')">群聊配置</button>
    <button data-tab="runtime" onclick="showTab('runtime')">运行配置</button>
    <button data-tab="env" onclick="showTab('env')">密钥</button>
    <button data-tab="status" onclick="showTab('status')">状态</button>
  </nav>
</header>
<main>
  <div id="sidebar"></div>
  <div id="editor">
    <div id="bar">
      <span id="cur">未选择文件</span>
      <button onclick="saveFile()">保存</button>
      <span id="dirty" style="display:none">● 未保存</span>
      <span id="msg"></span>
    </div>
    <textarea id="ta" oninput="markDirty()"
      placeholder="左侧选择文件开始编辑"></textarea>
  </div>
  <div class="pane" id="pane-live">
    <div class="card"><h2>时序队列</h2><div id="live-queue"></div></div>
    <div class="card"><h2>首页红点</h2><div id="live-home"></div></div>
    <div class="card"><h2>Proxy 流水</h2><div id="live-events"></div></div>
    <div class="card"><h2>原子操作</h2><div id="live-ops"></div></div>
  </div>
  <div class="pane" id="pane-groups"></div>
  <div class="pane" id="pane-runtime"></div>
  <div class="pane" id="pane-env"></div>
  <div class="pane" id="pane-status"></div>
</main>
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
function markDirty() {
  dirty = (document.getElementById('ta').value !== savedContent);
  document.getElementById('dirty').style.display = dirty ? 'inline' : 'none';
}
window.onbeforeunload = () => dirty ? '有未保存的修改' : null;

function showTab(name) {
  curTab = name;
  const editing = (name === 'prompts' || name === 'personas');
  document.getElementById('editor').style.display = editing ? 'flex' : 'none';
  document.getElementById('sidebar').style.display = editing ? '' : 'none';
  for (const p of ['live', 'groups', 'runtime', 'env', 'status'])
    document.getElementById('pane-' + p).style.display =
      (p === name) ? 'block' : 'none';
  for (const b of document.querySelectorAll('nav button'))
    b.classList.toggle('active', b.dataset.tab === name);
  if (editing) loadFiles(name);
  if (name === 'live') loadLive();
  if (name === 'groups') loadGroups();
  if (name === 'runtime') loadRuntime();
  if (name === 'env') loadEnv();
  if (name === 'status') loadStatus();
}

let GROUPS = [], LEVELS = {};
function loadGroups() {
  api('/api/groups').then(j => {
    GROUPS = j.groups; LEVELS = j.levels;
    let h = '<div class="card"><h2>群聊热情度</h2>' +
      '<p style="color:var(--dim)">改动写入对应会话的人格卡，下一次决策即生效，无需重启。</p>' +
      '<table><tr><th>会话</th><th>热情度</th><th>补充规则</th><th></th></tr>';
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
           '<td><button data-sess="' + encodeURIComponent(g.session) +
           '" onclick="setLevel(decodeURIComponent(this.dataset.sess))">保存</button></td></tr>';
    }
    h += '</table></div>';
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
      if (!j.ok) alert(j.error || '保存失败');
      loadGroups();
    }).catch(e => alert(e.message));
}

function loadFiles(which) {
  api('/api/files?dir=' + which).then(j => {
    const sb = document.getElementById('sidebar');
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
  }).catch(e => alert(e.message));
}

function openFile(path) {
  if (dirty && !confirm('当前文件有未保存修改，确定切换？')) return;
  api('/api/file?path=' + encodeURIComponent(path)).then(j => {
    curPath = path;
    savedContent = j.content;
    document.getElementById('ta').value = j.content;
    document.getElementById('cur').textContent = path;
    markDirty();
  }).catch(e => alert(e.message));
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
  }).catch(e => alert(e.message));
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
    let h = '<div class="card"><h2>运行配置（config/runtime.json）</h2><table>';
    for (const [k, t] of RUNTIME_SCHEMA) {
      const v = j.config[k];
      h += '<tr><td>' + k + '</td><td>';
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
    h += '</table><br><button onclick="saveRuntime()">保存</button>' +
         ' <span id="rt_msg"></span></div>';
    p.innerHTML = h;
  }).catch(e => alert(e.message));
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
    .catch(e => alert(e.message));
}

function loadEnv() {
  api('/api/env').then(j => {
    const p = document.getElementById('pane-env');
    let h = '<div class="card"><h2>密钥（workspace/.env，脱敏显示）</h2>' +
            '<table><tr><th>键</th><th>当前值</th>' +
            '<th>新值（留空=不变）</th></tr>';
    for (const item of j.keys)
      h += '<tr><td>' + esc(item.key) + '</td><td>' + esc(item.masked) +
           '</td><td><input type="text" data-envkey="' + esc(item.key) +
           '"></td></tr>';
    h += '<tr><td><input type="text" id="env_newkey" ' +
         'placeholder="NEW_KEY"></td><td></td>' +
         '<td><input type="text" id="env_newval"></td></tr>';
    h += '</table><br><button onclick="saveEnv()">保存</button>' +
         ' <span id="env_msg"></span></div>';
    p.innerHTML = h;
  }).catch(e => alert(e.message));
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
  }).then(() => { loadEnv(); }).catch(e => alert(e.message));
}

function loadStatus() {
  api('/api/status').then(j => {
    const p = document.getElementById('pane-status');
    const block = (title, obj) => '<div class="card"><h2>' + title +
      '</h2>' + (obj == null ? '<p class="dim">暂无数据</p>'
                 : '<pre>' + esc(JSON.stringify(obj, null, 2)) + '</pre>') +
      '</div>';
    let h = '<div class="card"><h2>时序队列（出队顺序）</h2>' +
            queueTable(j.queue) + '</div>';
    h += block('水位（runtime/watermarks.json）', j.watermarks);
    h += '<div class="card"><h2>任务台账（tasks/）</h2>';
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
    h += '</div>';
    p.innerHTML = h;
  }).catch(e => alert(e.message));
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
    document.getElementById('live-queue').innerHTML =
      queueTable(st && st.queue);
    document.getElementById('live-home').innerHTML =
      renderHome(hs && hs.scan);
    if (ev) {
      document.getElementById('live-events').innerHTML =
        renderEvents(ev.events || []);
    }
    if (ops) {
      document.getElementById('live-ops').innerHTML =
        renderOps(ops.ops || []);
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
  if (!ops.length) return '<p>暂无数据</p>';
  let h = '';
  for (const o of ops) {
    const ts = new Date(o.ts * 1000).toLocaleTimeString();
    const detail = Object.entries(o)
      .filter(([k]) => !['ts', 'op'].includes(k))
      .map(([k, v]) => k + '=' + v).join(' ');
    h += '<div class="ev" style="padding:2px 6px;border-bottom:1px solid #21262d">' +
         '<span style="color:var(--dim)">' + ts + '</span> <b>' + esc(o.op) +
         '</b> <span style="color:var(--dim)">' + esc(detail) + '</span></div>';
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
