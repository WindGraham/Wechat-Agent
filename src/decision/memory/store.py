# -*- coding: utf-8 -*-
"""decision/memory/store.py — MemoryStore：长期记忆的存取逻辑。

只实现"读写逻辑"，**不创建任何真实 memory 文件**——目录/文件在首次
实际写入时才生成（对齐 media/ 惰性建目录的惯例）。设计见
docs/DESIGN_DECISION_TOOL_ARCHITECTURE.md §三。

目录排布（对齐 media/ 按会话分目录的惯例）：
    workspace/memory/
    ├── global.json            # L0 全局（agent 本体相关）
    ├── users/<用户昵称>.json   # L1 按用户（每用户一文件）
    └── sessions/<会话名>.json  # L2 按会话（每会话一文件）

条目字段：id / key / content / scope / source / ts / updated_at /
          confidence / ref_msg（见定稿 §四）

并发安全：每文件一个锁 + tmp+replace 原子写（对齐 _save_watermarks）。
"""

import hashlib
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("decision.memory.store")

# 默认根目录（相对仓库根 workspace/memory）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_MEMORY_ROOT = os.path.join(PROJECT_ROOT, "workspace", "memory")

# 目录/上限常量（定稿 §七）
GLOBAL_LIMIT = 500          # 全局条目上限
USER_LIMIT = 50             # 每用户条目上限
SESSION_LIMIT = 50          # 每会话条目上限
_SAFE_RE = re.compile(r'[\\/:*?"<>|]')     # 文件名非法字符（对齐 media.sanitize）


def sanitize_name(name: str) -> str:
    """昵称/会话名 → 合法文件名（与 media_archive.sanitize_session 同规则）。"""
    return _SAFE_RE.sub("_", name or "") or "unnamed"


