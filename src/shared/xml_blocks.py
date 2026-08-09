# -*- coding: utf-8 -*-
"""shared/xml_blocks.py — LLM 输出 XML 动作块扫描器（决策层契约解析）。

块类型： <reply ...>...</reply> / <task ...>...</task> / <tool .../> / <silent/>
容错：逐块扫描，单个坏块（缺闭合/未知标签）只丢自己，不污染其他块。
转义：块内文本的 &lt; &amp; 在返回前反转义一次（LLM 按契约转义）。
"""

import re

BLOCK_TAGS = ("reply", "task", "tool", "silent")

_OPEN_RE = re.compile(r"<(reply|task|tool|silent)(\s[^>]*)?(/?)>")


class Block:
    """一个动作块。tag/attrs（原始属性串）/inner（内部文本，已反转义）/
    raw_inner（原始文本，未反转义——转发给交互层必须用它，否则
    <text> 等结构标签会被转义成字面量）/self_closing/valid。"""

    __slots__ = ("tag", "attrs", "inner", "raw_inner", "self_closing",
                 "valid", "error")

    def __init__(self, tag, attrs, inner, self_closing, valid=True, error="",
                 raw_inner=None):
        self.tag = tag
        self.attrs = attrs or ""
        self.inner = inner
        self.raw_inner = raw_inner if raw_inner is not None else inner
        self.self_closing = self_closing
        self.valid = valid
        self.error = error

    def __repr__(self):
        return f"<Block {self.tag} valid={self.valid} {self.attrs!r}>"


def unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def parse_attrs(attrs_str: str) -> dict:
    """' session="x" ref="m1"' → dict。"""
    return {m.group(1): m.group(2)
            for m in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', attrs_str or "")}


def extract_blocks(text: str) -> list:
    """顺序扫描顶层动作块，返回 Block 列表（坏块 valid=False 保留位置）。"""
    blocks = []
    pos = 0
    n = len(text or "")
    while pos < n:
        m = _OPEN_RE.search(text, pos)
        if not m:
            break
        tag = m.group(1)
        attrs = m.group(2) or ""
        self_closing = bool(m.group(3)) or tag in ("tool", "silent")
        open_end = m.end()

        if self_closing:
            blocks.append(Block(tag, attrs, "", True))
            pos = open_end
            continue

        close_tag = f"</{tag}>"
        close_idx = text.find(close_tag, open_end)
        next_open = _OPEN_RE.search(text, open_end)

        if close_idx < 0:
            # 无闭合：坏块，丢弃，从下一个块开头继续
            blocks.append(Block(tag, attrs, "", False,
                                valid=False, error="missing close tag"))
            pos = next_open.start() if next_open else n
            continue
        if next_open and next_open.start() < close_idx:
            # 闭合前先出现另一个块开头 → 本块没闭合，丢弃本块
            blocks.append(Block(tag, attrs, "", False,
                                valid=False, error="unclosed block"))
            pos = next_open.start()
            continue

        inner_raw = text[open_end:close_idx]
        blocks.append(Block(tag, attrs, unescape(inner_raw), False,
                            raw_inner=inner_raw))
        pos = close_idx + len(close_tag)
    return blocks
