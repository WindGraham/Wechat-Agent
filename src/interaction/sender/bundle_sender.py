# -*- coding: utf-8 -*-
"""bundle_sender.py — XML 动作包解释执行器。

输入：决策层的 XML 动作块文本（<reply>/<silent/> 等）
输出：ActionResult

核心规则：
- 多个 <reply> 块按出现顺序逐个执行
- 每个 <reply> 内的多个 <text> 为拟人拆句连发
- <quote> 先于 <text> 执行（引用回复流程）
- <image>/<file> 走相册/加号面板流程
- 执行期间屏幕被占，发现工作顺延（屏幕互斥锁）
- 失败有重试上限，耗尽排到队尾

与旧 sender.py 的关系：
- 旧 sender.send(session, text) 是底层发送原语（拟人分段+随机延迟）
- 本模块在其之上增加 XML 解析、引用/图片/文件等多模态动作
"""

import logging
import math
import random
import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

from ...shared.types import ActionResult

log = logging.getLogger("interaction.sender")

# ------------------------------------------------------------------ XML 解析
_REPLY_TAG_RE = re.compile(r"<reply[^>]*>.*?</reply>", re.DOTALL)
_SILENT_RE = re.compile(r"<silent\s*/>")
_IMAGE_RE = re.compile(r"<image\s+path\s*=\s*['\"]([^'\"]+)['\"]\s*/>")
_FILE_RE = re.compile(r"<file\s+path\s*=\s*['\"]([^'\"]+)['\"]\s*/>")

# ------------------------------------------------------------------ 拟人发送参数
FIRST_DELAY_MAX = 30.0        # 首段前延迟上限（秒）
SEG_DELAY_K = 1.2             # 段间 log(词数+1) 系数
SEG_DELAY_MAX = 8.0           # 段间延迟上限（秒）
MAX_SEGMENTS = 3              # 最多分段数
_SEG_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")

MAX_ACTION_ATTEMPTS = 2       # 行动条目尝试上限


