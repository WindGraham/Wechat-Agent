# -*- coding: utf-8 -*-
"""gateway/api/replay.py — 模型回放对比报告 API 蓝图。

数据源：workspace/gemini_replay/report_*.json（tests/test_gemini_replay.py 产出）
场景还原：workspace/runtime/proxy_events.jsonl（按 system+user 哈希回配对，
把每组的「新消息/任务回执」场景文本附到结果上，前端直接展示）

路由：
  /api/replay/reports        报告列表（新→旧）
  /api/replay/report?name=   单份报告（含场景还原与逐组对比摘要）
"""

import hashlib
import json
import os
import re

from flask import Blueprint, jsonify, request

from src.shared.xml_blocks import extract_blocks  # noqa: E402

REPLAY_DIR_NAME = "gemini_replay"
EVENTS_REL = os.path.join("workspace", "runtime", "proxy_events.jsonl")

# 事件流 + 场景哈希映射缓存：/api/replay/report 每次全量读 proxy_events.jsonl
# 及其归档会随归档无限增长而线性变慢，这里按"文件路径 + mtime + size"签名缓存，
# 只有事件文件变化才重算。
_EVENTS_CACHE = {"sig": None, "scene_map": None}

_RE_OPEN = re.compile(r"<(reply|task|tool|silent)(\s[^>]*)?(/?)>")


def _decision(text):
    """从模型产出归纳动作决策（与测试脚本同规则）。"""
    tags = [m.group(1) for m in _RE_OPEN.finditer(text or "")]
    if not tags:
        return "invalid"
    if set(tags) == {"silent"}:
        return "silent"
    if "task" in tags:
        return "task" + ("+reply" if "reply" in tags else "")
    if "tool" in tags:
        return "tool"
    if "reply" in tags:
        return f"reply×{tags.count('reply')}"
    return "mixed"


_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_TEXT_RE = re.compile(r"<text>(.*?)</text>", flags=re.S)
_QUOTE_RE = re.compile(r'<quote\s+match="([^"]*)"\s*/?>')
_FILE_RE = re.compile(r'<file\s+path="([^"]*)"\s*/?>')
_IMAGE_RE = re.compile(r'<image\s+path="([^"]*)"\s*/?>')


def _blocks_meta(text):
    """用项目自己的 xml_blocks 解析器把模型输出拆成块结构（与 Proxy 同规则）。

    返回 [{tag, attrs, self_closing, valid, error, inner, texts, quotes,
           files, images}]——<reply> 的 texts 即拟人拆句的逐条消息。
    """
    out = []
    for b in extract_blocks(text or ""):
        attrs = {m.group(1): m.group(2)
                 for m in _ATTR_RE.finditer(b.attrs or "")}
        out.append({
            "tag": b.tag,
            "attrs": attrs,
            "self_closing": b.self_closing,
            "valid": b.valid,
            "error": b.error or "",
            "inner": b.inner or "",
            "texts": [t.strip() for t in _TEXT_RE.findall(b.raw_inner or "")],
            "quotes": [q for q in _QUOTE_RE.findall(b.raw_inner or "")],
            "files": [f for f in _FILE_RE.findall(b.raw_inner or "")],
            "images": [i for i in _IMAGE_RE.findall(b.raw_inner or "")],
        })
    return out


def _scene(user_text):
    """从 user prompt 提取「新消息」区首行 / 任务回执摘要。"""
    m = re.search(r"【新消息】.*?\n(.*?)(?:\n# 执行中|\n$)", user_text or "",
                  flags=re.S)
    if m:
        return " ".join(m.group(1).split())[:200]
    m = re.search(r"【任务回执】.*?\n(.*?)(?:\n|$)", user_text or "", flags=re.S)
    if m:
        return "任务回执: " + " ".join(m.group(1).split())[:120]
    return ""


def _load_events(root):
    """读取 proxy_events.jsonl 及其归档（proxy_events.jsonl.<ts>）全部事件。

    事件流超限时归档轮转（proxy.py _rotate_events），旧数据完整保留在
    归档文件里——场景还原必须把归档也读进来，否则旧报告的 prompt 还原不了。
    """
    events = []
    base = os.path.join(root, EVENTS_REL)
    for path in sorted(_event_files(base)):
        try:
            with open(path, encoding="utf-8") as f:
                for l in f:
                    l = l.strip()
                    if l:
                        events.append(json.loads(l))
        except (OSError, ValueError):
            continue
    return events


def _event_files(base):
    """当前事件文件 + 同目录下所有 proxy_events.jsonl.<ts> 归档。"""
    yield base
    d = os.path.dirname(base)
    name = os.path.basename(base)
    if not os.path.isdir(d):
        return
    for fn in sorted(os.listdir(d)):
        if fn.startswith(name + "."):
            yield os.path.join(d, fn)


def _hash_scene_map(events):
    """proxy_events → {hash12: 场景文本}（与测试脚本同哈希算法）。"""
    pending, out = {}, {}
    for e in events:
        if e.get("type") == "prompt":
            pending[(e["session"], e.get("round"))] = e
        elif e.get("type") == "llm_output":
            p = pending.pop((e["session"], e.get("round")), None)
            if p is None:
                continue
            h = hashlib.md5((p["system"] + "\x00" + p["user"]).encode()
                            ).hexdigest()[:12]
            out.setdefault(h, _scene(p["user"]))
    return out


