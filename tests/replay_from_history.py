# -*- coding: utf-8 -*-
"""从真实聊天历史构建 20 组决策场景，同一份 prompt 分别发给 k3 和 gemini。

数据源：workspace/chatlogs/chatlog.db（全量真实消息）
prompt 装配：与线上完全一致——ContextBuilder（config/prompts order.txt 块）
+ MemoryInjector（workspace/memory 真实记忆），build() 产出 [system, user]。

每组场景 = 一个真实会话的一段历史（触发消息前 12 条）+ 1 条真实触发消息；
两类触发：群聊普通 / @陈曦 / 主人风图 / 图片 / 引用 / 私聊，共 20 组。

产出：workspace/gemini_replay/report_history_<ts>.json + .md
（与 test_gemini_replay.py 同格式，网关「模型对比」页直接显示，
  k3 与 gemini 用同一分块渲染、一一对应）

用法：
    python tests/replay_from_history.py            # 20 组 × 1 采样
    python tests/replay_from_history.py --samples 2
    python tests/replay_from_history.py --dry-run  # 只装配不调用
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.decision.prompt.builder import ContextBuilder  # noqa: E402
from src.decision.prompt.persona import PersonaRenderer  # noqa: E402
from src.decision.memory.injector import MemoryInjector  # noqa: E402
from src.decision.memory.store import MemoryStore  # noqa: E402
from src.shared.xml_blocks import extract_blocks  # noqa: E402

CHATLOG = ROOT / "workspace" / "chatlogs" / "chatlog.db"
OUT_DIR = ROOT / "workspace" / "gemini_replay"

# 模型与 Key
KIMI_KEY = None
GEMINI_KEY = os.environ.get("AIXHAN_API_KEY", "")
GEMINI_MODEL = "gemini-3.1-pro-preview"
NEW_MODEL = "gemini-3.6-flash"
OWNER = "风图"
OWNER_NICK = "陈曦"
HISTORY_N = 12            # 每组历史条数
TRIGGER_LABEL = "新消息"   # 【触发原因】（与 proxy 一致）


def load_env():
    global KIMI_KEY
    p = ROOT / "workspace" / ".env"
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k == "KIMI_API_KEY":
                    KIMI_KEY = v
    KIMI_KEY = KIMI_KEY or os.environ.get("KIMI_API_KEY")


# ─────────────────────────── 从 chatlog.db 抽取场景 ───────────────────────────
def load_chatlog():
    conn = sqlite3.connect(str(CHATLOG))
    cur = conn.cursor()
    cur.execute("SELECT session_id, name, is_group FROM sessions")
    sessions = {r[0]: {"name": r[1], "is_group": bool(r[2])} for r in cur.fetchall()}
    cur.execute("""SELECT session_id, seq, sender, content, content_type,
                          is_mine, mentions, ts_captured
                   FROM messages ORDER BY ts_captured, seq""")
    by_session = {}
    for sid, seq, sender, content, ctype, is_mine, mentions, ts in cur.fetchall():
        by_session.setdefault(sid, []).append({
            "sender": sender, "content": content or "", "content_type": ctype,
            "is_mine": bool(is_mine),
            "mentions": (mentions or "").split(",") if mentions else [],
            "ts": ts, "seq": seq,
        })
    conn.close()
    return sessions, by_session


def is_at_me(m):
    content = m["content"] or ""
    if "@所有人" in content:
        return True
    if any(nick and OWNER_NICK in nick for nick in m["mentions"]):
        return True
    return f"@{OWNER_NICK}" in content


def to_msg(m, at_me=False):
    """chatlog 行 → builder 需要的 Message 形对象。"""
    return SimpleNamespace(
        sender=m["sender"], content=m["content"],
        content_type=m["content_type"], is_mine=m["is_mine"],
        at_me=at_me or is_at_me(m),
        mentions=m["mentions"], seq=m["seq"], ts=m["ts"])


def pick_scenarios(by_session, sessions, n=20):
    """从真实消息挑 n 个触发点：按类别配额 + 会话轮转，覆盖六类场景。"""
    quotas = [("群聊普通", 6), ("@陈曦", 5), ("主人私聊", 3),
              ("图片/媒体", 2), ("引用", 2), ("私聊", 2)]

    def cond(label, m, sname, is_group):
        if label == "群聊普通":
            return (is_group and m["content_type"] == "text"
                    and not m["is_mine"] and not is_at_me(m)
                    and len(m["content"].strip()) >= 4)
        if label == "@陈曦":
            return (is_group and m["content_type"] == "text"
                    and not m["is_mine"] and is_at_me(m))
        if label == "主人私聊":
            return (sname == OWNER and not m["is_mine"]
                    and m["content_type"] == "text"
                    and len(m["content"].strip()) >= 4)
        if label == "图片/媒体":
            return (not m["is_mine"] and m["content_type"] == "multimedia")
        if label == "引用":
            return (not m["is_mine"] and m["content_type"] == "quote")
        if label == "私聊":
            return (not is_group and sname != OWNER and not m["is_mine"]
                    and m["content_type"] == "text"
                    and len(m["content"].strip()) >= 4)
        return False

    selected, seen = [], set()
    for label, quota in quotas:
        # 每类候选按会话收集
        by_sid = {}
        for sid, msgs in by_session.items():
            sname = sessions[sid]["name"]
            is_group = sessions[sid]["is_group"]
            for i, m in enumerate(msgs):
                if cond(label, m, sname, is_group):
                    by_sid.setdefault(sid, []).append(i)
        # 会话轮转取样：每个会话轮流出一个，直到本类配额满
        picked = []
        sids = list(by_sid.keys())
        k = 0
        while len(picked) < quota and len([s for s in sids if by_sid[s]]) > 0:
            sid = sids[k % len(sids)]
            if by_sid[sid]:
                i = by_sid[sid].pop(0)
                if (sid, i) not in seen:
                    seen.add((sid, i))
                    picked.append((sid, i, label))
            k += 1
            if k > 20000:
                break
        selected.extend(picked)

    # 兜底补足（不挑类型，取未用过的真实消息）
    if len(selected) < n:
        for sid, msgs in by_session.items():
            for i, m in enumerate(msgs):
                if len(selected) >= n:
                    break
                if (sid, i) in seen or m["is_mine"]:
                    continue
                seen.add((sid, i))
                selected.append((sid, i, "补充"))
            if len(selected) >= n:
                break

    out = []
    for sid, idx, label in selected[:n]:
        msgs = by_session[sid]
        hist = msgs[max(0, idx - HISTORY_N):idx]
        trig = msgs[idx]
        out.append({
            "session": sessions[sid]["name"],
            "is_group": sessions[sid]["is_group"],
            "label": label,
            "history": [to_msg(m) for m in hist],
            "trigger": to_msg(trig),
            "scene": f"m1 {trig['sender']}: "
                     f"{trig['content'][:80].replace(chr(10),' ')}",
        })
    return out



# ─────────────────────────── prompt 装配（与线上同链） ───────────────────────────
def build_prompt(sc, builder, injector):
    memory_block = injector.build_memory_block(
        sc["session"], sc["is_group"], sc["history"], [sc["trigger"]])
    messages = builder.build(
        sc["session"], sc["is_group"], TRIGGER_LABEL,
        sc["history"], [sc["trigger"]],
        running_tasks=[], memory_block=memory_block)
    return messages


# ─────────────────────────── 模型调用 ───────────────────────────
def call_k3(messages, timeout=180):
    """Kimi k3：与 KimiProvider 同路径（api.kimi.com/coding/v1）。"""
    url = "https://api.kimi.com/coding/v1/chat/completions"
    payload = {"model": "k3", "messages": messages, "max_tokens": 2048}
    t0 = time.time()
    r = requests.post(url, headers={
        "Authorization": f"Bearer {KIMI_KEY}",
        "Content-Type": "application/json"}, json=payload, timeout=timeout)
    dt = time.time() - t0
    if r.status_code != 200:
        raise RuntimeError(f"k3 HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    msg = data["choices"][0]["message"]
    return (msg.get("content") or "").strip(), {
        "latency_s": round(dt, 1),
        "model_version": data.get("model", "k3"),
        "thinking": (msg.get("reasoning_content") or "")[:200],
        "usage": data.get("usage", {}),
    }


def call_gemini(messages, model=GEMINI_MODEL, timeout=180):
    """Gemini：中转站 /v1beta generateContent（system + contents 双段）。"""
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    url = (f"https://api.aixhan.com/v1beta/models/{model}:generateContent")
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": 2048},
    }
    t0 = time.time()
    r = requests.post(url, headers={
        "Authorization": f"Bearer {GEMINI_KEY}",
        "Content-Type": "application/json"}, json=payload, timeout=timeout)
    dt = time.time() - t0
    if r.status_code != 200:
        raise RuntimeError(f"gemini HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    return text, {
        "latency_s": round(dt, 1),
        "model_version": data.get("modelVersion", model),
        "finish_reason": data.get("candidates", [{}])[0].get("finishReason", ""),
    }


# ─────────────────────────── 对比（与 test_gemini_replay 同规则） ───────────────────────────
_RE_OPEN = re.compile(r"<(reply|task|tool|silent)(\s[^>]*)?(/?)>")


def decide(text):
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


def analyze(output):
    blocks = extract_blocks(output or "")
    valid = [b for b in blocks if b.valid]
    replies = [b for b in valid if b.tag == "reply"]
    return {
        "raw": output,
        "n_valid": len(valid),
        "bad": [b.error for b in blocks if not b.valid],
        "decision": decide(output),
        "n_replies": len(replies),
    }


# ─────────────────────────── 主流程 ───────────────────────────
def main():
    global GEMINI_KEY, KIMI_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--gemini-key", default=os.environ.get("AIXHAN_API_KEY",
                                                          GEMINI_KEY))
    ap.add_argument("--kimi-key", default=None)
    args = ap.parse_args()
    GEMINI_KEY = args.gemini_key
    load_env()
    if args.kimi_key:
        KIMI_KEY = args.kimi_key
    if not KIMI_KEY:
        sys.exit("缺少 KIMI_API_KEY（workspace/.env 或 --kimi-key）")

    sessions, by_session = load_chatlog()
    scenarios = pick_scenarios(by_session, sessions, 20)
    print(f"场景: {len(scenarios)} 组")
    for i, sc in enumerate(scenarios, 1):
        print(f"  {i:02d} [{sc['label']}] {sc['session']} | {sc['scene'][:50]}")

    if args.dry_run:
        return

    builder = ContextBuilder(personas=PersonaRenderer(), owner=OWNER)
    injector = MemoryInjector(MemoryStore(str(ROOT / "workspace" / "memory")))

    # 预装配 20 组 prompt（打印长度抽查）
    prompts = [build_prompt(sc, builder, injector) for sc in scenarios]

    print(f"\n目标: k3(官方) vs {GEMINI_MODEL}(中转站) | 每场景 {args.samples} 采样 | 并发 {args.concurrency}\n")

    results = []
    jobs = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for i, (sc, msgs) in enumerate(zip(scenarios, prompts)):
            h = hashlib.md5((msgs[0]["content"] + "\x00" +
                             msgs[1]["content"]).encode()).hexdigest()[:12]
            for s in range(args.samples):
                jobs.append(ex.submit(_one_job, sc, msgs, h, s, i))
        for fut in as_completed(jobs):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                results.append({"error": str(e), "hash": "?"})

    results.sort(key=lambda r: (r.get("label", ""), r.get("idx", 0)))
    _write_reports(results, scenarios, args)
    _print_summary(results)


def _one_job(sc, msgs, h, sample, idx):
    try:
        k3_text, k3_meta = call_k3(msgs)
        g_text, g_meta = call_gemini(msgs, model=GEMINI_MODEL)
        n_text, n_meta = call_gemini(msgs, model=NEW_MODEL)
    except Exception as e:  # noqa: BLE001
        return {"hash": h, "sample": sample, "idx": idx,
                "label": sc["label"], "session": sc["session"],
                "error": str(e)}
    ka, ga, na = analyze(k3_text), analyze(g_text), analyze(n_text)
    comps = [{
        "same_decision": ka["decision"] == ga["decision"],
        "k3_valid": ka["n_valid"] > 0 and not ka["bad"],
        "gemini_valid": ga["n_valid"] > 0 and not ga["bad"],
        "new_valid": na["n_valid"] > 0 and not na["bad"],
        "k3": {"decision": ka["decision"], "n_valid": ka["n_valid"]},
        "gemini": {"decision": ga["decision"], "n_valid": ga["n_valid"]},
        "new": {"decision": na["decision"], "n_valid": na["n_valid"]},
    }]
    return {
        "hash": h, "sample": sample, "idx": idx,
        "label": sc["label"], "session": sc["session"],
        "scene": sc["scene"], "round": 0,
        "user_len": len(msgs[1]["content"]),
        "k3_outputs": [k3_text],
        "k3_meta": k3_meta,
        "gemini_text": g_text,
        "meta": g_meta,
        "new_text": n_text,
        "new_meta": n_meta,
        "comparisons": comps,
        "same_decision": ka["decision"] == ga["decision"],
        "k3_analysis": ka,
        "gemini_analysis": ga,
        "new_analysis": na,
        "match_any": ka["decision"] == ga["decision"],
    }


def _write_reports(results, scenarios, args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = OUT_DIR / f"report_history_{ts}"
    data = {
        "model": GEMINI_MODEL,
        "new_model": NEW_MODEL,
        "samples": args.samples,
        "generated_at": datetime.now().isoformat(),
        "n_unique_prompts": len(scenarios),
        "results": results,
    }
    with open(f"{base}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    _write_markdown(f"{base}.md", data, scenarios)
    print(f"\n报告已写入: {base}.json / {base}.md")


def _write_markdown(path, data, scenarios):
    lines = ["# 真实历史回放：k3 vs Gemini（20 组）", "",
             f"- 生成时间: {data['generated_at']}",
             f"- 模型: k3（官方） vs `{data['model']}`（中转站 /v1beta）",
             f"- 场景数: {len(scenarios)}，每场景采样 {data['samples']} 次", ""]
    ok = 0
    total = 0
    for r in data["results"]:
        if r.get("error"):
            lines.append(f"## ❌ {r.get('label')} {r.get('session')} 调用失败: {r['error']}")
            continue
        ok += 1 if r["match_any"] else 0
        total += 1
        lines.append(f"## [{r['label']}] {r['session']} | {r['scene']}")
        lines.append(f"- k3[{r['k3_analysis']['decision']}] vs "
                     f"gemini[{r['gemini_analysis']['decision']}] "
                     f"{'✅' if r['match_any'] else '❌'}")
        lines.append("```k3\n" + r["k3_outputs"][0] + "\n```")
        lines.append("```gemini\n" + r["gemini_text"] + "\n```")
        lines.append("")
    lines.insert(4, f"- 决策一致率: {ok}/{total}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _print_summary(results):
    print("═" * 76)
    print(f"{'#':>2s} {'场景':8s} {'会话':10s} {'k3':16s} {'Gemini':16s} {'耗时k3/g':>10s}")
    print("─" * 76)
    for r in sorted(results, key=lambda x: (x.get("idx", 0), x.get("sample", 0))):
        if r.get("error"):
            print(f"{r.get('idx',0)+1:2d} ERROR: {r['error'][:50]}")
            continue
        mark = "✅" if r["match_any"] else "❌"
        k3s = r["k3_meta"].get("latency_s", 0)
        gs = r["meta"].get("latency_s", 0)
        print(f"{r['idx']+1:2d} {mark} {r['label']:8s} {r['session']:10s} "
              f"{r['k3_analysis']['decision']:16s} {r['gemini_analysis']['decision']:16s} "
              f"{k3s:>4.0f}s/{gs:>4.0f}s")
    print("═" * 76)


if __name__ == "__main__":
    main()
