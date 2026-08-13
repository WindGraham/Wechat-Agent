# -*- coding: utf-8 -*-
"""shared/model_catalog.py — LLM API/模型目录（全项目单一事实来源）。

所有「选模型 / 校验 provider / 取默认模型」的地方都从这里取清单：
  - 网关决策模型面板（/api/decision_model）
  - 每会话模型配置（/api/session_config）
  - 决策层 provider 工厂的默认模型表（factory.DEFAULT_MODELS）

避免前端 / 后端 / 决策层各自硬编码一份、清单互相漂移（历史教训：前端
DM_MODELS 与后端 AVAILABLE_MODELS 不一致，deepseek 出现 deepseek-chat
与 deepseek-v4-pro 两套默认）。

数据源只有一个：MODEL_CATALOG。新增 / 下架模型只改这里。

注意：本文件属 src/shared（跨层共享），不随网关热重载监听（hot_reload
只盯 src/gateway/**）；改动后需重启网关 / agent 进程生效。
"""

# provider → 可选模型清单（顺序 = 下拉展示顺序）
MODEL_CATALOG = {
    "kimi": ["k3", "k3-256k", "kimi-for-coding",
             "kimi-for-coding-highspeed"],
    "deepseek": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat"],
    "gemini": ["gemini-3.1-pro-preview", "gemini-3.6-flash",
               "gemini-3.5-flash", "gemini-3-flash-preview",
               "gemini-2.5-flash", "gemini-2.5-flash-lite"],
}

# 默认 provider（runtime.json 未配置 decision_provider 时）
DEFAULT_PROVIDER = "kimi"

# provider → 默认模型（未显式指定 model 时的兜底）
DEFAULT_MODELS = {
    "kimi": "k3",
    "deepseek": "deepseek-v4-pro",
    "gemini": "gemini-3.1-pro-preview",
}

VALID_PROVIDERS = tuple(MODEL_CATALOG.keys())


def is_valid_provider(provider):
    """provider 是否在目录里。"""
    return provider in MODEL_CATALOG


def is_valid_model(provider, model):
    """(provider, model) 组合是否在目录里。"""
    return model in MODEL_CATALOG.get(provider, ())


def default_model_for(provider):
    """provider 的默认模型（目录未收录 provider 时返回 None）。"""
    return DEFAULT_MODELS.get(provider) or (
        MODEL_CATALOG.get(provider) or [None])[0]