class MemoryStore:
    """长期记忆存取。所有方法线程安全；写入用 tmp+replace 原子替换。"""

    def __init__(self, root: str = DEFAULT_MEMORY_ROOT,
                 clock=time.time, lock_factory=threading.Lock):
        self._root = root
        self._clock = clock
        # 每文件一个锁：不同文件可并发，同文件串行
        self._locks = {}
        self._locks_guard = threading.Lock()

    # ---------------------------------------------------------------- 路径
    def _file_lock(self, path: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(path, threading.Lock())

    def _global_path(self) -> str:
        return os.path.join(self._root, "global.json")

    def _user_path(self, user: str) -> str:
        return os.path.join(self._root, "users", sanitize_name(user) + ".json")

    # ---------------------------------------------------------------- 别名
    def resolve_user(self, display_name: str):
        """把"会话里出现的昵称"解析到用户（支持别名）。

        返回 (canonical_name, path) 或 (None, None)：
          1. 先查主昵称文件（users/<昵称>.json）
          2. 查不到 → 扫描所有用户文件的 aliases 列表，匹配则返回其主用户
        用于召回：会话里出现"图图"，若风图有别名"图图"，则召回风图的记忆。
        """
        name = (display_name or "").strip()
        if not name:
            return None, None
        # 1. 主昵称直接命中
        path = self._user_path(name)
        data = self._read_json(path)
        if data.get("facts") or data.get("aliases") is not None:
            return name, path
        # 2. 别名反查
        users_dir = os.path.join(self._root, "users")
        try:
            for fn in os.listdir(users_dir):
                if not fn.endswith(".json"):
                    continue
                p = os.path.join(users_dir, fn)
                d = self._read_json(p)
                aliases = d.get("aliases") or []
                if any(self._norm(a) == self._norm(name) for a in aliases):
                    canonical = d.get("user") or fn[:-5]
                    return canonical, p
        except OSError:
            pass
        return None, None

    def add_alias(self, user: str, alias: str) -> bool:
        """给用户加别名（昵称/别称标签）。写入用户文件 aliases 列表。"""
        alias = (alias or "").strip()
        user = (user or "").strip()
        if not alias or not user:
            return False
        path = self._user_path(user)
        with self._file_lock(path):
            data = self._read_json(path)
            data.setdefault("user", user)
            aliases = data.setdefault("aliases", [])
            if not any(self._norm(a) == self._norm(alias) for a in aliases):
                aliases.append(alias)
            data["updated_at"] = self._clock()
            self._write_json(path, data)
            return True

    @staticmethod
    def _norm(s: str) -> str:
        """轻量归一化：去空白 + 小写（别名匹配用）。"""
        return re.sub(r"\s+", "", s or "").lower()

    def _session_path(self, session: str) -> str:
        return os.path.join(self._root, "sessions",
                            sanitize_name(session) + ".json")

    # ---------------------------------------------------------------- 读
    def _read_json(self, path: str) -> dict:
        """读 JSON 文件；不存在返回空结构（不创建文件）。"""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _facts_of(self, path: str) -> list:
        data = self._read_json(path)
        return data.get("facts", [])

    # ---------------------------------------------------------------- 写
    def _write_json(self, path: str, data: dict):
        """原子写：tmp + os.replace（对齐 _save_watermarks）。
        首次写入时才建目录（惰性，不预先创建）。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ---------------------------------------------------------------- id
    @staticmethod
    def _id_prefix(kind: str, name: str) -> str:
        """id 前缀：g（全局）/ u_<hash>（用户）/ s_<hash>（会话）。
        hash 取昵称/会话名 sha1 前 6 位，防重名且确定性。"""
        if kind == "g":
            return "g"
        h = hashlib.sha1((name or "").encode("utf-8")).hexdigest()[:6]
        return f"{kind}_{h}"

    # ---------------------------------------------------------------- 定位
    def _locate(self, scope: str, user: str, session: str):
        """按 scope 定位文件与 id 前缀。
        scope: "global" | "user" | "session"（agent 传入或由当前上下文推断）。"""
        if scope == "global":
            return self._global_path(), self._id_prefix("g", "")
        if scope == "user":
            return self._user_path(user), self._id_prefix("u", user)
        return self._session_path(session), self._id_prefix("s", session)

    # ---------------------------------------------------------------- 操作
    def add(self, content: str, key: str = "", scope: str = "global",
            user: str = "", session: str = "", source: str = "",
            ref_msg: str = "", confidence: float = 0.9) -> dict:
        """新增记忆条目。返回新条目 dict；失败抛异常。

        去重：同文件内 key+content 高度相似（normalize 后相等）→ 更新原条目。
        上限：超限淘汰"最旧 + 最低置信"。
        """
        content = (content or "").strip()
        if not content:
            raise ValueError("memory add: content 为空")
        path, prefix = self._locate(scope, user, session)
        with self._file_lock(path):
            data = self._read_json(path)
            facts = data.setdefault("facts", [])
            now = self._clock()

            # 去重：同 key + 内容相似 → 更新
            norm = self._norm(content)
            for f in facts:
                if f.get("key") == key and norm and \
                        self._norm(f.get("content", "")) == norm:
                    f["content"] = content
                    f["updated_at"] = now
                    f["confidence"] = max(f.get("confidence", 0.0),
                                          confidence)
                    data["updated_at"] = now
                    self._write_json(path, data)
                    return f

            # 新增
            seq = len(facts) + 1
            entry = {
                "id": f"{prefix}_{seq}",
                "key": key or "general",
                "content": content,
                "scope": scope,
                "source": source or (session or user or "unknown"),
                "ts": now,
                "updated_at": now,
                "confidence": float(confidence),
            }
            if ref_msg:
                entry["ref_msg"] = ref_msg
            facts.append(entry)
            # 上限淘汰（最旧 + 最低置信）
            limit = self._limit_for(scope)
            while len(facts) > limit:
                idx = min(range(len(facts)),
                          key=lambda i: (facts[i].get("confidence", 0),
                                         facts[i].get("updated_at", 0)))
                facts.pop(idx)
            data["updated_at"] = now
            self._write_json(path, data)
            return entry

    @staticmethod
    def _norm(s: str) -> str:
        """轻量归一化：去空白 + 小写（去重用，够用即可）。"""
        return re.sub(r"\s+", "", s or "").lower()

    @staticmethod
    def _limit_for(scope: str) -> int:
        return {"global": GLOBAL_LIMIT,
                "user": USER_LIMIT,
                "session": SESSION_LIMIT}.get(scope, GLOBAL_LIMIT)

    def read(self, key: str, scope: str = "global", user: str = "",
             session: str = "") -> list:
        """按 key 查询（精确匹配 key）。返回匹配条目列表。"""
        path, _ = self._locate(scope, user, session)
        facts = self._facts_of(path)
        return [f for f in facts if f.get("key") == key]

    def list_scope(self, scope: str = "global", user: str = "",
                   session: str = "", limit: int = 500) -> list:
        """按 scope 全量取条目（注入器用，不按关键词过滤）。
        scope: global / user / session / all。
        返回带 _file 标注的条目列表（按 updated_at 倒序）。"""
        paths = self._paths_for(scope, user, session)
        hits = []
        for path in paths:
            for f in self._facts_of(path):
                f = dict(f)
                d = os.path.dirname(path)
                f["_file"] = "global" if d == self._root \
                    else os.path.basename(d)
                hits.append(f)
        hits.sort(key=lambda f: -f.get("updated_at", 0))
        return hits[:limit]

    def search(self, keyword: str, scope: str = "all", user: str = "",
               session: str = "", limit: int = 10) -> list:
        """按关键词模糊检索 content/key。
        scope=all 时跨 global+users+sessions 检索，带来源标注。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        kw = self._norm(keyword)
        hits = []
        paths = self._paths_for(scope, user, session)
        for path in paths:
            for f in self._facts_of(path):
                hay = self._norm(f.get("content", "")) + \
                    self._norm(f.get("key", ""))
                if kw and kw in hay:
                    f = dict(f)
                    d = os.path.dirname(path)
                    f["_file"] = "global" if d == self._root \
                        else os.path.basename(d)
                    hits.append(f)
        hits.sort(key=lambda f: -f.get("updated_at", 0))
        return hits[:limit]

    def _paths_for(self, scope: str, user: str, session: str) -> list:
        """检索范围对应的文件列表（不创建，仅返回存在路径）。"""
        if scope == "global":
            return [self._global_path()]
        if scope == "user":
            return [self._user_path(user)]
        if scope == "session":
            return [self._session_path(session)]
        # all：global + 所有用户文件 + 所有会话文件（遍历目录）
        paths = [self._global_path()]
        for sub in ("users", "sessions"):
            base = os.path.join(self._root, sub)
            try:
                for fn in sorted(os.listdir(base)):
                    if fn.endswith(".json"):
                        paths.append(os.path.join(base, fn))
            except OSError:
                pass
        return paths

    def update(self, fact_id: str, content: str = None,
               confidence: float = None) -> bool:
        """按 id 更新条目（content 或 confidence）。找不到返回 False。"""
        path = self._find_by_id(fact_id)
        if path is None:
            return False
        with self._file_lock(path):
            data = self._read_json(path)
            for f in data.get("facts", []):
                if f.get("id") == fact_id:
                    if content is not None:
                        f["content"] = content.strip()
                    if confidence is not None:
                        f["confidence"] = float(confidence)
                    f["updated_at"] = self._clock()
                    data["updated_at"] = self._clock()
                    self._write_json(path, data)
                    return True
        return False

    def delete(self, fact_id: str) -> bool:
        """按 id 删除条目。找不到返回 False。"""
        path = self._find_by_id(fact_id)
        if path is None:
            return False
        with self._file_lock(path):
            data = self._read_json(path)
            facts = data.get("facts", [])
            new = [f for f in facts if f.get("id") != fact_id]
            if len(new) == len(facts):
                return False
            data["facts"] = new
            data["updated_at"] = self._clock()
            self._write_json(path, data)
            return True

    def _find_by_id(self, fact_id: str):
        """按 id 前缀定位文件（g→global, u→users/*, s→sessions/*）。
        遍历对应目录查找（文件量小，够用）。"""
        if fact_id.startswith("g"):
            p = self._global_path()
            return p if os.path.exists(p) else None
        prefix = fact_id.split("_")[0] if "_" in fact_id else ""
        if prefix in ("u", "s"):
            sub = "users" if prefix == "u" else "sessions"
            base = os.path.join(self._root, sub)
            try:
                for name in os.listdir(base):
                    if not name.endswith(".json"):
                        continue
                    p = os.path.join(base, name)
                    for f in self._facts_of(p):
                        if f.get("id") == fact_id:
                            return p
            except OSError:
                return None
        return None
