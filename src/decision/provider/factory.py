# -*- coding: utf-8 -*-
"""provider/factory.py — 按配置创建 provider。

密钥读取优先级：环境变量 > workspace/.env
（KIMI_API_KEY / DEEPSEEK_API_KEY / AIXHAN_API_KEY 或 GEMINI_API_KEY）。
"""

import logging
import os

from .base import DeepSeekProvider, KimiProvider, LLMProvider, ProviderError
from .gemini import GeminiProvider, DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
# provider → 默认模型（网关决策模型面板展示用）收敛到 shared 单一事实来源
from src.shared.model_catalog import DEFAULT_MODELS

log = logging.getLogger("decision.provider.factory")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
ENV_PATH = os.path.join(PROJECT_ROOT, "workspace", ".env")


def _read_env_file(path=ENV_PATH) -> dict:
    """读 workspace/.env（KEY=VALUE 行，# 注释）。文件不存在返回空。"""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _get_key(name: str, env_file: dict) -> str:
    return os.environ.get(name) or env_file.get(name, "")


def create_provider(prefer: str = "kimi", model: str = None,
                    env_path=ENV_PATH) -> LLMProvider:
    """创建 provider。prefer ∈ {kimi, deepseek, gemini}。

    - kimi：有 KIMI_API_KEY 走 k3，否则回退 deepseek
    - deepseek：有 DEEPSEEK_API_KEY 走 deepseek 模型，否则回退 kimi
    - gemini：有 AIXHAN_API_KEY（或 GEMINI_API_KEY）走中转站 gemini，
      否则回退 kimi
    """
    env_file = _read_env_file(env_path)
    kimi_key = _get_key("KIMI_API_KEY", env_file)
    ds_key = _get_key("DEEPSEEK_API_KEY", env_file)
    gemini_key = (_get_key("AIXHAN_API_KEY", env_file)
                  or _get_key("GEMINI_API_KEY", env_file))

    if prefer == "gemini":
        if gemini_key:
            p = GeminiProvider(gemini_key, model=model or GEMINI_DEFAULT_MODEL)
            log.info("LLM provider: gemini (model=%s)", p.model)
            return p
        log.warning("无 AIXHAN_API_KEY/GEMINI_API_KEY，回退 kimi")
        prefer = "kimi"

    order = ["kimi", "deepseek"] if prefer == "kimi" else ["deepseek", "kimi"]
    for name in order:
        if name == "kimi" and kimi_key:
            p = KimiProvider(kimi_key, model=model or "k3")
            log.info("LLM provider: kimi (model=%s)", p.model)
            return p
        if name == "deepseek" and ds_key:
            p = DeepSeekProvider(ds_key, model=model or "deepseek-chat")
            log.info("LLM provider: deepseek (model=%s)", p.model)
            return p
    raise ProviderError("未找到任何 LLM API key（env 或 workspace/.env）")
