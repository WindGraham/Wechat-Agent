# -*- coding: utf-8 -*-
"""proxy/media.py — 媒体转换队列：未标注多媒体 → 多模态文字描述。

时限并发（默认 2），转换结果经交互层接口写回消息日志。
某会话决策前，其新消息里的媒体条目必须转换完成（或超时降级）。
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("decision.proxy.media")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
MEDIA_ROOT = os.path.join(PROJECT_ROOT, "workspace", "media")

MEDIA_TYPES = {"multimedia", "image", "sticker", "voice", "video",
               "unknown_nontext"}
PROMPT = ("详细描述这张微信聊天中的图片或表情内容。如果是表情包，描述画面"
          "和文字；如果是照片，描述场景。用 2-4 句话。")
PER_ITEM_TIMEOUT = 45        # 单条视觉识别超时（秒），超时降级占位符


class MediaConverter:
    """provider: 带 vision_file 的 LLM provider；
    writer: 写回回调 fn(session, sender, old_content, new_content)。"""

    def __init__(self, provider, writer, max_workers: int = 2):
        self._provider = provider
        self._writer = writer
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="media")

    def needs_convert(self, msg) -> bool:
        """该消息是否需要媒体转换（未标注的多媒体）。"""
        ctype = getattr(msg, "content_type", "text") or "text"
        if ctype not in MEDIA_TYPES:
            return False
        if not getattr(msg, "media_path", None):
            return False
        return "内容:" not in (getattr(msg, "content", "") or "")

    def convert_all(self, session: str, messages) -> int:
        """并发转换 messages 里所有待标注条目，写回日志并更新内存。
        返回转换成功条数。单条失败/超时 → 保留占位符，不阻塞。"""
        targets = [m for m in messages if self.needs_convert(m)]
        if not targets:
            return 0

        def _one(m):
            try:
                # media_path 可能是相对 workspace/media 的路径（归档约定）
                path = m.media_path
                if not os.path.isabs(path):
                    path = os.path.join(MEDIA_ROOT, path)
                desc = self._provider.vision_file(path, PROMPT)
                desc = (desc or "").strip()[:300]
                if not desc:
                    return False
                orig = (m.content or "").strip() or "[图片]"
                new_content = f"{orig}\n内容: {desc}"
                self._writer(session, m.sender, m.content, new_content)
                m.content = new_content
                return True
            except Exception:  # noqa: BLE001
                log.exception("媒体转换失败: %s", m.media_path)
                return False

        futures = [self._pool.submit(_one, m) for m in targets]
        done = 0
        for f in futures:
            try:
                if f.result(timeout=PER_ITEM_TIMEOUT):
                    done += 1
            except Exception:  # noqa: BLE001
                log.warning("媒体转换超时/异常，降级占位符")
        if done:
            log.info("[%s] 媒体转换完成 %d/%d", session, done, len(targets))
        return done