def _events_signature(base):
    """事件文件集合的签名：(path, mtime, size) 元组列表的 tuple。"""
    sig = []
    for path in _event_files(base):
        try:
            st = os.stat(path)
            sig.append((path, st.st_mtime, st.st_size))
        except OSError:
            continue
    return tuple(sig)


def _load_scene_map_cached(root):
    """带签名的场景哈希映射加载：事件文件没变时命中缓存，不重读全量。"""
    base = os.path.join(root, EVENTS_REL)
    sig = _events_signature(base)
    if _EVENTS_CACHE["sig"] == sig and _EVENTS_CACHE["scene_map"] is not None:
        return _EVENTS_CACHE["scene_map"]
    scene_map = _hash_scene_map(_load_events(root))
    _EVENTS_CACHE["sig"] = sig
    _EVENTS_CACHE["scene_map"] = scene_map
    return scene_map


def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    bp = Blueprint("replay_api", __name__)
    rep_root = os.path.join(root, "workspace", REPLAY_DIR_NAME)

    def _list_reports():
        if not os.path.isdir(rep_root):
            return []
        files = []
        for fn in sorted(os.listdir(rep_root)):
            if not fn.startswith("report_") or not fn.endswith(".json"):
                continue
            full = os.path.join(rep_root, fn)
            try:
                with open(full, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            results = data.get("results", [])
            n = len(results)
            ok = sum(1 for r in results if r.get("match_any"))
            files.append({
                "name": fn,
                "mtime": os.path.getmtime(full),
                "model": data.get("model", "?"),
                "generated_at": data.get("generated_at", ""),
                "n_unique": data.get("n_unique_prompts", 0),
                "n_samples": n,
                "match": ok,
                "match_rate": round(ok / n, 2) if n else 0,
            })
        files.sort(key=lambda f: -f["mtime"])
        return files

    @bp.route("/api/replay/reports")
    def api_replay_reports():
        return jsonify({"ok": True, "reports": _list_reports()})

    @bp.route("/api/replay/report")
    def api_replay_report():
        name = request.args.get("name", "")
        full = os.path.realpath(os.path.join(rep_root, name or ""))
        if not full.startswith(os.path.realpath(rep_root) + os.sep) \
                or not os.path.isfile(full):
            return jsonify({"ok": False, "error": "not found"}), 404
        try:
            with open(full, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return jsonify({"ok": False, "error": "parse failed"}), 500

        scene_map = _load_scene_map_cached(root)

        # 按 hash 聚合样本，附场景与逐组摘要
        groups = {}
        for r in data.get("results", []):
            h = r.get("hash", "?")
            g = groups.setdefault(h, {
                "hash": h,
                "session": r.get("session", ""),
                "round": r.get("round"),
                "label": r.get("label", ""),
                # 报告自带 scene（如 replay_from_history 构建的真实场景）优先，
                # 否则回退到 proxy_events 哈希配对
                "scene": r.get("scene") or scene_map.get(h, ""),
                "k3_outputs": [{
                    "text": o,
                    "blocks": _blocks_meta(o),
                } for o in r.get("k3_outputs", [])],
                "samples": [],
            })
            if r.get("error"):
                g["samples"].append({"error": r["error"]})
                continue
            comps = r.get("comparisons", [])
            sample_item = {
                "latency_s": r.get("meta", {}).get("latency_s"),
                "model_version": r.get("meta", {}).get("model_version", ""),
                "gemini_text": r.get("gemini_text", ""),
                "gemini_blocks": _blocks_meta(r.get("gemini_text", "")),
                "gemini_decision": comps[0]["gemini"]["decision"]
                if comps else "?",
                "gemini_valid": comps[0]["gemini_valid"] if comps else False,
                "match_any": r.get("match_any", False),
                "per_variant": [{
                    "same": c["same_decision"],
                    "k3_decision": c["k3"]["decision"],
                    "k3_valid": c["k3_valid"],
                } for c in comps],
            }
            if "new_text" in r:
                sample_item["new_text"] = r.get("new_text", "")
                sample_item["new_blocks"] = _blocks_meta(r.get("new_text", ""))
                sample_item["new_latency_s"] = r.get("new_meta", {}).get("latency_s")
                sample_item["new_model_version"] = r.get("new_meta", {}).get("model_version", "")
                sample_item["new_valid"] = comps[0].get("new_valid", False) if comps else False
            g["samples"].append(sample_item)

        # k3 决策摘要（去重变体）
        for g in groups.values():
            k3dec = set()
            for r in data.get("results", []):
                if r.get("hash") != g["hash"] or r.get("error"):
                    continue
                for c in r.get("comparisons", []):
                    k3dec.add(c["k3"]["decision"])
            g["k3_decisions"] = sorted(k3dec)

        return jsonify({
            "ok": True,
            "name": name,
            "model": data.get("model", "?"),
            "new_model": data.get("new_model", ""),
            "samples": data.get("samples", 0),
            "generated_at": data.get("generated_at", ""),
            "groups": list(groups.values()),
        })

    return bp
