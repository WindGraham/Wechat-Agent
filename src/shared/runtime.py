# -*- coding: utf-8 -*-
"""runtime.py — runtime.json 运行时配置加载器（CONTRACTS.md §五）。

- get(key, default)：字段读取，缺字段回退 DEFAULTS，再回退调用方 default
- check()：按 mtime 热重读（mtime 变了就重新加载）， reload 后触发 on_change 回调
- 属性访问（cfg.sweep_interval）与 get() 等价，供 run_loop / scanner 直接读属性
- .config 返回自身，兼容 scanner 的 runtime.config.monitored 读法

JSON 解析失败时保留上一份配置（不崩溃、不丢配置）。
"""

import copy
import json
import logging
import os

log = logging.getLogger("shared.runtime")

# CONTRACTS.md §五 字段表的默认值兜底
DEFAULTS = {
    "max_concurrent_decisions": 1,
    "media_convert_concurrency": 2,
    "history_size": 200,
    "sweep_interval": [45, 90],
    "notify_interval": [3, 6],
    "paused": False,
    "muted_until": 0.0,
    "owner": "",
    "owner_nick": "",
    "action_max_attempts": 2,
    "task_retention_days": 14,
    "tool_model": "kimi-code/k3",
    # scanner / notify 侧使用（不在 §五 表内，缺省安全值）
    "monitored": [],
    "open_all_sessions": False,
}


class RuntimeConfig:
    """runtime.json 加载器：读取 + mtime 热重读 + 默认值兜底。"""

    def __init__(self, path):
        self._path = os.fspath(path)
        self._mtime = None
        self._values = copy.deepcopy(DEFAULTS)
        self._callbacks = []
        self.reload()

    # ------------------------------------------------------------------ 读取
    def get(self, key, default=None):
        """读字段：文件值 > DEFAULTS > 调用方 default。"""
        return self._values.get(key, default)

    def __getattr__(self, name):
        # 只兜底数据字段（_ 开头走默认属性查找，避免无限递归）
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name)

    @property
    def config(self):
        """兼容 scanner 的 runtime.config.xxx 读法。"""
        return self

    def snapshot(self):
        """当前生效配置的拷贝（含默认值兜底后的全集）。"""
        return copy.deepcopy(self._values)

    # ------------------------------------------------------------------ 热重读
    def on_change(self, callback):
        """注册 reload 回调：callback(self)。立即调用一次（对齐初值）。"""
        self._callbacks.append(callback)
        try:
            callback(self)
        except Exception:
            log.exception("runtime on_change callback failed")

    def check(self):
        """mtime 变了就重新加载。返回是否发生了 reload。"""
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            return False  # 文件消失：保留当前配置
        if self._mtime is not None and mtime == self._mtime:
            return False
        return self.reload()

    def reload(self):
        """重新加载文件。JSON 解析失败保留旧值。返回是否成功加载。"""
        try:
            mtime = os.path.getmtime(self._path)
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            log.warning("runtime.json 读取/解析失败，保留当前配置: %s",
                        self._path)
            return False
        if not isinstance(data, dict):
            log.warning("runtime.json 顶层不是对象，保留当前配置")
            return False
        values = copy.deepcopy(DEFAULTS)
        values.update(data)
        self._values = values
        self._mtime = mtime
        log.info("runtime config loaded: %s (%d keys)", self._path, len(data))
        for cb in self._callbacks:
            try:
                cb(self)
            except Exception:
                log.exception("runtime on_change callback failed")
        return True
