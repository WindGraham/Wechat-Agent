# -*- coding: utf-8 -*-
"""decision/proxy/events.py — 事件类型常量与事件工具。

事件是 proxy 内部各处理器之间的通信载体。事件类型定义在这里，
新增事件类型只需在此登记 + 在 handlers/ 加一个处理器文件。
"""

# 事件类型
EV_LOG_UPDATED = "log_updated"
EV_TASK_DONE = "task_done"
EV_MEMORY_WARM = "memory_warm"
EV_SEARCH_DONE = "search_done"

# 优先级：0=主人 1=@我 2=任务回执 3=普通
_PRIORITY = {
    "owner": 0,
    "mention": 1,
    EV_TASK_DONE: 2,
}


def priority_of(ev: dict) -> int:
    """事件优先级（用于事件队列排序）。"""
    if ev.get("owner"):
        return _PRIORITY["owner"]
    if ev.get("mention"):
        return _PRIORITY["mention"]
    return _PRIORITY.get(ev.get("type"), 3)


def same_event(a: dict, b: dict) -> bool:
    """同会话同类事件判定（合并用：保留最新）。"""
    return a.get("type") == b.get("type") \
        and a.get("session") == b.get("session")


def sort_key(ev: dict):
    """事件队列排序键：优先级 + 时间戳。"""
    return (priority_of(ev), ev.get("ts", 0.0))
