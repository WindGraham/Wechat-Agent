# -*- coding: utf-8 -*-
"""bundle_sender.py — XML 动作包解释执行器。

输入：决策层的 XML 动作块文本（<reply>/<silent/> 等）
输出：ActionResult

核心规则：
- 逐块扫描提取顶层块；单个坏块（缺闭合标签）只丢自己，不污染后续好块
- 多个 <reply> 块按出现顺序逐个执行（最多 3 个，防刷屏）
- 每个 <reply> 内的多个 <text> = 拆成多条小消息依次发送（条间随机延迟 1~3s）；
  单条 <text> 长文不拆（整条交给端口 sender，其内部分段是另一回事）
- 文本里的 &lt;/&amp; 由 ET 解析时反转义，结果就是最终文本，不做二次反转义
- <quote> + <text>：第一条 <text> 走引用回复流程发出，其余 <text> 照常逐条发
- <image>/<file> 走相册/加号面板流程（action/image_sender.py）
- 执行期间屏幕被占，发现工作顺延（屏幕互斥锁）

与端口 action/sender.py 的分工：
- sender.send(session, text) 是底层拟人发送原语（单条消息的标点分段+随机延迟）
- 本模块只做 XML 解析与多模态动作编排，不自己分段
"""

import logging
import random
import re
import time
import xml.etree.ElementTree as ET

from ...shared.types import ActionResult

log = logging.getLogger("interaction.sender")

# ------------------------------------------------------------------ 块提取
# 顶层块开头：<reply ...> / <task ...> / <tool .../> / <silent/> / <moments-post .../>
_BLOCK_START_RE = re.compile(r"<(reply|task|tool|silent|moments-post)(?=[\s>/])")

MAX_REPLY_BLOCKS = 3          # 一包最多执行的 reply 块数（防刷屏）
TEXT_MSG_GAP = (1.0, 3.0)     # 多条 <text> 之间的随机延迟（秒）


def _extract_blocks(xml_text):
    """逐个提取顶层块，返回 [(tag, block_xml), ...]。

    对每个 '<tag' 开头，在其后找最近的 '</tag>'；若该区间内先出现另一个
    块开头（说明上一个没闭合），只丢弃坏块、从新的块开头继续扫。
    自闭合块（<silent/> 等，'>' 前是 '/'）直接提取。
    """
    blocks = []
    pos = 0
    n = len(xml_text)
    while True:
        m = _BLOCK_START_RE.search(xml_text, pos)
        if not m:
            break
        tag = m.group(1)
        gt = xml_text.find(">", m.end())
        if gt == -1:
            log.warning("丢弃坏块（无 '>'）：%r", xml_text[m.start():m.start() + 60])
            break
        if xml_text[gt - 1] == "/":                       # 自闭合
            blocks.append((tag, xml_text[m.start():gt + 1]))
            pos = gt + 1
            continue
        close = xml_text.find(f"</{tag}>", gt + 1)
        nxt = _BLOCK_START_RE.search(xml_text, gt + 1)
        if close == -1 or (nxt and nxt.start() < close):
            # 坏块：未闭合（被下一个块开头截断，或根本没有闭合标签）——只丢自己
            log.warning("丢弃未闭合的坏块：%r", xml_text[m.start():m.start() + 60])
            if nxt:
                pos = nxt.start()
                continue
            break
        blocks.append((tag, xml_text[m.start():close + len(tag) + 3]))
        pos = close + len(tag) + 3
    return blocks


