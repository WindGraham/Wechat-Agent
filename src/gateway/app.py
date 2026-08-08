# -*- coding: utf-8 -*-
"""gateway/app.py — 网关 Web 管理面（Flask）。

按 docs/GATEWAY.md：网关只"读文件/写文件 + 读状态接口"，
不调用任何层的函数；配置变更只落文件，各层按 mtime 热读。

- Prompt 编辑器：config/prompts/（按 order.txt 顺序，标注 system/user 分组）
  与 config/personas/（yaml 人格卡）的查看/编辑
- 运行配置：config/runtime.json 表单化读写（字段按 CONTRACTS.md §五 白名单）
- 密钥：workspace/.env 查看（脱敏：前4后2）与更新；只落本机文件
- 状态页：workspace/runtime/ 的 queue.json / watermarks.json 与 tasks/ 台账摘要

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


def create_app(project_root=None):
    """Flask 应用工厂。project_root 指向仓库根（含 config/ 与 workspace/），
    缺省取仓库根；测试传 tmp 目录副本。main.py 可用线程挂载本工厂产物。"""
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

    return app


# ---------------------------------------------------------------- 列表逻辑
def _read_json(path):
    """读 JSON 文件；不存在/解析失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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
  body { font-family: sans-serif; margin: 0; color: #222; }
  header { background: #2b3a4a; color: #fff; padding: 8px 16px; }
  header h1 { font-size: 16px; margin: 0; display: inline-block; }
  nav { display: inline-block; margin-left: 24px; }
  nav button { margin-right: 6px; padding: 4px 12px; }
  main { display: flex; height: calc(100vh - 40px); }
  #sidebar { width: 280px; overflow: auto; border-right: 1px solid #ccc;
             padding: 8px; }
  #sidebar .f { padding: 3px 6px; cursor: pointer; border-radius: 4px;
                font-size: 13px; }
  #sidebar .f:hover { background: #eef; }
  #sidebar .f.active { background: #dbe7ff; }
  #sidebar .tag { font-size: 11px; color: #fff; border-radius: 3px;
                  padding: 0 4px; margin-right: 4px; }
  .tag.system { background: #587; } .tag.user { background: #975; }
  .tag.persona { background: #759; } .tag.order { background: #555; }
  #editor { flex: 1; display: flex; flex-direction: column; padding: 8px; }
  #editor textarea { flex: 1; font-family: monospace; font-size: 13px;
                     width: 100%; box-sizing: border-box; }
  #bar { padding: 6px 0; }
  #dirty { color: #c60; font-weight: bold; margin-left: 12px; }
  .pane { flex: 1; overflow: auto; padding: 12px; display: none; }
  table { border-collapse: collapse; }
  td, th { border: 1px solid #ccc; padding: 4px 8px; font-size: 13px; }
  input[type=text], input[type=number] { width: 220px; }
  pre { background: #f6f6f6; padding: 8px; font-size: 12px; }
  #msg { margin-left: 12px; color: #282; }
</style>
</head>
<body>
<header>
  <h1>Wechat-Agent 网关</h1>
  <nav>
    <button onclick="showTab('prompts')">Prompt 编辑</button>
    <button onclick="showTab('personas')">人格卡</button>
    <button onclick="showTab('runtime')">运行配置</button>
    <button onclick="showTab('env')">密钥</button>
    <button onclick="showTab('status')">状态</button>
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
  <div class="pane" id="pane-runtime"></div>
  <div class="pane" id="pane-env"></div>
  <div class="pane" id="pane-status"></div>
</main>
<script>
let curPath = null, savedContent = "", dirty = false;

function api(path, opts) {
  return fetch(path, opts).then(r => r.json().then(j => {
    if (!r.ok) throw new Error(j.error || r.status);
    return j;
  }));
}
function markDirty() {
  dirty = (document.getElementById('ta').value !== savedContent);
  document.getElementById('dirty').style.display = dirty ? 'inline' : 'none';
}
window.onbeforeunload = () => dirty ? '有未保存的修改' : null;

function showTab(name) {
  const editing = (name === 'prompts' || name === 'personas');
  document.getElementById('editor').style.display = editing ? 'flex' : 'none';
  for (const p of ['runtime', 'env', 'status'])
    document.getElementById('pane-' + p).style.display =
      (p === name) ? 'block' : 'none';
  if (editing) loadFiles(name);
  if (name === 'runtime') loadRuntime();
  if (name === 'env') loadEnv();
  if (name === 'status') loadStatus();
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
                    '</span>' + ord + f.name;
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
    let h = '<h3>运行配置（config/runtime.json）</h3><table>';
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
             (v ?? '') + '">';
      else
        h += '<input type="number" id="rt_' + k + '" value="' +
             (v ?? '') + '">';
      h += '</td></tr>';
    }
    h += '</table><br><button onclick="saveRuntime()">保存</button>' +
         ' <span id="rt_msg"></span>';
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
    let h = '<h3>密钥（workspace/.env，脱敏显示）</h3><table>' +
            '<tr><th>键</th><th>当前值</th><th>新值（留空=不变）</th></tr>';
    for (const item of j.keys)
      h += '<tr><td>' + item.key + '</td><td>' + item.masked + '</td>' +
           '<td><input type="text" data-envkey="' + item.key + '"></td></tr>';
    h += '<tr><td><input type="text" id="env_newkey" ' +
         'placeholder="NEW_KEY"></td><td></td>' +
         '<td><input type="text" id="env_newval"></td></tr>';
    h += '</table><br><button onclick="saveEnv()">保存</button>' +
         ' <span id="env_msg"></span>';
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
    const block = (title, obj) => '<h3>' + title + '</h3>' +
      (obj == null ? '<p>暂无数据</p>'
                   : '<pre>' + JSON.stringify(obj, null, 2) + '</pre>');
    let h = block('队列（runtime/queue.json）', j.queue) +
            block('水位（runtime/watermarks.json）', j.watermarks);
    h += '<h3>任务台账（tasks/）</h3>';
    if (!j.tasks.length) h += '<p>暂无数据</p>';
    else {
      h += '<table><tr><th>日期</th><th>task_id</th><th>会话</th>' +
           '<th>描述</th><th>状态</th></tr>';
      for (const t of j.tasks)
        h += '<tr><td>' + t.date + '</td><td>' + t.task_id + '</td><td>' +
             t.session + '</td><td>' + t.desc + '</td><td>' + t.status +
             '</td></tr>';
      h += '</table>';
    }
    p.innerHTML = h;
  }).catch(e => alert(e.message));
}

showTab('prompts');
</script>
</body>
</html>
"""
