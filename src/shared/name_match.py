# -*- coding: utf-8 -*-
"""name_match.py — 会话名/昵称匹配（OCR 容错）。

从 action/wechat_tools 下沉到 shared：perception(scanner) 与 action 都用，
消除 perception → action 的反向依赖。

判定层级：
1. 归一化（去空白、去括号成员数）后相等或互为包含
2. 省略号分段按序匹配（聊天页标题栏长名字被微信省略成 '前段...后段'）
3. OCR 易混字符 fold（0/O→o，小写化）后再判相等/包含
"""

import re


def _norm(s):
    """名字匹配归一化：去空白和括号成员数。"""
    if not s:
        return ""
    s = "".join(str(s).split())
    for sep in ("(", "（"):
        if sep in s:
            s = s.split(sep)[0]
    return s


def _fold(s):
    """OCR 易混字符折叠（实测："Doo" 被识别成 "Do0"）：0/O->o，小写化。"""
    return s.lower().replace("0", "o")


def _elide_match(elided, full):
    """聊天页标题栏长名字会被微信省略成 '前段..后段'：分段按序匹配全文。
    OCR 可能把省略号读成单个 '.'（2026-08-04 实测 '怨憎会爱别离要.风要雨得雨'），
    因此单个点也按省略处理。"""
    parts = [p for p in re.split(r"\.+|…", elided) if p]
    if len(parts) < 2:
        return False
    pos = 0
    for p in parts:
        i = full.find(p, pos)
        if i < 0:
            return False
        pos = i + len(p)
    return True


def _name_match(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    if "." in a or "." in b or "…" in a or "…" in b:
        return _elide_match(a, b) or _elide_match(b, a)
    fa, fb = _fold(a), _fold(b)          # OCR 混淆容错
    return fa == fb or fa in fb or fb in fa
