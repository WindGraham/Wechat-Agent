# -*- coding: utf-8 -*-
"""proxy/media_enrich.py — prompt 多媒体增强（2026-08-27 用户定稿）。

决策 prompt 构造时扫描最近消息窗口（history 200 条 + 新消息）：
  - 链接消息（content_type=link，content 含 URL）→ 抓取网页正文，
    作为【链接内容】块追加到 user prompt 尾部（web_fetch 带磁盘缓存）；
  - 图片/表情消息（content_type ∈ image/sticker/media 且 media_path 指向
    本地文件）→ 返回路径列表，由 decider 以 image_url 形式直发多模态模型。

聊天日志里只存 URL / 本地路径（轻），正文与图片像素不进日志。
任何单条失败只跳过该条，绝不影响决策主流程。
"""

import logging
import os
import re

log = logging.getLogger("decision.proxy.media_enrich")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
WORKSPACE = os.path.join(PROJECT_ROOT, "workspace")
MEDIA_ROOT = os.path.join(WORKSPACE, "media")

_URL_RE = re.compile(r"https?://[^\s，。）)\]】\"']+")
IMAGE_TYPES = {"image", "sticker", "media", "video"}


def _msg_text(m) -> str:
    return getattr(m, "content", "") or ""


def _extract_url(m):
    if getattr(m, "content_type", "") != "link":
        return None
    mt = _URL_RE.search(_msg_text(m))
    return mt.group(0) if mt else None


def _image_abs(m):
    """媒体消息的本地图片绝对路径（不存在返回 None）。"""
    if getattr(m, "content_type", "") not in IMAGE_TYPES:
        return None
    p = getattr(m, "media_path", None) or ""
    if not p:
        return None
    if not os.path.isabs(p):
        # 相对路径两處都試：workspace/media（归档约定）与 workspace（crops）
        for root in (MEDIA_ROOT, WORKSPACE):
            cand = os.path.join(root, p)
            if os.path.exists(cand):
                return cand
        return None
    return p if os.path.exists(p) else None


def collect_media(history, new_msgs, link_limit=3, image_limit=4,
                  image_types=IMAGE_TYPES):
    """从窗口消息里选出要增强的链接 URL 与图片路径（时间序，最新优先截断）。

    返回 (urls, image_paths)：均为旧→新顺序，最多 link_limit / image_limit 条。
    """
    msgs = list(history or []) + list(new_msgs or [])
    urls, seen_url = [], set()
    images = []
    for m in reversed(msgs):   # 从新到旧挑，截断后恢复时间序
        if len(urls) < link_limit:
            u = _extract_url(m)
            if u and u not in seen_url:
                seen_url.add(u)
                urls.append(u)
        if len(images) < image_limit \
                and getattr(m, "content_type", "") in image_types:
            p = _image_abs(m)
            if p and p not in images:
                images.append(p)
    urls.reverse()
    images.reverse()
    return urls, images


def build_link_block(urls, max_chars=3000, fetch_fn=None) -> str:
    """抓取链接正文 → 【链接内容】块文本。全部失败返回空串。"""
    if fetch_fn is None:
        from ..search.web_fetch import fetch_text
        fetch_fn = fetch_text
    parts = []
    for u in urls:
        try:
            text = fetch_fn(u, max_chars=max_chars)
        except Exception:  # noqa: BLE001
            log.exception("链接正文抓取异常: %s", u)
            text = None
        if text:
            parts.append(f"🔗 {u}\n{text}")
    if not parts:
        return ""
    return "【链接内容】\n" + "\n\n".join(parts)


def enrich(history, new_msgs, rt) -> tuple:
    """一站式增强：返回 (tail_text, image_paths)。

    rt: runtime 配置读取 callable(key, default)。涉及键：
      prompt_crawl_links (True) / link_crawl_per_prompt (3) /
      link_crawl_max_chars (3000) / prompt_attach_images_max (4)
    """
    crawl = bool(rt("prompt_crawl_links", True))
    urls, images = collect_media(
        history, new_msgs,
        link_limit=int(rt("link_crawl_per_prompt", 3)) if crawl else 0,
        image_limit=int(rt("prompt_attach_images_max", 4)))
    tail = build_link_block(
        urls, max_chars=int(rt("link_crawl_max_chars", 3000))) if urls else ""
    return tail, images


# ------------------------------------------------------------------ 图片直发
def encode_image_b64(path: str, max_px: int = 768):
    """本地图片 → base64 JPEG（长边 ≤max_px）。失败返回 None。"""
    try:
        import base64
        import cv2
        img = cv2.imread(path)
        if img is None:
            return None
        h, w = img.shape[:2]
        scale = max_px / max(h, w)
        if scale < 1.0:
            img = cv2.resize(img, (max(1, int(w * scale)),
                                   max(1, int(h * scale))),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None
        return base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:  # noqa: BLE001
        log.exception("图片编码失败: %s", path)
        return None


def attach_images(messages, image_paths, max_px: int = 768) -> tuple:
    """把最后一条 user 消息的 content 转成 OpenAI content 数组
    （text + image_url×N），图片直发多模态模型。

    返回 (new_messages, n_attached)；无可用图片/无 user 消息时原样返回 0。
    """
    img_parts = []
    for p in image_paths:
        b64 = encode_image_b64(p, max_px=max_px)
        if b64:
            img_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                              "detail": "auto"}})
    if not img_parts:
        return messages, 0
    out = [dict(m) for m in messages]
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user":
            text = out[i].get("content") or ""
            if not isinstance(text, str):
                return messages, 0
            out[i]["content"] = [{"type": "text", "text": text}] + img_parts
            return out, len(img_parts)
    return messages, 0
