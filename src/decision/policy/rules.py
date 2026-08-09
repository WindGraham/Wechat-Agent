# -*- coding: utf-8 -*-
"""policy/rules.py — 回复策略：必回判定、@我判定、防重登记、兜底。"""

import hashlib
import json
import logging
import os
import re
import unicodedata

log = logging.getLogger("decision.policy")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
STORE_PATH = os.path.join(PROJECT_ROOT, "workspace", "runtime",
                          "replied_mentions.json")

AT_ALL = "@所有人"
_MENTION_RE = re.compile(r"@([^\s@，,：:]+)")
FALLBACK_REPLY = "在的，刚看到"


def _norm(s: str) -> str:
    """昵称归一化：NFKC + 去空白 + 小写（容忍 OCR 粘连/截断）。"""
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+", "", t).lower()


def _nick_match(a: str, b: str) -> bool:
    """归一化后相等或互为包含（'陈曦你可以调取本机的' 命中 '陈曦'）。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


class RepliedMentionStore:
    """已回复 @ 登记（workspace/runtime/replied_mentions.json 持久化）。

    key = sha1(归一化 sender|content)；每会话 FIFO 保留最近 max_per_session 个。
    """

    def __init__(self, path=STORE_PATH, max_per_session=1000):
        self._path = path
        self._max = max_per_session
        self._data = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                log.warning("replied_mentions.json 损坏，重建空登记")
                self._data = {}

    @staticmethod
    def _key(sender: str, content: str) -> str:
        raw = f"{_norm(sender)}|{_norm(content)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def is_replied(self, session: str, sender: str, content: str) -> bool:
        return self._key(sender, content) in set(self._data.get(session, []))

    def mark_replied(self, session: str, sender: str, content: str):
        keys = self._data.setdefault(session, [])
        k = self._key(sender, content)
        if k not in keys:
            keys.append(k)
            del keys[:-self._max]
            self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False)
        os.replace(tmp, self._path)


class Policy:
    """回复策略判定。owner_nick=主人在群里的昵称（@判定用）。"""

    def __init__(self, owner: str = "", owner_nick: str = "",
                 store: RepliedMentionStore = None):
        self._owner = owner
        self._owner_nick = owner_nick
        self._store = store or RepliedMentionStore()

    # ---------------------------------------------------------------- @判定
    def is_at_me(self, msg) -> bool:
        """CONTRACTS §六：mentions 命中主人昵称 / 内容含 @所有人 /
        内容直含 @主人昵称。msg 为 Message 或 dict。"""
        get = (lambda k, d=None: msg.get(k, d)) if isinstance(msg, dict) \
            else (lambda k, d=None: getattr(msg, k, d))
        content = get("content", "") or ""
        if AT_ALL in content:
            return True
        mentions = get("mentions", None)
        if mentions is None:
            mentions = _MENTION_RE.findall(content) if "@" in content else []
        if any(_nick_match(m, self._owner_nick) for m in mentions):
            return True
        return f"@{self._owner_nick}" in content

    # ---------------------------------------------------------------- 必回
    def must_reply(self, session: str, is_group: bool, new_messages) -> bool:
        """私聊必回；群聊 @我 必回；主人说话必回。"""
        if not is_group:
            return True
        if session == self._owner or any(
                _nick_match(getattr(m, "sender", ""), self._owner)
                for m in new_messages):
            return True
        return any(self.is_at_me(m) for m in new_messages)

    def unreplied_mentions(self, session: str, new_messages) -> list:
        """新消息里 @我 且尚未回复的条目（逐条必回用）。"""
        out = []
        for m in new_messages:
            if getattr(m, "is_mine", False):
                continue
            if not self.is_at_me(m):
                continue
            if self._store.is_replied(session, m.sender, m.content):
                continue
            out.append(m)
        return out

    def mark_replied(self, session: str, msg):
        self._store.mark_replied(session, msg.sender, msg.content)

    # ---------------------------------------------------------------- 兜底
    @staticmethod
    def fallback_bundle(session: str) -> str:
        """必回场景模型持续沉默时的兜底回复（XML bundle）。"""
        return (f'<reply session="{session}">'
                f"<text>{FALLBACK_REPLY}</text></reply>")
