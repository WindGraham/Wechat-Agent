# -*- coding: utf-8 -*-
"""prompt/persona.py — 人格卡加载与渲染（config/personas/*.yaml）。

合并规则：default.yaml（底座）→ <会话名>.yaml（覆盖）。dict 递归合并，
list/scalar 覆盖，None 不覆盖。按 mtime 缓存。
"""

import logging
import os

import yaml

log = logging.getLogger("decision.prompt.persona")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
PERSONAS_DIR = os.path.join(PROJECT_ROOT, "config", "personas")


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if v is None:
            continue
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class PersonaRenderer:
    """人格卡 → system prompt 人设文本。"""

    def __init__(self, personas_dir=PERSONAS_DIR):
        self._dir = personas_dir
        self._cache = {}          # path -> (mtime, data)

    def _load_file(self, name: str):
        path = os.path.join(self._dir, f"{name}.yaml")
        try:
            mtime = os.path.getmtime(path)
            cached = self._cache.get(path)
            if cached and cached[0] == mtime:
                return cached[1]
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                self._cache[path] = (mtime, data)
                return data
        except (OSError, yaml.YAMLError) as e:
            log.debug("persona 卡读取失败 %s: %s", path, e)
        return None

    def load(self, session: str) -> dict:
        """加载会话人格卡：default 底座 + 会话卡覆盖（文件名模糊匹配）。"""
        persona = self._load_file("default") or {}
        for name in (session, session.replace(" ", ""), session.split("(")[0]):
            card = self._load_file(name)
            if card:
                persona = _deep_merge(persona, card)
                break
        return persona

    def render(self, session: str) -> str:
        """人格卡 → 人设文本块（填入 persona.md 的 {persona} 槽位）。"""
        p = self.load(session)
        if not p:
            return ""
        parts = []

        identity = p.get("identity", {})
        name = identity.get("name", "")
        role = identity.get("role", "")
        desc = identity.get("description", "")
        if name:
            head = f"你是{name}"
            if role:
                head += f"，{role}"
            parts.append(head + "。" + desc)

        personality = p.get("personality", {})
        if personality.get("traits"):
            parts.append(f"性格：{'、'.join(personality['traits'])}。")
        if personality.get("tone"):
            parts.append(f"语气：{personality['tone']}。")
        if personality.get("dont_be"):
            parts.append(f"不要：{'、'.join(personality['dont_be'])}。")

        speaking = p.get("speaking", {})
        if speaking.get("style"):
            parts.append(f"说话风格：{speaking['style']}。")
        if speaking.get("max_length"):
            parts.append(f"每次回复最多{speaking['max_length']}。")
        if speaking.get("forbidden_starts"):
            parts.append("禁止以"
                         + "、".join(speaking["forbidden_starts"]) + "开头。")
        if speaking.get("forbidden_patterns"):
            parts.append(f"禁止：{'、'.join(speaking['forbidden_patterns'])}。")
        if speaking.get("allowed"):
            parts.append(f"允许：{'；'.join(speaking['allowed'])}。")

        rules = p.get("rules", {})
        if rules.get("reply_rules"):
            lines = ["回复策略："]
            for r in rules["reply_rules"]:
                lines.append(f"  - {r.get('condition','')}时：{r.get('action','')}")
            parts.append("\n".join(lines))
        if rules.get("never"):
            parts.append(f"绝对不要：{'；'.join(rules['never'])}。")
        for sr in rules.get("special", []):
            if sr.get("rule"):
                parts.append(sr["rule"])

        memory = p.get("memory", {})
        if memory.get("core"):
            parts.append(f"核心记忆：{'；'.join(memory['core'])}。")

        return "\n\n".join(parts)
