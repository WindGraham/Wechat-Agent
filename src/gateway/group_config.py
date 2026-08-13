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


def _top_key(line: str):
    """行首是否为顶层 mapping key（indent 0、非注释/空白/列表项）。

    返回 key 名或 None。用于定位 rules / x_level 块边界，做到"只改受管
    字段、其它行（含注释/空行/别的段）逐字保留"。
    """
    if not line or line[0] in (" ", "\t"):
        return None
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith("-") or s.startswith("?"):
        return None
    if ":" in s:
        key = s.split(":", 1)[0].strip()
        if key:
            return key
    return None


def _dump_indented(obj, indent: int = 2):
    """safe_dump 一段结构并整体缩进 indent 空格（用于嵌进 rules: 块）。"""
    dumped = yaml.safe_dump(obj, allow_unicode=True, sort_keys=False)
    prefix = " " * indent
    return [prefix + ln if ln.strip() else ln for ln in dumped.splitlines()]


def _set_x_level(lines, level: str):
    """更新（或追加）顶层 x_level 行，返回新行列表。"""
    for i, ln in enumerate(lines):
        if _top_key(ln) == "x_level":
            lines[i] = f"x_level: {level}"
            return lines
    lines.append(f"x_level: {level}")
    return lines


def write_level(personas_dir: str, session: str, level: str,
                extra_rule: str = "") -> str:
    """把级别写进会话人格卡。返回路径。

    只重写受管的 rules 块（reply_rules / special）与顶层 x_level 行，
    卡片其余部分（顶部注释、identity/personality/speaking 等段及其注释）
    逐字保留——修复此前用 yaml.safe_dump 整卡重写、抹掉手写注释的问题。

    限制：rules 块内除 reply_rules/special 外的其它键（如 never）经
    safe_dump 重排，若这些键内部有手写注释会丢失（极罕见，群卡基本无）。
    """
    if level not in LEVELS:
        raise ValueError(f"未知级别: {level}")
    path = card_path(personas_dir, session)

    raw = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    # 1) 组装新的 rules 块（保留 rules 下其它键）
    rules_src = data.get("rules", {})
    rules_src = rules_src if isinstance(rules_src, dict) else {}
    other = {k: v for k, v in rules_src.items()
             if k not in ("reply_rules", "special")}
    reply_rules = [{"condition": c, "action": a}
                   for c, a in LEVELS[level]["rules"]]
    special = [s for s in (rules_src.get("special") or [])
               if not str(s.get("rule", "") if isinstance(s, dict) else "")
               .startswith("【群规补充】")]
    if extra_rule.strip():
        special.append({"rule": f"【群规补充】{extra_rule.strip()}"})
    new_rules = {"reply_rules": reply_rules, "special": special}
    new_rules.update(other)
    rules_block = ["rules:"] + _dump_indented(new_rules, 2)

    # 2) 在原文里替换 rules: 顶层块（块外内容逐字保留）
    lines = raw.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if _top_key(ln) == "rules":
            start = i
            break
    if start is None:
        out = lines + rules_block
    else:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if _top_key(lines[j]) is not None:
                end = j
                break
        out = lines[:start] + rules_block + lines[end:]

    # 3) 更新/追加 x_level
    out = _set_x_level(out, level)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return path
