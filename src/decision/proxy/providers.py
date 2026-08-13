# -*- coding: utf-8 -*-
"""proxy/providers.py — ProviderRegistry：决策 provider 与会话配置管理。

从 Proxy 抽出的"provider + 会话配置"职责：
  - 主决策 provider 的热切换（网关模型切换）
  - 记忆提取专用 provider（独立于主 provider，通常用便宜模型）
  - 每会话专用 provider/model（workspace/runtime/session_config.json 热读）
  - 缓存命中统计（网关展示用）
"""

import json
import logging
import os
import threading

log = logging.getLogger("decision.proxy.providers")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SESSION_CONFIG_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                                   "session_config.json")
CACHE_STATS_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                                "cache_stats.json")


class ProviderRegistry:
    """决策 provider + 会话配置的统一管理。"""

    def __init__(self, provider, runtime_get, clock=None):
        self._provider = provider
        self._extract_provider = None
        self._rt = runtime_get          # callable(key, default) -> value
        self._clock = clock
        # 每会话配置热读缓存
        self._session_cfg_cache = None  # (mtime, data)
        self._session_cfg_lock = threading.Lock()
        # (provider, model) -> LLMProvider 懒创建缓存
        self._session_providers = {}
        self._session_prov_lock = threading.Lock()
        self._cache_stats_lock = threading.Lock()

    # ---------------------------------------------------------------- 主 provider
    @property
    def main(self):
        return self._provider

    def set_provider(self, provider):
        """热替换决策 LLM provider（网关模型切换）。

        只换决策对话用的 provider；媒体转换器（MediaConverter）仍持有
        启动时的 provider 引用——切换成无图像能力的模型时图片识别不受影响。
        """
        old = getattr(self._provider, "model", "?")
        self._provider = provider
        log.info("决策 provider 热切换: %s -> %s",
                 old, getattr(provider, "model", "?"))

    def provider_info(self) -> dict:
        """当前决策 provider 实况（网关展示用）。"""
        p = self._provider
        return {"model": getattr(p, "model", "?"),
                "url": getattr(p, "_url", ""),
                "token_floor": getattr(p, "_token_floor", 0),
                "token_ceiling": getattr(p, "_token_ceiling", 0)}

    # ---------------------------------------------------------------- 提取 provider
    def set_extract_provider(self, provider):
        """设置记忆提取专用 provider（通常用便宜模型）。不设置回退主 provider。"""
        self._extract_provider = provider

    def extract_provider(self):
        """取提取用 provider：专用 > 主决策 provider。"""
        return self._extract_provider or self._provider

    # ---------------------------------------------------------------- 每会话配置
    def load_session_config(self) -> dict:
        """热读 workspace/runtime/session_config.json（mtime 缓存）。"""
        with self._session_cfg_lock:
            try:
                mtime = os.path.getmtime(SESSION_CONFIG_PATH)
            except OSError:
                return {}
            if self._session_cfg_cache \
                    and self._session_cfg_cache[0] == mtime:
                return self._session_cfg_cache[1]
            try:
                with open(SESSION_CONFIG_PATH, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            data = data if isinstance(data, dict) else {}
            self._session_cfg_cache = (mtime, data)
            return data

    def session_config(self, session: str) -> dict:
        cfg = self.load_session_config().get(session)
        return cfg if isinstance(cfg, dict) else {}

    def session_options(self, session: str) -> dict:
        """该会话的 prompt 携带选项：{include_memory, include_history, goal}。"""
        cfg = self.session_config(session)
        return {
            "include_memory": bool(cfg.get("include_memory", True)),
            "include_history": bool(cfg.get("include_history", True)),
            "goal": (cfg.get("goal") or "").strip(),
        }

    def provider_for(self, session: str):
        """该会话的决策 provider：会话配置了 provider/model 用专用实例
        （懒创建缓存），否则回退全局。"""
        cfg = self.session_config(session)
        prov = (cfg.get("provider") or "").strip()
        model = (cfg.get("model") or "").strip()
        if not prov or not model:
            return self._provider
        key = (prov, model)
        with self._session_prov_lock:
            p = self._session_providers.get(key)
        if p is not None:
            return p
        try:
            from ..provider.factory import create_provider
            p = create_provider(prefer=prov, model=model)
            p.set_token_limits(self._rt("decision_token_floor", 0),
                               self._rt("decision_token_ceiling", 0))
        except Exception as e:  # noqa: BLE001
            log.warning("会话 %s 的 provider(%s/%s) 创建失败，回退全局: %s",
                        session, prov, model, e)
            return self._provider
        with self._session_prov_lock:
            self._session_providers[key] = p
        log.info("会话 %s 使用专用决策 provider: %s/%s", session, prov, model)
        return p

    # ---------------------------------------------------------------- 缓存统计
    def note_cache(self, provider) -> None:
        """把一次 LLM 调用的缓存命中计入滚动统计（网关展示用）。"""
        try:
            st = provider.cache_stats()
        except Exception:  # noqa: BLE001
            return
        if not st.get("prompt_tokens") and not st.get("cached_tokens"):
            return
        with self._cache_stats_lock:
            try:
                with open(CACHE_STATS_PATH, encoding="utf-8") as f:
                    agg = json.load(f)
            except (OSError, ValueError):
                agg = {"count": 0, "prompt_tokens": 0, "cached_tokens": 0}
            agg["count"] = agg.get("count", 0) + 1
            agg["prompt_tokens"] = (agg.get("prompt_tokens", 0)
                                    + st["prompt_tokens"])
            agg["cached_tokens"] = (agg.get("cached_tokens", 0)
                                    + st["cached_tokens"])
            try:
                with open(CACHE_STATS_PATH, "w", encoding="utf-8") as f:
                    json.dump(agg, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
