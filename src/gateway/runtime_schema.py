# -*- coding: utf-8 -*-
"""gateway/runtime_schema.py — runtime.json 的单一事实来源。

网关对 config/runtime.json 的字段表、校验、读写过去散在三处
（app.py 的 _RUNTIME_FIELDS、config.py 的 _validate_runtime、
agent.py 的 _read_runtime/_write_runtime/_MODEL_KEYS），字段一旦
新增/改名就会三处漂移（历史教训：open_all_sessions 等字段不在
app.py 白名单里，/api/runtime PUT 会误报 unknown field）。

这里收敛为唯一的一份 schema + 读/写/校验，其它模块只 import。

注意：本文件属 src/gateway，改它会被 hot_reload 的 mtime 检测到，
因此 _reload_modules() 必须显式 reload 本模块（见 hot_reload.py）。
"""

import json
import os

# 字段表：name -> 期望类型（isinstance 校验）。可空字段用 type 元组表达。
RUNTIME_FIELDS = {
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
    "open_all_sessions": bool,
    "task_timeout_s": int,
    "tool_model_mm": str,
    "tool_model_text": str,
    "friend_auto_accept": bool,
    "friend_check_interval": int,
    "friend_max_accept": int,
    "decision_provider": str,
    "decision_model": str,
    "decision_token_floor": int,
    "decision_token_ceiling": int,
    "extract_provider": str,
    "extract_model": (str, type(None)),
    "special_prompts": dict,
}

# [min, max] 数值区间字段（二元数组，元素为数值且非 bool）
RUNTIME_INTERVAL_FIELDS = (
    "sweep_interval",
    "notify_interval",
    "screen_watch_interval",
)

# 决策模型切换涉及的字段（gateway/api/agent.py 用）
MODEL_FIELDS = (
    "decision_provider",
    "decision_model",
    "decision_token_floor",
    "decision_token_ceiling",
)


def validate_runtime(body: dict):
    """按字段表校验待写入的 runtime 字段子集；返回错误信息或 None。

    与历史行为一致：未知字段、类型不符、区间非二元数值数组都报错。
    """
    for key, value in body.items():
        if key in RUNTIME_INTERVAL_FIELDS:
            if (not isinstance(value, (list, tuple)) or len(value) != 2
                    or not all(isinstance(v, (int, float))
                               and not isinstance(v, bool)
                               for v in value)):
                return f"{key} must be [min, max] numbers"
            continue
        expected = RUNTIME_FIELDS.get(key)
        if expected is None:
            return f"unknown field: {key}"
        if isinstance(value, bool) and expected is not bool:
            return f"{key} must be {expected}"
        if not isinstance(value, expected):
            return f"{key} must be {expected}"
    return None


def read_runtime(root: str) -> dict:
    """读 config/runtime.json；不存在/解析失败返回 {}。"""
    path = os.path.join(root, "config", "runtime.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_runtime(root: str, data: dict):
    """写回 config/runtime.json（创建父目录，UTF-8 缩进 2，末尾换行）。"""
    path = os.path.join(root, "config", "runtime.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
