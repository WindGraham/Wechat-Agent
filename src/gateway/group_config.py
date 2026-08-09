# -*- coding: utf-8 -*-
"""group_config.py — 群聊热情度配置 → 人格卡 reply_rules 映射。

用户在网关选级别，这里生成/更新 config/personas/<会话>.yaml 的
rules 段（人格其余部分仍走 default.yaml 底座）。PromptLibrary/
PersonaRenderer 按 mtime 热读，下一次决策即生效。
"""

import os
import re

import yaml

# 级别 → reply_rules（只覆盖回复策略，不动人格其他部分）
LEVELS = {
    "active": {
        "label": "活跃",
        "desc": "有消息就想聊，积极参与话题",
        "rules": [
            ("私聊", "必须回复"),
            ("群聊中被@", "必须回复"),
            ("主人（风图）说话", "必须回复，优先级最高"),
            ("群聊普通消息", "积极回复，主动参与话题"),
        ],
    },
    "normal": {
        "label": "正常",
        "desc": "话题有趣或相关才回（默认）",
        "rules": [
            ("私聊", "必须回复"),
            ("群聊中被@", "必须回复"),
            ("主人（风图）说话", "必须回复，优先级最高"),
            ("群聊中话题与自己无关", "沉默（输出 <silent/>）"),
            ("群聊中话题有趣或能帮上忙", "可以回复，简短自然"),
        ],
    },
    "mention": {
        "label": "仅@我",
        "desc": "只有被 @ 才回复，其余一律沉默",
        "rules": [
            ("私聊", "必须回复"),
            ("群聊中被@", "必须回复"),
            ("主人（风图）说话", "必须回复，优先级最高"),
            ("群聊中没被@的任何消息", "沉默（输出 <silent/>），一条都不回"),
        ],
    },
    "quiet": {
        "label": "安静",
        "desc": "基本不说话，只回 @我 和主人",
        "rules": [
            ("私聊", "主人说话才回，其他人私聊简短回"),
            ("群聊中被@", "必须回复"),
            ("主人（风图）说话", "必须回复，优先级最高"),
            ("群聊普通消息", "沉默（输出 <silent/>）"),
        ],
    },
    "off": {
        "label": "静默",
        "desc": "全部沉默，什么都不回",
        "rules": [
            ("任何消息", "沉默（输出 <silent/>），包括被@"),
        ],
    },
}

DEFAULT_LEVEL = "normal"


def _safe_name(session: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", session)


def card_path(personas_dir: str, session: str) -> str:
    return os.path.join(personas_dir, _safe_name(session) + ".yaml")


def read_level(personas_dir: str, session: str) -> tuple:
    """读当前级别。返回 (level, extra_rule)。无卡 → (None, '')。"""
    path = card_path(personas_dir, session)
    if not os.path.exists(path):
        return None, ""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return None, ""
    level = data.get("x_level")
    extra = ""
    for sr in (data.get("rules", {}).get("special") or []):
        if sr.get("rule", "").startswith("【群规补充】"):
            extra = sr["rule"].replace("【群规补充】", "", 1)
    return (level if level in LEVELS else None), extra


def write_level(personas_dir: str, session: str, level: str,
                extra_rule: str = "") -> str:
    """把级别写进会话人格卡（保留卡片其他段，只更新 rules）。返回路径。"""
    if level not in LEVELS:
        raise ValueError(f"未知级别: {level}")
    path = card_path(personas_dir, session)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            data = {}

    rules = data.setdefault("rules", {})
    rules["reply_rules"] = [
        {"condition": c, "action": a} for c, a in LEVELS[level]["rules"]]
    special = [s for s in (rules.get("special") or [])
               if not s.get("rule", "").startswith("【群规补充】")]
    if extra_rule.strip():
        special.append({"rule": f"【群规补充】{extra_rule.strip()}"})
    rules["special"] = special
    data["x_level"] = level
    data["rules"] = rules

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return path