class BundleSender:
    """XML 动作包解释执行器。

    依赖注入：
    - port_sender: 端口发送器（ports/android/action/sender.py 的 Sender）
    - port_navigator: 端口导航器（ports/android/action/navigator.py 的 Navigator）
    - port_tools: 端口工具（WeChatTools，发图/发文件/引用回复用）
    - msglog_conn: 消息日志连接（self_send 入库用）
    """

    def __init__(self, port_sender, port_navigator, port_tools,
                 msglog_conn=None):
        self._sender = port_sender          # 底层拟人发送器
        self._nav = port_navigator           # 导航器
        self._tools = port_tools             # WeChatTools（发图/发文件）
        self._conn = msglog_conn             # 消息日志连接
        self._mutex = False                  # 屏幕互斥锁（发送中=True）
        self._rand = random.uniform
        self._clock = time.time

    # ------------------------------------------------------------------ 主入口
    def submit_bundle(self, session: str, blocks_xml: str) -> ActionResult:
        """决策层 → 交互层：投递一包 XML 动作块。

        只处理 <reply> 和 <silent/>。
        <task> 块由 Proxy 分流到工具层，不出现在这里。
        """
        if not blocks_xml or not blocks_xml.strip():
            return ActionResult(ok=True)

        # 屏幕互斥：获取发送锁
        if self._mutex:
            return ActionResult(ok=False, error="屏幕正忙（发现工作中），请稍后重试",
                                retryable=True,
                                escalation_hint="发送队列阻塞")
        self._mutex = True
        try:
            return self._execute_bundle(session, blocks_xml)
        finally:
            self._mutex = False

    def _execute_bundle(self, session: str, blocks_xml: str) -> ActionResult:
        """执行一包动作块。"""
        # 提取所有 <reply> 块
        reply_blocks = _REPLY_TAG_RE.findall(blocks_xml)
        has_silent = bool(_SILENT_RE.search(blocks_xml))

        if not reply_blocks and has_silent:
            return ActionResult(ok=True)  # 沉默，无动作

        if not reply_blocks:
            return ActionResult(ok=False,
                                error="XML 包中无可执行的 <reply> 块",
                                retryable=False)

        # 逐块执行（最多 3 个 reply 块，防刷屏）
        for i, block in enumerate(reply_blocks[:3]):
            result = self._execute_reply(session, block)
            if not result.ok:
                return result

        return ActionResult(ok=True)

    def _execute_reply(self, session: str, block_xml: str) -> ActionResult:
        """执行单个 <reply> 块。"""
        try:
            # 解析 XML（容忍不规范的 XML）
            root = ET.fromstring(block_xml)
        except ET.ParseError:
            # 放宽解析：去掉属性中的特殊字符再试
            try:
                cleaned = re.sub(r'&(?!lt;|amp;|gt;|quot;)', '&amp;', block_xml)
                root = ET.fromstring(cleaned)
            except ET.ParseError as e:
                log.warning("XML parse error: %s", e)
                return ActionResult(ok=False, error=f"XML 解析失败: {e}",
                                    retryable=False)

        # 1. 先执行 <quote>（引用回复流程）
        quote_elem = root.find("quote")
        if quote_elem is not None:
            quote_match = (quote_elem.get("match") or
                           quote_elem.text or "").strip()
            if quote_match:
                self._do_quote_reply(session, quote_match)

        # 2. 执行 <image>（发图片）
        image_elem = root.find("image")
        if image_elem is not None:
            img_path = image_elem.get("path", "").strip()
            if img_path:
                return self._do_send_image(session, img_path)

        # 3. 执行 <file>（发文件）
        file_elem = root.find("file")
        if file_elem is not None:
            file_path = file_elem.get("path", "").strip()
            if file_path:
                return self._do_send_file(session, file_path)

        # 4. 执行 <text>（拟人拆句连发）
        text_elems = root.findall("text")
        ref = root.get("ref", "")

        if text_elems:
            texts = [self._decode_xml_text(e.text or "") for e in text_elems]
            # 合并所有文本段，由底层 sender 分段发送
            full_text = "".join(texts)
            return self._do_send_text(session, full_text)
        else:
            # 没有 <text> 也没有 image/file/quote：检查是否有裸文本
            tail = root.tail or ""
            if root.text and root.text.strip():
                return self._do_send_text(session,
                                          self._decode_xml_text(root.text))
            return ActionResult(ok=True)

    # ------------------------------------------------------------------ 动作实现
    def _do_send_text(self, session: str, text: str) -> ActionResult:
        """拟人分段发送文本。复用端口 sender 的延迟/分段逻辑。"""
        if not text.strip():
            return ActionResult(ok=True)

        # 进入会话
        r = self._nav.enter_session(session)
        if not r.success:
            return ActionResult(ok=False,
                                error=f"进入会话失败: {r.error}",
                                retryable=True,
                                escalation_hint=f"无法进入会话 {session}")

        # 发送
        try:
            self._sender.send(session, text)
            return ActionResult(ok=True)
        except Exception as e:
            log.exception("[%s] send_text failed", session)
            return ActionResult(ok=False, error=str(e), retryable=True,
                                escalation_hint=f"发送文本到 {session} 失败")

    def _do_quote_reply(self, session: str, match_text: str) -> ActionResult:
        """引用回复：长按目标气泡 → 菜单找"引用" → 后续由 _do_send_text 完成。"""
        log.info("[%s] quote reply: match=%r", session, match_text[:40])
        # 引用回复的底层实现较复杂（需长按气泡+OCR菜单），
        # 当前由端口工具层处理。这里只做标记。
        # TODO: 完整实现引用回复流程
        return ActionResult(ok=True)

    def _do_send_image(self, session: str, path: str) -> ActionResult:
        """发送图片：加号面板 → 相册 → 选择 → 发送。"""
        log.info("[%s] send image: %s", session, path)
        try:
            # 由端口工具层实现
            # self._tools.send_image(session, path)
            return ActionResult(ok=False, error="发图片功能待实现",
                                retryable=False,
                                escalation_hint="图片发送需端口实现")
        except Exception as e:
            return ActionResult(ok=False, error=str(e), retryable=True)

    def _do_send_file(self, session: str, path: str) -> ActionResult:
        """发送文件：加号面板 → 文件 → 选择 → 发送。"""
        log.info("[%s] send file: %s", session, path)
        try:
            # 由端口工具层实现
            return ActionResult(ok=False, error="发文件功能待实现",
                                retryable=False,
                                escalation_hint="文件发送需端口实现")
        except Exception as e:
            return ActionResult(ok=False, error=str(e), retryable=True)

    # ------------------------------------------------------------------ 工具方法
    @staticmethod
    def _decode_xml_text(text: str) -> str:
        """反转义 XML 文本中的 &lt; &amp; 等。"""
        if not text:
            return ""
        return text.replace("&lt;", "<").replace("&gt;", ">") \
                   .replace("&amp;", "&").replace("&quot;", '"')
