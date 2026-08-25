# -*- coding: utf-8 -*-
"""prompt_builder.py — 将采集到的消息拼接为 LLM prompt。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class MessageEntry:
    """单条消息的规范化表示。"""
    sender: str = ""
    content: str = ""
    timestamp: str = ""
    content_type: str = "text"
    matched: bool = False
    avatar_score: float = 0.0
    nick_score: float = 0.0
    avatar_cand: str = ""
    is_mine: bool = False


def parse_segments(segs: list, time_hint: str = "") -> List[MessageEntry]:
    """将 segment_cutlines 输出的段列表解析为 MessageEntry 列表。"""
    entries = []
    cur_time = time_hint

    for seg in segs:
        factor = seg.get("factor", "")
        content = seg.get("content", "").strip()

        if factor == "time":
            cur_time = content
            entries.append(MessageEntry(
                content=content,
                timestamp=cur_time,
                content_type="time_divider",
            ))
            continue

        sender = seg.get("nickname", "") or seg.get("avatar_cand", "未知")
        avatar_score = seg.get("avatar_score", 0.0) or 0.0
        nick_score = seg.get("nick_score", 0.0) or 0.0
        matched = factor == "dual"

        content_type = "text"
        if seg.get("image") or "[图片]" in content:
            content_type = "image"
        elif seg.get("quote") or "引用" in content:
            content_type = "quote"

        entries.append(MessageEntry(
            sender=sender,
            content=content,
            timestamp=cur_time,
            content_type=content_type,
            matched=matched,
            avatar_score=avatar_score,
            nick_score=nick_score,
            avatar_cand=seg.get("avatar_cand", ""),
        ))

    return entries


def build_prompt_text(
    entries: List[MessageEntry],
    group_name: str = "",
    include_meta: bool = False,
    max_length: Optional[int] = None,
) -> str:
    """拼接为纯文本 prompt。"""
    lines = []
    if group_name:
        lines.append(f"=== 群聊：{group_name} ===")
        lines.append("")

    for e in entries:
        if e.content_type == "time_divider":
            lines.append(f"[{e.content}]")
            continue

        sender = e.sender or "未知"
        line = f"{sender}: {e.content}"

        if include_meta and (e.avatar_score > 0 or e.nick_score > 0):
            meta = []
            if e.avatar_score > 0:
                meta.append(f"头像{e.avatar_score:.0%}")
            if e.nick_score > 0:
                meta.append(f"昵称{e.nick_score:.0%}")
            if e.avatar_cand and e.avatar_cand != sender:
                meta.append(f"→{e.avatar_cand}")
            line += f"  ({' · '.join(meta)})"

        lines.append(line)

    text = "\n".join(lines)

    if max_length and len(text) > max_length:
        lines_rev = list(reversed(lines))
        accum = 0
        keep = []
        for line in lines_rev:
            if accum + len(line) + 1 > max_length:
                break
            keep.append(line)
            accum += len(line) + 1
        text = "\n".join(reversed(keep))
        text = "...（更早消息已截断）\n" + text

    return text


def build_prompt_json(entries: List[MessageEntry], group_name: str = "") -> str:
    """拼接为 JSON 格式。"""
    data = {
        "group": group_name,
        "message_count": len([e for e in entries if e.content_type != "time_divider"]),
        "messages": [
            {
                "sender": e.sender,
                "content": e.content,
                "timestamp": e.timestamp,
                "type": e.content_type,
                "matched": e.matched,
                "confidence": {
                    "avatar": round(e.avatar_score, 3),
                    "nickname": round(e.nick_score, 3),
                } if e.avatar_score > 0 or e.nick_score > 0 else None,
            }
            for e in entries
            if e.content_type != "time_divider"
        ],
        "time_dividers": [e.content for e in entries if e.content_type == "time_divider"],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def build_prompt_compact(entries: List[MessageEntry], group_name: str = "") -> str:
    """紧凑格式，适合 token 敏感场景。"""
    lines = []
    if group_name:
        lines.append(f"群:{group_name}")

    cur_time = ""
    for e in entries:
        if e.content_type == "time_divider":
            cur_time = e.content
            continue
        sender = e.sender or "?"
        time_prefix = f"[{cur_time}] " if cur_time else ""
        lines.append(f"{time_prefix}{sender}:{e.content}")

    return "\n".join(lines)


def build_prompt_from_manifest(
    manifest_path: str,
    format: str = "text",
    group_name: str = "",
    **kwargs,
) -> str:
    """从 manifest.json 直接构建 prompt。"""
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    all_entries = []
    for screen in reversed(manifest.get("screens", [])):
        segs = screen.get("msgs", [])
        time_hint = ""
        for seg in segs:
            if seg.get("factor") == "time":
                time_hint = seg.get("content", "")
                break
        entries = parse_segments(segs, time_hint=time_hint)
        all_entries.extend(entries)

    if format == "json":
        return build_prompt_json(all_entries, group_name=group_name)
    elif format == "compact":
        return build_prompt_compact(all_entries, group_name=group_name)
    else:
        return build_prompt_text(all_entries, group_name=group_name, **kwargs)


def build_prompt(entries: List[MessageEntry], format: str = "text", **kwargs) -> str:
    """统一入口。"""
    if format == "json":
        return build_prompt_json(entries, **kwargs)
    elif format == "compact":
        return build_prompt_compact(entries, **kwargs)
    else:
        return build_prompt_text(entries, **kwargs)


if __name__ == "__main__":
    test_entries = [
        MessageEntry(content="昨天 23:32", content_type="time_divider"),
        MessageEntry(sender="26cs潘奥文", content="所以陈曦是个名人吗", matched=True, avatar_score=0.92, nick_score=0.85),
        MessageEntry(sender="25ai盛子楠", content="名猫", matched=True, avatar_score=0.88),
        MessageEntry(content="00:49", content_type="time_divider"),
        MessageEntry(sender="23 ai ljz", content="哥哥和我吃饭", matched=True, avatar_score=0.75, nick_score=0.65),
        MessageEntry(sender="24数2ymh", content="妈妈", matched=False, avatar_score=0.45),
    ]
    print("=== TEXT ===")
    print(build_prompt_text(test_entries, group_name="被打信科2026游泳馆", include_meta=True))
    print()
    print("=== COMPACT ===")
    print(build_prompt_compact(test_entries, group_name="被打信科2026游泳馆"))
