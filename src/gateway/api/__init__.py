# -*- coding: utf-8 -*-
"""gateway/api — 网关 API 蓝图（按域拆分，高内聚）。

每个模块一个 Blueprint，通过 create_bp(ctx) 工厂创建。
ctx: 依赖上下文 {root, proxy, supervisor, agent_callback_url, reloader}。

新增 API 域 = 新建 api/<域>.py + 在 create_bps() 注册。
"""

from flask import Blueprint

from . import agent, config, emojis, live, memory, messages, models, replay, sessioncfg, tts  # noqa: F401


def create_bps(ctx: dict) -> list:
    """创建全部 API 蓝图。ctx 含依赖注入。"""
    return [
        agent.create_bp(ctx),
        memory.create_bp(ctx),
        config.create_bp(ctx),
        live.create_bp(ctx),
        models.create_bp(ctx),
        emojis.create_bp(ctx),
        replay.create_bp(ctx),
        sessioncfg.create_bp(ctx),
        messages.create_bp(ctx),
        tts.create_bp(ctx),
    ]
