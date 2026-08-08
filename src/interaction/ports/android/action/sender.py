# -*- coding: utf-8 -*-
"""sender.py — 拟人发送：标点分段 + 随机延迟 + 重试（action 层）。

在 wechat_tools.send_text 之上叠加"人味"延迟层：
  - 首段前延迟 2~6s + 回复长度 × 随机系数（上限 FIRST_DELAY_MAX，防刷屏）
  - 段间按 jieba 词数计算 log 间隔（段越长停顿越久，像真人分段打字）
  - 每段发送失败等 0.5~1.5s 重试 1 次，仍失败抛 RuntimeError
底层 ADB 输入/触控随机化（高斯偏移、贝塞尔、jitter）都在 device_ctl 内，
本层只负责节奏。随机延迟是风控对冲的硬需求，不可删除。
"""

import logging
import math
import random
import re
import time

import jieba

log = logging.getLogger("action.sender")

FIRST_DELAY_MAX = 30.0        # 首段前延迟上限（秒）
SEG_DELAY_K = 1.2             # 段间 log(词数+1) 系数
SEG_DELAY_MAX = 8.0           # 段间延迟上限（秒）
MAX_SEGMENTS = 3              # 最多分段数，超出合并进最后一段
_SEG_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")


class Sender:
    """拟人消息发送器。sleep_fn / rand_fn / clock 可注入以便单测。"""

    def __init__(self, tools, context=None, sleep_fn=time.sleep,
                 rand_fn=random.uniform, clock=time.time):
        self.tools = tools            # WeChatTools（send_text / dev）
        self._ctx = context           # SessionContext（可选，用于 self_send 入库）
        self.sleep_fn = sleep_fn
        self.rand_fn = rand_fn
        self.clock = clock

    # ---------------------------------------------------------- 主入口
    def send(self, session, reply_text) -> bool:
        """向会话发送回复，成功返回 True，重试后仍失败抛 RuntimeError。"""
        reply = reply_text or ""
        segments = self._split_segments(reply)
        delay = min(FIRST_DELAY_MAX,
                    self.rand_fn(2, 6) + len(reply) * self.rand_fn(0.1, 0.25))
        log.info("[%s] sending %d segment(s), first delay %.1fs",
                 session, len(segments), delay)
        self.sleep_fn(delay)

        start = self.clock()
        for i, seg in enumerate(segments):
            if i > 0:                       # 段间停顿：像人换行再打下一句
                words = len(jieba.lcut(seg))
                gap = min(SEG_DELAY_MAX,
                          math.log(words + 1) * SEG_DELAY_K + self.rand_fn(0, 0.5))
                self.sleep_fn(gap)
            self._send_once(seg)
            log.info("[%s] sent segment %d/%d (%.1fs): %r",
                     session, i + 1, len(segments), self.clock() - start, seg)
        self._log_self_send(session, reply)
        return True

    def _log_self_send(self, session, text):
        """发送回执入库（source='self_send'，gap_ok=True）。"""
        if self._ctx is None:
            return
        try:
            from ..perception.reader import SnapEntry
            sid = self._ctx.get_or_create_session(session, True)
            self._ctx.append_incremental(sid, [SnapEntry(
                kind="msg", sender="self", content=text,
                content_type="text", is_mine=True)],
                source="self_send", gap_ok=True)
        except Exception:
            log.exception("log self send failed: %s", session)

    def _send_once(self, seg):
        """发送一段：失败等 0.5~1.5s 重试 1 次，仍失败抛 RuntimeError。"""
        r = self.tools.send_text(seg)
        if r.success:
            return r
        log.warning("send failed (%s), retry once", r.error)
        self.sleep_fn(self.rand_fn(0.5, 1.5))
        r = self.tools.send_text(seg)
        if not r.success:
            raise RuntimeError(f"send_text failed after retry: {r.error}")
        return r

    # ---------------------------------------------------------- 工具方法
    @staticmethod
    def _split_segments(reply, max_seg=MAX_SEGMENTS):
        """按标点边界分句；超过 max_seg 段时把溢出部分合并进最后一段。"""
        parts = [p.strip() for p in _SEG_SPLIT_RE.split(reply) if p.strip()]
        if not parts:
            return [reply.strip()]
        if len(parts) <= max_seg:
            return parts
        return parts[:max_seg - 1] + ["".join(parts[max_seg - 1:])]
