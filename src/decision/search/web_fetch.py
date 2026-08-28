# -*- coding: utf-8 -*-
"""decision/search/web_fetch.py — 链接正文抓取（prompt 多媒体增强用）。

聊天里的链接消息（[链接] <url>）在决策 prompt 尾部追加网页正文，
让模型直接读到链接内容（2026-08-27 用户定稿）。

实现要点：
  - requests GET（桌面 UA，timeout 15s）；
  - HTML 去 script/style/标签取正文（mp.weixin.qq.com 服务端渲染，
    无需 JS 即可解析；其他站点尽力而为）；
  - 磁盘缓存 workspace/media/link_cache/<sha1(url)>.txt，命中不重抓
    （同一链接在 200 条窗口里会反复出现，缓存是必须的不是优化）；
  - 任何失败返回 None（调用方跳过该条，绝不影响决策主流程）。
"""

import hashlib
import html
import logging
import os
import re

log = logging.getLogger("decision.search.web_fetch")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
CACHE_DIR = os.path.join(PROJECT_ROOT, "workspace", "media", "link_cache")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_WS_RE = re.compile(r"[ \t\u3000\xa0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _cache_path(url: str) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}.txt")


def _html_to_text(page: str) -> str:
    """HTML → 纯文本（标题 + 正文，去脚本/样式/标签）。"""
    title = ""
    m = _TITLE_RE.search(page)
    if m:
        title = html.unescape(m.group(1)).strip()
    body = _SCRIPT_STYLE_RE.sub("\n", page)
    body = _TAG_RE.sub("\n", body)
    body = html.unescape(body)
    lines = []
    for ln in body.splitlines():
        ln = _WS_RE.sub(" ", ln).strip()
        if ln:
            lines.append(ln)
    text = "\n".join(lines)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    if title and title not in text[:200]:
        text = title + "\n" + text
    return text.strip()


def fetch_text(url: str, max_chars: int = 3000, timeout: int = 15,
               use_cache: bool = True):
    """抓 url 的正文文本（≤max_chars）。失败/无内容返回 None。"""
    if not url or not url.startswith(("http://", "https://")):
        return None

    if use_cache:
        try:
            with open(_cache_path(url), encoding="utf-8") as f:
                cached = f.read()
            if cached:
                return cached[:max_chars]
        except OSError:
            pass

    try:
        import requests
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200:
            log.warning("fetch %s -> HTTP %d", url, r.status_code)
            return None
        r.encoding = r.apparent_encoding or r.encoding
        text = _html_to_text(r.text)
    except Exception as e:  # noqa: BLE001
        log.warning("fetch %s failed: %s: %s", url, type(e).__name__, e)
        return None

    if not text:
        return None

    # 写缓存（全量，截断在读取侧做；缓存失败不影响返回）
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(url), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        log.exception("link cache 写入失败: %s", url)
    return text[:max_chars]
