#!/usr/bin/env python3
"""media_archive.py - 多媒体消息截图归档（WP7）。

需求：multimedia 消息（图片/表情/视频等非文字泡）在打标的同时，
把按头像边界划分出的那一整条消息段（群聊含昵称行 + 多媒体内容）
从全屏截图上裁下来，存到该会话专有的文件夹，并给每条一个标识码
（media_id），标注与截图文件一一对应。

- 目录：<root>/<sanitize(session)>/<media_id>.png + <media_id>.json sidecar；
  sanitize 规则与 msg_log.export_text_log 一致（[\\/:*?"<>|] → _）。
- media_id = "m" + align_key(sender, content)[:10]（sha1 前缀，确定性）；
  同 id 文件已存在则跳过重写（幂等，重跑不产生重复文件），
  但消息上的 media_id/media_path/media_crop 标注每次都会补上。
- sidecar json：media_id/seq_in_session/sender/nickname/content_type/
  lines/y/ocr_conf/media_crop。
- media_path 为相对 root 的路径（"<session>/<media_id>.png"）。
"""

import json
import os
import re

import cv2

from ....msglog.message_log import align_key
from . import layout_consts as LC

CROP_PAD = 8                      # 段条带上下各留的边距（px）
BAND_Y0 = LC.CONTENT_Y0           # 内容区顶 200
BAND_Y1 = LC.INPUT_BAR_Y0         # 内容区底（输入栏顶）2110

_SAFE_RE = re.compile(r'[\\/:*?"<>|]')

# 归档根目录：workspace/media（相对 CWD 的默认值曾导致裁图散落仓库根）
PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "..", "..", ".."))
DEFAULT_MEDIA_ROOT = os.path.join(PROJECT_ROOT, "workspace", "media")


def sanitize_session(name):
    """会话名 -> 合法目录名（与 msg_log.export_text_log 同规则）"""
    return _SAFE_RE.sub("_", name or "") or "session"


def _sender_of(msg):
    """media_id 用的发送者标识：自己=self，对方=昵称（读不到退化为 other）"""
    if msg.get("side") == "self":
        return "self"
    return msg.get("nickname") or "other"


def media_id_of(msg):
    """"m" + align_key(sender, content)[:10]，同一消息重算结果不变"""
    return "m" + align_key(_sender_of(msg), msg.get("content") or "")[:10]


def _crop_band(ordered, i):
    """第 i 条消息的整行段条带：段顶 = 消息 y，段底 = 下一条消息 y
    （最后一条到输入栏顶），上下各留 CROP_PAD 并钳制在内容区。"""
    y = int(ordered[i]["y"])
    next_y = int(ordered[i + 1]["y"]) if i + 1 < len(ordered) else BAND_Y1
    y0 = max(BAND_Y0, y - CROP_PAD)
    y1 = min(BAND_Y1, next_y + CROP_PAD)
    return 0, y0, LC.SCREEN_W, max(y1, y0 + 1)


def archive_multimedia(img, session, is_group, messages,
                       root=DEFAULT_MEDIA_ROOT):
    """裁出 messages 中所有 multimedia 消息的段条带并归档。

    img: BGR 全屏截图（与 slice_chat 输入同一张）；messages: slice_chat
    返回的消息列表（按 y 排序）。multimedia 消息会被原地补上
    media_id/media_path/media_crop 字段；返回同一个列表。"""
    safe = sanitize_session(session)
    session_dir = os.path.join(root, safe)
    ordered = sorted(messages, key=lambda m: m["y"])

    for i, msg in enumerate(ordered):
        if msg.get("content_type") != "multimedia":
            continue
        x0, y0, x1, y1 = _crop_band(ordered, i)
        media_id = media_id_of(msg)
        rel_path = os.path.join(safe, media_id + ".png")
        png_path = os.path.join(root, rel_path)
        msg["media_id"] = media_id
        msg["media_path"] = rel_path
        msg["media_crop"] = [x0, y0, x1, y1]
        if os.path.exists(png_path):
            continue                                # 幂等：同 id 跳过重写
        os.makedirs(session_dir, exist_ok=True)
        cv2.imwrite(png_path, img[y0:y1, x0:x1])
        seq = sum(1 for f in os.listdir(session_dir) if f.endswith(".png"))
        sidecar = {
            "media_id": media_id,
            "seq_in_session": seq,
            "session": session,
            "is_group": bool(is_group),
            "sender": _sender_of(msg),
            "nickname": msg.get("nickname"),
            "content_type": msg["content_type"],
            "lines": list(msg.get("lines") or []),
            "y": int(msg["y"]),
            "ocr_conf": msg.get("ocr_conf"),
            "media_crop": [x0, y0, x1, y1],
        }
        with open(os.path.join(session_dir, media_id + ".json"),
                  "w", encoding="utf-8") as f:
            json.dump(sidecar, f, ensure_ascii=False, indent=2)
    return messages