class BundleSender:
    """XML 动作包解释执行器。

    依赖注入：
    - port_sender: 端口发送器（ports/android/action/sender.py 的 Sender）
    - port_navigator: 端口导航器（ports/android/action/navigator.py 的 Navigator）
    - port_tools: 端口工具（WeChatTools，quote/image/file 流程取其 dev）
    """

    def __init__(self, port_sender, port_navigator, port_tools,
                 session_reader=None, emoji_index=None):
        self._sender = port_sender         # 底层拟人发送器
        self._nav = port_navigator         # 导航器
        self._tools = port_tools           # WeChatTools（quote/image/file 用其 dev）
        self._session_reader = session_reader  # 日志定位引用目标方向（可选）
        self._emoji_index_inst = emoji_index   # 表情检索（可选，None 时懒加载默认）
        self._mutex = False                # 屏幕互斥锁（发送中=True）
        self._rand = random.uniform
        self._clock = time.time
        self._sleep = time.sleep

    # ------------------------------------------------------------------ 主入口
    def submit_bundle(self, session: str, blocks_xml: str) -> ActionResult:
        """决策层 → 交互层：投递一包 XML 动作块。

        只执行 <reply> 和 <silent/>。<task>/<tool> 块由 Proxy 分流，
        出现在这里只记日志跳过（提取逻辑同样对它们做坏块隔离）。
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
        """执行一包动作块（<reply>/<silent/>/<moments-post>）。"""
        blocks = _extract_blocks(blocks_xml)
        replies = [b for tag, b in blocks if tag == "reply"]
        moments = [b for tag, b in blocks if tag == "moments-post"]
        has_silent = any(tag == "silent" for tag, _ in blocks)
        for tag, _ in blocks:
            if tag in ("task", "tool"):
                log.warning("<%s> 块不应到达交互层（Proxy 分流），跳过", tag)

        # 朋友圈发布：独立动作，与 reply 正交（决策层经 submit_bundle 投递）
        for block in moments:
            result = self._execute_moments(block)
            if not result.ok:
                return result

        if not replies and has_silent and not moments:
            return ActionResult(ok=True)  # 沉默，无动作

        if not replies and not moments:
            return ActionResult(ok=False,
                                error="XML 包中无可执行的 <reply> 块",
                                retryable=False)

        # 逐块执行（最多 MAX_REPLY_BLOCKS 个 reply 块，防刷屏）
        for block in replies[:MAX_REPLY_BLOCKS]:
            result = self._execute_reply(session, block)
            if not result.ok:
                return result

        return ActionResult(ok=True)

    def _execute_moments(self, block_xml: str) -> ActionResult:
        """执行 <moments-post text="..."/>：发一条纯文字朋友圈。

        交互层内部动作：submit_bundle 已持屏幕互斥锁，这里直接调
        action/moments_poster.py 的真机发布流程（决策层不碰设备）。
        """
        try:
            root = ET.fromstring(block_xml)
        except ET.ParseError:
            cleaned = re.sub(r'&(?!lt;|amp;|gt;|quot;)', '&amp;', block_xml)
            try:
                root = ET.fromstring(cleaned)
            except ET.ParseError as e:
                log.warning("moments-post XML parse error: %s", e)
                return ActionResult(ok=False, error=f"XML 解析失败: {e}",
                                    retryable=False)
        text = (root.get("text") or root.text or "").strip()
        if not text:
            return ActionResult(ok=False, error="<moments-post> 缺 text 属性",
                                retryable=False)
        from ..ports.android.action.moments_poster import post_text_moments
        log.info("发布朋友圈: %r", text[:40])
        try:
            r = post_text_moments(self._tools, text)
        except Exception as e:
            log.exception("发布朋友圈异常")
            return ActionResult(ok=False, error=f"发布朋友圈异常: {e}",
                                retryable=True)
        if r.get("ok") and r.get("posted"):
            return ActionResult(ok=True)
        return ActionResult(ok=False,
                            error=f"发布朋友圈失败: {r.get('error')}",
                            retryable=True,
                            escalation_hint="朋友圈发布失败")

    def _execute_reply(self, session: str, block_xml: str) -> ActionResult:
        """执行单个 <reply> 块。"""
        try:
            root = ET.fromstring(block_xml)
        except ET.ParseError:
            # 放宽解析：裸 & 转义后再试（契约要求 LLM 转义，这里只是兜底）
            try:
                cleaned = re.sub(r'&(?!lt;|amp;|gt;|quot;)', '&amp;', block_xml)
                root = ET.fromstring(cleaned)
            except ET.ParseError as e:
                log.warning("XML parse error: %s", e)
                return ActionResult(ok=False, error=f"XML 解析失败: {e}",
                                    retryable=False)

        # <image>/<file>/<sticker> 三种媒体绝不可混用（契约）。
        # 每种媒体都可与 <text> 同块：先发文字、再发媒体（两条消息）。
        image_elem = root.find("image")
        file_elem = root.find("file")
        sticker_elem = root.find("sticker")
        media_n = sum(e is not None for e in (image_elem, file_elem, sticker_elem))
        if media_n > 1:
            return ActionResult(ok=False,
                                error="<image>/<file>/<sticker> 不可在同一 <reply> 混用",
                                retryable=False)

        # 先校验媒体参数（提前报错，避免发了文字才发现媒体参数非法）
        media_kind = media_path = None
        if image_elem is not None:
            img_path = (image_elem.get("path") or "").strip()
            if not img_path:
                return ActionResult(ok=False, error="<image> 缺 path 属性",
                                    retryable=False)
            media_kind, media_path = "image", img_path
        if file_elem is not None:
            file_path = (file_elem.get("path") or "").strip()
            if not file_path:
                return ActionResult(ok=False, error="<file> 缺 path 属性",
                                    retryable=False)
            media_kind, media_path = "file", file_path
        sticker_seq = None
        if sticker_elem is not None:
            query = (sticker_elem.get("query") or "").strip()
            seq = (sticker_elem.get("seq") or "").strip()
            if query:
                return ActionResult(ok=False,
                                    error="<sticker> 不允许 query 盲发："
                                          "请先 <tool name=\"emoji\" query=\"...\"/> "
                                          "搜索拿到 seq 再精确发送",
                                    retryable=False)
            if not seq:
                return ActionResult(ok=False, error="<sticker> 缺 seq 属性",
                                    retryable=False)
            sticker_seq = seq

        # <text> 列表：ET 解析已反转义，e.text 即最终文本
        texts = [(e.text or "") for e in root.findall("text")]
        texts = [t for t in texts if t.strip()]

        # <quote>：引用回复——第一条 <text> 走引用流程发出；
        # 引用失败（目标找不到/流程中断）降级为普通发送，不丢消息
        # （"每条 @ 必回"优先于"必须带引用"，2026-08-09 JY君 实测）
        quote_elem = root.find("quote")
        if quote_elem is not None:
            quote_match = (quote_elem.get("match") or
                           (quote_elem.text or "")).strip()
            if not quote_match:
                return ActionResult(ok=False, error="<quote> 缺 match 属性",
                                    retryable=False)
            if not texts:
                return ActionResult(ok=False,
                                    error="<quote> 需要至少一个 <text> 作为回复内容",
                                    retryable=False)
            r = self._do_quote_reply(session, quote_match, texts[0])
            if r.ok:
                texts = texts[1:]  # 第一条已带引用发出，其余照常逐条发
            else:
                log.warning("[%s] 引用失败（%s），降级普通发送", session, r.error)

        # 先发文字，再发媒体（image/file/sticker 都与 text 共存，先文后图）
        if texts:
            r = self._do_send_texts(session, texts)
            if not r.ok:
                return r

        if sticker_seq is not None:
            return self._do_send_sticker(session, sticker_seq)
        if media_kind is not None:
            return self._do_send_media(session, media_kind, media_path)

        # 没有 <text>/媒体：裸文本兜底（健壮性，契约外输入）
        if root.text and root.text.strip():
            return self._do_send_texts(session, [root.text])
        return ActionResult(ok=True)

    # ------------------------------------------------------------------ 动作实现
    def _enter_session(self, session: str):
        """进入会话；失败返回 ActionResult，成功返回 None。"""
        r = self._nav.enter_session(session)
        if not r.success:
            return ActionResult(ok=False,
                                error=f"进入会话失败: {r.error}",
                                retryable=True,
                                escalation_hint=f"无法进入会话 {session}")
        return None

    def _locate_direction(self, session: str, match_text: str):
        """用消息日志判断引用目标在屏幕的哪个方向。

        返回 "above"（向上翻找）/ None（应在当前屏底部区域）。
        入会话后停在底部，目标不在当前屏时几乎必然在上方；
        日志里找不到目标时也按 above 处理（先向上翻找）。"""
        if not self._session_reader:
            return "above"
        from ..msglog.message_log import normalize
        try:
            rows = self._session_reader.get_context(session, n=300)
        except Exception:  # noqa: BLE001
            return "above"
        needle = normalize(match_text)
        if not needle:
            return "above"
        target_idx = None
        for i, m in enumerate(rows):            # 取最后一次出现（最新）
            c = normalize(getattr(m, "content", ""))
            if c and (needle in c or c in needle):
                target_idx = i
        if target_idx is None:
            return "above"
        # 底部 6 条以内 = 应在当前屏（OCR 抖动可能没找到，值得重试）
        if target_idx >= len(rows) - 6:
            return None
        return "above"

    def _scroll_search(self, direction: str):
        """按方向滚一屏（随机化，走端口工具）。"""
        if direction == "above":
            self._tools.scroll_up()              # 看更早
        else:
            self._tools.scroll_down()            # 看更新

    def _do_send_texts(self, session: str, texts) -> ActionResult:
        """逐条发送文本：多个 <text> = 多条小消息依次发，条间随机延迟 1~3s。
        单条长文不拆——整条交给端口 sender（其内部标点分段是拟人节奏，不是拆句）。"""
        err = self._enter_session(session)
        if err is not None:
            return err
        try:
            for i, text in enumerate(texts):
                if i > 0:
                    self._sleep(self._rand(*TEXT_MSG_GAP))
                self._sender.send(session, text)
            return ActionResult(ok=True)
        except Exception as e:
            log.exception("[%s] send_text failed", session)
            return ActionResult(ok=False, error=str(e), retryable=True,
                                escalation_hint=f"发送文本到 {session} 失败")

    def _do_quote_reply(self, session: str, match_text: str,
                        reply_text: str) -> ActionResult:
        """引用回复：进会话 → 长按目标气泡 → 菜单找"引用" → 输入 → 发送。
        底层八步流程在 ports/android/action/quote_reply.py，逐步验证不盲点。"""
        from ..ports.android.action.quote_reply import quote_reply
        log.info("[%s] quote reply: match=%r", session, match_text[:40])
        err = self._enter_session(session)
        if err is not None:
            return err

        def _try():
            return quote_reply(self._tools.dev, match_text=match_text,
                               reply_text=reply_text, sleep_fn=self._sleep)

        def _pack(r):
            if r.get("ok"):
                if not r.get("verified"):
                    log.warning("[%s] quote reply 发出但轻验证未确认（假定已发出）",
                                session)
                return ActionResult(ok=True)
            retryable = r.get("step") not in ("args", "find_target")
            return ActionResult(
                ok=False,
                error=f"引用回复失败@{r.get('step')}: {r.get('error')}",
                retryable=retryable,
                escalation_hint=f"引用回复 {session} 失败: {r.get('error')}")

        try:
            r = _try()
        except Exception as e:
            log.exception("[%s] quote reply exception", session)
            return ActionResult(ok=False, error=f"引用回复异常: {e}",
                                retryable=True,
                                escalation_hint=f"引用回复 {session} 异常")
        if r.get("ok") or r.get("step") != "find_target":
            return _pack(r)

        # 目标不在当前屏：按消息日志判方向，滚动查找（最多 4 屏）
        direction = self._locate_direction(session, match_text)
        if direction is None:
            # 应在当前屏（底部区域）：OCR 抖动兜底重试一次，
            # 再向下滚一屏（处理期间可能有新消息到底部）
            log.info("[%s] quote target 应在当前屏，重试+向下补查", session)
            r = _try()
            if r.get("ok") or r.get("step") != "find_target":
                return _pack(r)
            self._scroll_search("below")
            self._sleep(self._rand(0.6, 1.2))
            r = _try()
            if r.get("ok") or r.get("step") != "find_target":
                return _pack(r)
            # 日志与屏幕错位（如有媒体消息没同步进日志）时"应在当前屏"
            # 会误判，继续向上翻找（2026-08-09 JY君 实测）
            log.info("[%s] 当前屏判定可能错位，改为向上翻找", session)
            direction = "above"

        log.info("[%s] quote target 在屏幕%s侧，滚动查找", session,
                 "上" if direction == "above" else "下")
        for i in range(4):
            self._scroll_search(direction)
            self._sleep(self._rand(0.6, 1.2))
            try:
                r = _try()
            except Exception as e:
                log.exception("[%s] quote reply exception", session)
                return ActionResult(ok=False, error=f"引用回复异常: {e}",
                                    retryable=True)
            if r.get("ok") or r.get("step") != "find_target":
                return _pack(r)
        return _pack(r)

    def _do_send_media(self, session: str, kind: str, path: str) -> ActionResult:
        """发图片/文件：进会话 → action/image_sender.py 的相册/加号面板流程。"""
        from ..ports.android.action import image_sender
        log.info("[%s] send %s: %s", session, kind, path)
        err = self._enter_session(session)
        if err is not None:
            return err
        fn = image_sender.send_image if kind == "image" else image_sender.send_file
        try:
            r = fn(self._tools.dev, path)
        except Exception as e:
            log.exception("[%s] send %s exception", session, kind)
            return ActionResult(ok=False, error=f"发{kind}异常: {e}",
                                retryable=True)
        if not r.get("ok"):
            retryable = r.get("step") not in ("args",)
            return ActionResult(
                ok=False,
                error=f"发{kind}失败@{r.get('step')}: {r.get('error')}",
                retryable=retryable,
                escalation_hint=f"发{kind}到 {session} 失败: {r.get('error')}")
        return ActionResult(ok=True)

    def _emoji_index(self):
        """懒加载表情检索器（测试可注入自定义实例）。"""
        if self._emoji_index_inst is None:
            from ...shared.emoji_index import EmojiIndex
            self._emoji_index_inst = EmojiIndex()
        return self._emoji_index_inst

    def _do_send_sticker(self, session: str, seq: str) -> ActionResult:
        """发表情包：按 seq 精确取图 → 复用相册流程发送（gif 自动成动图表情）。

        只接受 seq（精确发送）；query 盲发已在 _execute_reply 里禁止。
        """
        from ..ports.android.action import image_sender
        index = self._emoji_index()
        try:
            picked = index.get(int(seq))
        except (TypeError, ValueError):
            return ActionResult(ok=False,
                                error=f"<sticker> seq 必须是数字: {seq!r}",
                                retryable=False)
        if not picked:
            return ActionResult(ok=False,
                                error=f"表情库中找不到 seq={seq} 的表情",
                                retryable=False)
        log.info("[%s] send sticker seq=%s (%s): %s", session,
                 picked["seq"],
                 picked.get("text_content") or picked.get("mood"),
                 picked["path"])
        err = self._enter_session(session)
        if err is not None:
            return err
        try:
            r = image_sender.send_image(self._tools.dev, picked["path"])
        except Exception as e:
            log.exception("[%s] send sticker exception", session)
            return ActionResult(ok=False, error=f"发表情异常: {e}",
                                retryable=True)
        if not r.get("ok"):
            retryable = r.get("step") not in ("args",)
            return ActionResult(
                ok=False,
                error=f"发表情失败@{r.get('step')}: {r.get('error')}",
                retryable=retryable,
                escalation_hint=f"发表情到 {session} 失败: {r.get('error')}")
        return ActionResult(ok=True)
