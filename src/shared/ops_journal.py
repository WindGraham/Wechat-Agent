# -*- coding: utf-8 -*-
"""ops_journal.py — 交互层原子操作流水（网关实况页可见）。

每个触屏/输入动作写一行 JSONL 到 workspace/runtime/interaction_ops.jsonl。
文件超 2MB 截断保留后半。只增不改，失败静默（绝不影响动作本身）。
"""

import json
import logging
import os
import time

log = logging.getLogger("shared.ops_journal")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
OPS_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                        "interaction_ops.jsonl")
_MAX_BYTES = 2 * 1024 * 1024


def log_op(op: str, **params):
    """记录一个原子操作。op: tap/swipe/long_press/input_text/back/..."""
    try:
        line = {"ts": time.time(), "op": op}
        for k, v in params.items():
            s = str(v)
            line[k] = s[:60] + "…" if len(s) > 60 else s
        os.makedirs(os.path.dirname(OPS_PATH), exist_ok=True)
        with open(OPS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        # 有界：超 2MB 截断保留后半（按行对齐）
        if os.path.getsize(OPS_PATH) > _MAX_BYTES:
            with open(OPS_PATH, "rb") as f:
                data = f.read()[-_MAX_BYTES // 2:]
            nl = data.find(b"\n")
            with open(OPS_PATH, "wb") as f:
                f.write(data[nl + 1:] if nl > 0 else data)
    except Exception:  # noqa: BLE001
        pass
