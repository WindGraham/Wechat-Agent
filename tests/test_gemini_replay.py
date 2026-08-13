# -*- coding: utf-8 -*-
"""决策层模型回放对比测试：把历史喂给 k3 的 prompt 回放给 Gemini 3.1 Pro Preview。

数据源：workspace/runtime/proxy_events.jsonl（决策层事件流，含完整 prompt 与
k3 产出）。对每个唯一 prompt（按 system+user 内容去重）：

  1. 原样回放给中转站 gemini-3.1-pro-preview（/v1beta generateContent，
     systemInstruction + contents 双段，与决策层 system/user 消息位对应）
  2. 用项目自己的 xml_blocks 解析器分别解析 k3 产出与 Gemini 产出
  3. 逐项对比：动作决策（reply/silent/task/tool）、XML 合法块数、
     reply 数量、ref/session 属性、文本内容

用法：
    python tests/test_gemini_replay.py                     # 默认跑一遍
    python tests/test_gemini_replay.py --samples 2         # 每个 prompt 采 2 次
    python tests/test_gemini_replay.py --concurrency 4     # 并发
    AIXHAN_API_KEY=sk-xxx python tests/test_gemini_replay.py

产出：workspace/gemini_replay/report_<时间戳>.json + .md
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.shared.xml_blocks import extract_blocks  # noqa: E402

EVENTS_PATH = ROOT / "workspace" / "runtime" / "proxy_events.jsonl"
OUT_DIR = ROOT / "workspace" / "gemini_replay"

# 与 scripts/label_emojis.py 同源的默认 key；可用环境变量覆盖
DEFAULT_KEY = os.environ.get("AIXHAN_API_KEY", "")
BASE_URL = "https://api.aixhan.com/v1beta/models/{model}:generateContent"


# ─────────────────────────────── 数据提取 ───────────────────────────────
def load_events(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def pair_prompt_outputs(events):
    """顺序配对：llm_output 归到最近一个未配对的同 (session, round) prompt。"""
    pairs = []
    pending = {}
    for e in events:
        if e["type"] == "prompt":
            pending[(e["session"], e.get("round"))] = e
        elif e["type"] == "llm_output":
            key = (e["session"], e.get("round"))
            p = pending.pop(key, None)
            if p is not None:
                pairs.append((p, e))
    return pairs


def unique_prompts(pairs):
    """按 system+user 内容哈希去重，返回 {hash: {prompt, k3_outputs:[...]}}。"""
    uniq = {}
    for p, o in pairs:
        h = hashlib.md5((p["system"] + "\x00" + p["user"]).encode()).hexdigest()[:12]
        item = uniq.setdefault(h, {"prompt": p, "k3_outputs": []})
        if o["output"] not in item["k3_outputs"]:
            item["k3_outputs"].append(o["output"])
    return uniq


# ─────────────────────────────── Gemini 调用 ───────────────────────────────
def call_gemini(model, api_key, system, user, timeout=180):
    """POST /v1beta generateContent，返回 (text, meta) 或抛异常。"""
    url = BASE_URL.format(model=model)
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": 2048},
    }
    t0 = time.time()
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    dt = time.time() - t0
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    meta = {
        "latency_s": round(dt, 1),
        "model_version": data.get("modelVersion", ""),
        "finish_reason": data.get("candidates", [{}])[0].get("finishReason", ""),
        "usage": data.get("usageMetadata", {}),
    }
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    return text, meta


# ─────────────────────────────── 对比 ───────────────────────────────
def parse_attrs(attrs_str):
    return {m.group(1): m.group(2)
            for m in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', attrs_str or "")}


def _text_parts(inner):
    return re.findall(r"<text>(.*?)</text>", inner or "", flags=re.S)


def _reply_text(b):
    parts = _text_parts(b.raw_inner)
    return " / ".join(p.strip() for p in parts) if parts else (b.inner or "")[:120]


def decide(blocks):
    """从合法块归纳动作决策。"""
    valid = [b for b in blocks if b.valid]
    if not valid:
        return "invalid"
    tags = [b.tag for b in valid]
    if set(tags) == {"silent"}:
        return "silent"
    if "task" in tags:
        return "task" + ("+reply" if "reply" in tags else "")
    if "tool" in tags:
        return "tool"
    if "reply" in tags:
        return "reply"
    return "mixed"


def analyze(output):
    """解析模型产出，返回结构化结果。"""
    blocks = extract_blocks(output or "")
    valid = [b for b in blocks if b.valid]
    replies = [b for b in valid if b.tag == "reply"]
    return {
        "raw": output,
        "n_blocks": len(blocks),
        "n_valid": len(valid),
        "bad": [b.error for b in blocks if not b.valid],
        "decision": decide(blocks),
        "reply_texts": [_reply_text(b) for b in replies],
        "replies": [{
            "session": parse_attrs(b.attrs).get("session", ""),
            "ref": parse_attrs(b.attrs).get("ref", ""),
            "text": _reply_text(b),
        } for b in replies],
    }


def compare(k3_out, g_out):
    """对比一份 k3 产出与一份 Gemini 产出。"""
    a, b = analyze(k3_out), analyze(g_out)
    return {
        "k3": a,
        "gemini": b,
        "same_decision": a["decision"] == b["decision"],
        "k3_valid": a["n_valid"] > 0 and not a["bad"],
        "gemini_valid": b["n_valid"] > 0 and not b["bad"],
        "reply_count_match": len(a["reply_texts"]) == len(b["reply_texts"]),
    }


# ─────────────────────────────── 主流程 ───────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-3.1-pro-preview")
    ap.add_argument("--samples", type=int, default=2,
                    help="每个唯一 prompt 采样次数（默认 2）")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--key", default=os.environ.get("AIXHAN_API_KEY", DEFAULT_KEY))
    args = ap.parse_args()

    pairs = pair_prompt_outputs(load_events(EVENTS_PATH))
    uniq = unique_prompts(pairs)
    items = list(uniq.items())
    print(f"事件流: {len(pairs)} 个 prompt→产出 配对 → 去重后 {len(items)} 个唯一 prompt")
    print(f"目标模型: {args.model} | 每 prompt 采样 {args.samples} 次 | 并发 {args.concurrency}")
    print()

    results = []
    jobs = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for h, item in items:
            p = item["prompt"]
            for s in range(args.samples):
                jobs.append(ex.submit(_one_job, args.model, args.key, h, p, item, s))
        for fut in as_completed(jobs):
            try:
                results.append(fut.result())
            except Exception as e:  # 单条失败不中断
                results.append({"error": str(e), "hash": "?"})

    results.sort(key=lambda r: r.get("hash", ""))
    _write_reports(results, items, args)
    _print_summary(results)


def _one_job(model, key, h, p, item, sample):
    try:
        g_text, g_meta = call_gemini(model, key, p["system"], p["user"])
    except Exception as e:
        return {"hash": h, "sample": sample, "error": str(e),
                "k3_outputs": item["k3_outputs"]}
    comps = [compare(k3o, g_text) for k3o in item["k3_outputs"]]
    return {
        "hash": h,
        "sample": sample,
        "session": p["session"],
        "round": p.get("round"),
        "user_len": len(p["user"]),
        "k3_outputs": item["k3_outputs"],
        "gemini_text": g_text,
        "meta": g_meta,
        "comparisons": comps,
        "match_any": any(c["same_decision"] for c in comps),
    }


def _write_reports(results, items, args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = OUT_DIR / f"report_{ts}"
    data = {
        "model": args.model,
        "samples": args.samples,
        "generated_at": datetime.now().isoformat(),
        "n_unique_prompts": len(items),
        "results": results,
    }
    with open(f"{base}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    _write_markdown(f"{base}.md", data, items)
    print(f"\n报告已写入: {base}.json / {base}.md")


def _write_markdown(path, data, items):
    lines = ["# Gemini 3.1 Pro Preview vs k3 决策层回放对比",
             "",
             f"- 生成时间: {data['generated_at']}",
             f"- 模型: `{data['model']}`（中转站 /v1beta）",
             f"- 唯一 prompt 数: {data['n_unique_prompts']}，每 prompt 采样 {data['samples']} 次",
             ""]
    ok = 0
    total = 0
    for r in data["results"]:
        if r.get("error"):
            lines.append(f"## ❌ hash={r['hash']} 调用失败: {r['error']}")
            continue
        ok += 1 if r["match_any"] else 0
        total += 1
        lines.append(f"## {r['hash']} session={r['session']} round={r['round']} "
                     f"sample={r['sample']} user={r['user_len']}B "
                     f"耗时={r['meta']['latency_s']}s")
        lines.append("")
        lines.append("### 存储的 k3 产出")
        for k3o in r["k3_outputs"]:
            a = analyze(k3o)
            lines.append(f"- `{k3o}`  → 决策={a['decision']} 合法块={a['n_valid']}")
        lines.append("")
        lines.append(f"### Gemini 产出（{r['meta'].get('model_version','')}）")
        g = analyze(r["gemini_text"])
        lines.append(f"```\n{r['gemini_text']}\n```")
        lines.append(f"→ 决策={g['decision']} 合法块={g['n_valid']} "
                     f"finish={r['meta'].get('finish_reason')}")
        if g["bad"]:
            lines.append(f"⚠️ 坏块: {g['bad']}")
        lines.append("")
        lines.append("### 与各 k3 产出的对比")
        for i, c in enumerate(r["comparisons"]):
            mark = "✅" if c["same_decision"] else "❌"
            lines.append(f"- {mark} vs k3#{i}: 决策 "
                         f"{c['k3']['decision']} vs {c['gemini']['decision']} | "
                         f"reply 数 {len(c['k3']['reply_texts'])} vs "
                         f"{len(c['gemini']['reply_texts'])} | "
                         f"k3合法={c['k3_valid']} gemini合法={c['gemini_valid']}")
        lines.append("")
    lines.insert(4, f"- 决策一致率: {ok}/{total}（有任一 k3 变体决策相同即算一致）")
    lines.insert(5, "")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _print_summary(results):
    print("═" * 72)
    print(f"{'hash':14s} {'session':8s} {'k3决策(变体)':22s} {'Gemini':14s} {'耗时':>5s}")
    print("─" * 72)
    for r in sorted(results, key=lambda x: x.get("hash", "")):
        if r.get("error"):
            print(f"{r.get('hash','?'):14s} ERROR: {r['error'][:60]}")
            continue
        k3_dec = sorted({c["k3"]["decision"] for c in r["comparisons"]})
        g_dec = r["comparisons"][0]["gemini"]["decision"] if r["comparisons"] else "?"
        mark = "✅" if r["match_any"] else "❌"
        print(f"{r['hash']:14s} {r['session']:8s} {mark} {','.join(k3_dec):22s} "
              f"{g_dec:14s} {r['meta']['latency_s']:>5.1f}s")
    print("═" * 72)


if __name__ == "__main__":
    main()
