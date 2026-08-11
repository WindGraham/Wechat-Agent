# -*- coding: utf-8 -*-
"""decision/proxy/special_scheduler.py — Special Prompt 调度器（投硬币器）。

按方案 B（指数间隔/泊松过程）调度特殊 prompt：
  - 用 rate_per_day 算每秒强度 λ = rate_per_day / 86400
  - 每次触发后抽样下一次触发时间：next_at = now + random.expovariate(λ)
  - 每分钟 tick 一次检查 now >= next_at
  - 状态持久化到 workspace/runtime/special_scheduler.json

与 Proxy 的关系：
  - 初始化：Proxy.__init__() 中创建
  - tick：Proxy.run_forever() 每轮 poll 时调用 scheduler.tick()
  - 触发：scheduler 直接 push EV_SPECIAL_RUN 事件
"""

import json
import logging
import os
import random
import time

log = logging.getLogger("decision.special_scheduler")

# 默认路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_STATE_PATH = os.path.join(
    PROJECT_ROOT, "workspace", "runtime", "special_scheduler.json")

# 默认配置：不配置则无特殊 prompt
DEFAULT_PROMPTS = {}


class SpecialScheduler:
    """特殊 prompt 调度器。

    使用方式：
        scheduler = SpecialScheduler(push_fn=proxy._push_event, configs={...})
        # 在 run_forever 循环中：
        scheduler.tick(now)
    """

    def __init__(self, push_fn, configs: dict = None,
                 state_path: str = DEFAULT_STATE_PATH,
                 clock=time.time, rng=None):
        self._push = push_fn          # fn(ev_dict) → 入队事件
        self._clock = clock
        self._rng = rng or random.Random()
        self._state_path = state_path
        self._configs = dict(configs or {})
        # 状态：{prompt_name: {"last_trigger": ts, "next_at": ts}}
        self._state = self._load_state()

    # ---------------------------------------------------------------- 配置
    def update_configs(self, configs: dict):
        """热更新调度配置（从 runtime.json 重新读取后调用）。"""
        self._configs = dict(configs or {})
        # 新出现或已存在的保留 next_at；已删除的清理状态记录
        for name in list(self._state.keys()):
            if name not in self._configs:
                del self._state[name]
        log.info("special scheduler configs updated: %s",
                 list(self._configs.keys()))

    # ---------------------------------------------------------------- tick
    def tick(self, now: float = None):
        """每分钟调用一次：检查是否有特殊 prompt 到期触发。"""
        now = now or self._clock()
        for name, cfg in self._configs.items():
            if not cfg.get("enabled", True):
                continue
            lam = cfg.get("rate_per_day", 0.5) / 86400.0
            if lam <= 0:
                continue
            state = self._state.setdefault(name, {})
            next_at = state.get("next_at", 0)
            if now < next_at:
                continue
            # 触发
            self._trigger(name, now, cfg)
            # 抽样下一次
            state["last_trigger"] = now
            state["next_at"] = now + self._rng.expovariate(lam)
            self._save_state()

    def _trigger(self, name: str, now: float, cfg: dict):
        """触发一个特殊 prompt。"""
        session = cfg.get("session", f"__special_{name}__")
        log.info("special scheduler trigger: %s (session=%s)", name, session)
        self._push({
            "type": "special_run",
            "prompt_name": name,
            "session": session,
            "ts": now,
        })

    # ---------------------------------------------------------------- 持久化
    def _load_state(self) -> dict:
        try:
            if os.path.exists(self._state_path):
                with open(self._state_path, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            log.debug("special scheduler state 读取失败，用空状态")
        return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_path)
        except OSError:
            log.debug("special scheduler state 写入失败", exc_info=True)
