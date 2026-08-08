# -*- coding: utf-8 -*-
"""wechat_tools.py — 端口工具层：把微信封装成语义化工具（发送/导航/滚动/读屏）。

每个工具 = 动作 + 状态查询，返回 ToolResult（含页面文字描述 + 可用动作）。
仅适配 OnePlus 6T (1080x2340, 深色模式) + 微信 8.0.76。

依赖：..device.device_ctl（DeviceCtl）、..perception.state_builder /
..perception.ocr_engine（V2 感知层）。
硬性约束：截图即读即删（capture_bytes 内存截图 + 内存解析，全程不落盘）。
"""

# 本模块直接连接真实手机：以脚本方式直接运行没有任何安全的自测可做
# （旧自测脚本会真实发消息，已移除）。闸门放在 import 之前——直接运行时
# 包相对 import 必然失败，必须先拦住并给出说明。
if __name__ == "__main__":
    print("wechat_tools.py 不提供自测入口：直接运行会真实操作手机微信，已禁用。"
          "请用离线单测（假 dev）验证逻辑。")
    raise SystemExit(1)

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from ..device.device_ctl import DeviceCtl
from ..perception.state_builder import build_state as _v2_build_state
from ..perception.ocr_engine import run_ocr as _v2_run_ocr
from ..device import layout

log = logging.getLogger("wechat_tools")

# ------------------------------------------------------------------ 坐标常量
# 聊天页输入栏/发送按钮坐标全部走 layout.py 两态常量（未聚焦/聚焦），
# 不再保留裸坐标：IME 锁定 ADBKeyBoard 后点击输入框不弹键盘，
# 聚焦后 "ADB Keyboard {ON}" 细条把输入栏顶起 ~115px（layout.py 头部注释）。
# 触屏动作只准 tap_rect/swipe_zone（layout.py 第 8 行约定），裸坐标 tap 视为违规。

MAX_LIST_SCROLLS = 5            # enter_session 列表最多下翻屏数
CONF_THRESHOLD = 0.5            # OCR 平均置信度低于此值重截图一次


# ------------------------------------------------------------------ ToolResult
@dataclass
class ToolResult:
    success: bool
    page: str = ""
    title: str = ""
    description: str = ""
    available_actions: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def summary(self):
        head = f"[{'OK' if self.success else 'FAIL'}] {self.page} | {self.title}"
        if self.error:
            head += f" | error={self.error}"
        return head


# 名字匹配逻辑已下沉到 shared/name_match.py（scanner/sender 共用），
# 此处 re-export 保持向后兼容
from .....shared.name_match import (  # noqa: E402,F401
    _norm, _fold, _elide_match, _name_match)


# ------------------------------------------------------------------ 主类
class WeChatTools:
    def __init__(self):
        self.dev = DeviceCtl()
        # 每个会话见过的消息集合 {(sender, content)}，用于标 "(新)"
        self._seen_msgs = {}

    # ---------------------------------------------------------- 截图 + 解析
    def _snap(self, retry_low_conf=True):
        """截图 -> 解析 -> 删除截图。unknown / 低置信度重截一次。返回 state dict。"""
        state = self._snap_once()
        ptype = state.get("page", {}).get("type", "")
        conf = state.get("meta", {}).get("confidence", 0.0)
        if retry_low_conf and (ptype == "wechat_unknown" or conf < CONF_THRESHOLD):
            log.info("low quality parse (type=%s conf=%.3f), recapture", ptype, conf)
            self.dev.wait_random(400, 800)
            state2 = self._snap_once()
            conf2 = state2.get("meta", {}).get("confidence", 0.0)
            # 取更好的那份
            if state2.get("page", {}).get("type") != "wechat_unknown" or conf2 > conf:
                state = state2
        return state

    def _snap_once(self):
        """内存截图 -> V2 感知层解析。全程不落盘，无需清理临时文件。"""
        return _v2_build_state(self.dev.capture_bytes())

    def parse_state(self, img):
        """解析已截好的帧（frame_bus 复用同一帧，避免二次截图）。"""
        return _v2_build_state(img)

    # ---------------------------------------------------------- 描述生成
    def _describe(self, state, mark_new=False):
        """state dict -> (description, available_actions[str])。"""
        page = state.get("page", {})
        ptype = page.get("type", "")
        title = page.get("title", "")
        elements = state.get("elements", [])

        if ptype == "wechat_home":
            lines = ["当前页面：微信首页", "底部导航：微信 | 通讯录 | 发现 | 我", "",
                     "会话列表："]
            for i, e in enumerate(elements, 1):
                if e.get("type") != "session_item":
                    continue
                seg = f"{i}. {e.get('label', '?')}"
                unread = e.get("unread_count", 0)
                if e.get("mention_me"):
                    seg += "[有人@我]"
                if unread > 0:
                    seg += f"（{unread}条未读）"
                elif unread == -1:
                    seg += "（有未读）"
                preview = e.get("last_message", "")
                tm = e.get("last_message_time", "")
                if preview:
                    seg += f"：{preview}"
                if tm:
                    seg += f" {tm}"
                lines.append(seg)
            desc = "\n".join(lines)
        elif ptype == "wechat_chat":
            kind = f"群聊，{page['member_count']}人" if page.get("member_count") else "私聊"
            lines = [f"当前页面：与{title}的聊天", f"聊天对象：{title}（{kind}）", "",
                     "消息列表（从旧到新）："]
            seen = self._seen_msgs.setdefault(title, set())
            first_visit = not seen
            for e in elements:
                if e.get("type") == "time_divider":
                    continue
                if e.get("type") == "message_bubble":
                    sender = "我" if e.get("is_mine") else e.get("sender", "?")
                    content = (e.get("content") or "").replace("\n", " / ")
                    tm = e.get("time_hint") or "--:--"
                    key = (sender, content)
                    new_mark = ""
                    if mark_new and not first_visit and key not in seen:
                        new_mark = "（新）"
                    seen.add(key)
                    lines.append(f"[{tm}] {sender}：{content}{new_mark}")
                elif e.get("type") == "pinned_message":
                    lines.append(f"[置顶] {e.get('content', '')}")
                elif e.get("type") == "system_message":
                    lines.append(f"[系统] {e.get('content', '')}")
            if len(lines) == 4:
                lines.append("（当前屏幕无消息）")
            desc = "\n".join(lines)
        else:
            raw = ""
            for e in elements:
                if e.get("type") == "raw_ocr_text":
                    raw = e.get("content", "")
                    break
            desc = f"当前页面：{title or '未知'}（{ptype}）\n原始OCR文本：\n{raw}"

        actions = []
        for a in state.get("available_actions", []):
            act = a.get("action", "")
            tgt = a.get("target") or a.get("text") or a.get("path")
            actions.append(f'{act}("{tgt}")' if tgt else f"{act}()")
        return desc, actions

    def _result(self, state, success=True, error=None, mark_new=False):
        page = state.get("page", {})
        desc, actions = self._describe(state, mark_new=mark_new)
        return ToolResult(success=success, page=page.get("type", ""),
                          title=page.get("title", ""), description=desc,
                          available_actions=actions, error=error)

    def _fail(self, error, state=None):
        log.warning("tool failed: %s", error)
        if state is None:
            try:
                state = self._snap()
            except Exception:
                state = {"page": {"type": "", "title": ""},
                         "elements": [], "available_actions": []}
        return self._result(state, success=False, error=error)

    # ---------------------------------------------------------- 工具 1
    def open_wechat(self):
        """打开微信（已在前台则直接用），返回当前页面描述。"""
        log.info("tool call: open_wechat()")
        try:
            act = self.dev.get_current_activity()
            if not act.startswith("com.tencent.mm"):
                self.dev.open_wechat()
                self.dev.wait_random(800, 1500)
            state = self._snap()
        except Exception as e:
            return self._fail(f"打开微信失败: {e}")
        r = self._result(state)
        log.info("open_wechat -> %s", r.summary())
        return r

    # ---------------------------------------------------------- 工具 2
    def enter_session(self, name):
        """进入指定会话：列表当前屏 -> 下翻最多 5 屏 -> 搜索 fallback。"""
        log.info("tool call: enter_session(%r)", name)
        try:
            return self._enter_session(name)
        except Exception as e:
            log.exception("enter_session exception")
            return self._fail(f"进入会话异常: {e}")

    def _enter_session(self, name):
        state = self._snap()
        # 已在目标会话里
        if state["page"]["type"] == "wechat_chat" and \
                _name_match(state["page"]["title"], name):
            return self._result(state, mark_new=True)

        # 回到首页
        for _ in range(3):
            if state["page"]["type"] == "wechat_home":
                break
            self.dev.back()
            self.dev.wait_random(600, 1000)
            state = self._snap()
        if state["page"]["type"] != "wechat_home":
            return self._fail("无法回到微信首页", state)

        # 列表查找（当前屏 + 下翻最多 MAX_LIST_SCROLLS 屏）
        seen_labels = set()
        for scroll_i in range(MAX_LIST_SCROLLS + 1):
            hit = self._find_session_item(state, name)
            if hit:
                pos = hit["position"]
                log.info("found %r in list (scroll %d), tap", name, scroll_i)
                self.dev.tap_rect(layout.Rect(pos["x"], pos["y"],
                                              pos["w"], pos["h"]))
                self.dev.wait_random(800, 1500)
                return self._verify_entered(name)
            labels = tuple(e.get("label", "") for e in state.get("elements", [])
                           if e.get("type") == "session_item")
            if scroll_i == MAX_LIST_SCROLLS or labels and labels[-3:] and \
                    set(labels).issubset(seen_labels):
                break
            seen_labels.update(labels)
            self.dev.swipe_zone(layout.HOME_LIST_ZONE, direction="up",
                                length_ratio=(0.55, 0.75))   # 列表下翻
            self.dev.wait_random(600, 1000)
            state = self._snap()

        # fallback：搜索
        log.info("%r not in list, fallback to search", name)
        return self._search_session(name)

    @staticmethod
    def _find_session_item(state, name):
        best = None
        for e in state.get("elements", []):
            if e.get("type") != "session_item" or e.get("partial"):
                continue            # 残缺条目（屏幕边缘截断）不作进入候选
            if _norm(e.get("label")) == _norm(name):
                return e            # 精确匹配优先
            if best is None and _name_match(e.get("label"), name):
                best = e
        return best

    def _search_session(self, name):
        self.dev.tap_rect(layout.HOME_SEARCH)
        self.dev.wait_random(800, 1500)
        self.dev.tap_rect(layout.SEARCH_INPUT)
        self.dev.wait_random(400, 800)
        self.dev.input_text(name)
        self.dev.wait_random(800, 1500)

        ocr_items = self._full_ocr()

        # 搜索结果行：搜索框（顶部）之下、包含目标名的文本，取最靠上的一个
        nn = _fold(_norm(name))
        hits = [it for it in ocr_items
                if it["cy"] > 280 and nn and nn in _fold(_norm(it["text"]))]
        hits.sort(key=lambda it: it["cy"])
        if not hits:
            log.warning("search found no result for %r", name)
            self.dev.back()          # 退回首页
            self.dev.wait_random(600, 1000)
            return self._fail(f"找不到会话：{name}（列表和搜索均无）")
        hit = hits[0]
        log.info("search hit: %r at (%.0f,%.0f)", hit["text"], hit["cx"], hit["cy"])
        self.dev.tap_rect(layout.Rect(int(hit["cx"]) - 60, int(hit["cy"]) - 40,
                                      120, 80))
        self.dev.wait_random(800, 1500)
        return self._verify_entered(name)

    def _verify_entered(self, name):
        """验证已进入目标聊天页，失败重试 1 次（重新 tap 暂无，重截图即可）。"""
        for attempt in range(2):
            state = self._snap()
            ptype, title = state["page"]["type"], state["page"]["title"]
            if ptype == "wechat_chat" and _name_match(title, name):
                r = self._result(state, mark_new=True)
                log.info("enter_session(%r) -> %s", name, r.summary())
                return r
            log.info("verify entered failed (attempt %d): type=%s title=%r",
                     attempt, ptype, title)
            self.dev.wait_random(600, 1000)
        return self._fail(f"未能进入会话：{name}（当前页面 {ptype}:{title}）", state)

    # ---------------------------------------------------------- 工具 3
    def back(self):
        """返回上一页。输入框聚焦态下第一次 back 只取消聚焦（收起 IME 细条，
        页面不退出，2026-08-04 真机实测），此时页面未变化则补按一次。"""
        log.info("tool call: back()")
        try:
            before = self.dev.get_current_activity()
            before_state = self._snap()
            before_page = (before_state["page"].get("type"),
                           before_state["page"].get("title"))
            self.dev.back()
            self.dev.wait_random(800, 1500)
            state = self._snap()
            cur_page = (state["page"].get("type"), state["page"].get("title"))
            if cur_page == before_page and before_page[0] == "wechat_chat":
                log.info("back only unfocused input bar, press back again")
                self.dev.back()
                self.dev.wait_random(800, 1500)
                state = self._snap()
            r = self._result(state)
        except Exception as e:
            return self._fail(f"back 失败: {e}")
        log.info("back -> %s (was %s)", r.summary(), before)
        return r

    # ---------------------------------------------------------- 工具 4/5
    def scroll_up(self):
        """聊天页看更早消息 / 列表看更靠上的条目：手指从上往下滑。"""
        log.info("tool call: scroll_up()")
        try:
            self.dev.swipe_zone(layout.CHAT_SCROLL_ZONE, direction="down",
                                length_ratio=(0.6, 0.8))
            self.dev.wait_random(400, 900)
            state = self._snap()
            r = self._result(state, mark_new=state["page"]["type"] == "wechat_chat")
        except Exception as e:
            return self._fail(f"scroll_up 失败: {e}")
        log.info("scroll_up -> %s", r.summary())
        return r

    def scroll_down(self):
        """聊天页看更新消息 / 列表下翻：手指从下往上滑。"""
        log.info("tool call: scroll_down()")
        try:
            self.dev.swipe_zone(layout.CHAT_SCROLL_ZONE, direction="up",
                                length_ratio=(0.6, 0.8))
            self.dev.wait_random(400, 900)
            state = self._snap()
            r = self._result(state, mark_new=state["page"]["type"] == "wechat_chat")
        except Exception as e:
            return self._fail(f"scroll_down 失败: {e}")
        log.info("scroll_down -> %s", r.summary())
        return r

    # ---------------------------------------------------------- 工具 6
    def send_text(self, text):
        """当前聊天页发送文本。失败重试 1 次，仍失败清理输入框残留。"""
        log.info("tool call: send_text(%r)", text)
        try:
            state = self._snap()
            if state["page"]["type"] != "wechat_chat":
                return self._fail("当前不在聊天页，无法发送", state)

            for attempt in range(2):
                r = self._send_once(text)
                if r.success:
                    log.info("send_text -> %s", r.summary())
                    return r
                log.warning("send_text attempt %d failed: %s", attempt, r.error)
                self.dev.wait_random(500, 900)
            # 清理输入框残留（可能仍处于聚焦态，输入栏在聚焦坐标）
            self.dev.tap_rect(self._input_bar_rect())
            self.dev.wait_random(300, 600)
            self.dev.clear_text()
            self.dev.wait_random(400, 800)
            return self._fail(f"发送失败（重试后仍未发出）：{r.error}")
        except Exception as e:
            log.exception("send_text exception")
            return self._fail(f"发送异常: {e}")

    def _send_once(self, text):
        """聚焦态两态发送流程（IME 锁定 ADBKeyBoard 后，layout.py 常量）：
        语音模式检测并切回文本 -> 点未聚焦态输入栏聚焦 -> ADB_CLEAR_TEXT 清空
        -> jieba 分块广播输入 -> 绿掩膜+位置校验发送按钮 -> tap_rect(SEND_BTN)
        -> 气泡 OCR 验证。全程无键盘弹起，发送按钮固定在聚焦态坐标。
        2026-08-04 修复：微信重启/手动使用后输入栏可能停在语音模式（"按住说话"），
        没有文本输入框，广播必然不上屏——发送前先切回文本模式。"""
        if self._in_voice_mode():
            log.info("输入栏处于语音模式，切回文本模式")
            self.dev.tap_rect(layout.CHAT_VOICE)
            self.dev.wait_random(800, 1200)
        # 上轮引用发送失败可能留下引用预览条：盖住输入框固定点按区（点不中
        # 输入框），且不清掉本条普通发送会被当成引用发出（2026-08-08 实测）
        from .quote_reply import dismiss_quote_preview
        dismiss_quote_preview(self.dev)
        self.dev.tap_rect(self._input_bar_rect())
        self.dev.wait_random(400, 800)
        self.dev.clear_text()
        self.dev.wait_random(200, 400)

        self.dev.input_text(text)
        self.dev.wait_random(400, 800)

        btn = self._find_send_btn()
        if not btn:
            # 文本仍没上屏（罕见：语音模式切换未生效/输入连接未建立）：
            # 重进一次文本模式 + 补一次广播
            self.dev.tap_rect(layout.CHAT_VOICE)
            self.dev.wait_random(600, 1000)
            self.dev.tap_rect(self._input_bar_rect())
            self.dev.wait_random(400, 700)
            self.dev.input_text(text)
            self.dev.wait_random(500, 900)
            btn = self._find_send_btn()
        if not btn:
            return self._result(self._snap(), success=False,
                                error="发送按钮未出现（输入未上屏？）")
        # 动态定位点按（引用预览条/聚焦态会顶起按钮，固定坐标会按空）
        self.dev.tap_rect(layout.Rect(btn[0] - 60, btn[1] - 40, 120, 80))
        self.dev.wait_random(600, 1200)

        # 验证：最后一条消息 is_mine 且内容匹配。气泡渲染/OCR 有延迟，
        # 最多再补 2 次快照确认；仍确认不到时**不判失败**——绿色发送按钮已点下，
        # 消息几乎必然已发出，判失败会让上层重发同一条造成重复刷屏（2026-08-04 实测）。
        state = self._verify_sent(text)
        if state["page"]["type"] != "wechat_chat":
            return self._result(state, success=False, error="发送后页面不在聊天页")
        return self._result(state, mark_new=True)

    def _verify_sent(self, text):
        """点发送后确认自己气泡已上屏：最多 3 次快照（每次间隔 ~1s）。
        返回最后一份 state；找不到也不报失败（假定已发出，防重复发送）。"""
        state = self._snap()
        t = _norm(text)
        for _ in range(3):
            if state["page"]["type"] != "wechat_chat":
                return state
            for e in reversed(state.get("elements", [])):
                if e.get("type") == "message_bubble" and e.get("is_mine"):
                    c = _norm(e.get("content"))
                    if t and (t in c or c in t or (len(t) >= 4 and t[:4] == c[:4])):
                        return state
                    break
            self.dev.wait_random(900, 1400)
            state = self._snap()
        log.warning("send 已点发送但验证未确认到气泡（假定已发出，不重发）")
        return state

    def chat_is_group(self):
        """当前聊天页是否群聊：标题栏原始 OCR 里找 "(人数)" 后缀。
        群聊标题必带、私聊没有；state 里的 page.title 被规整过会丢掉
        人数后缀，所以这里自己裁标题带跑一次 OCR（不过滤字高）。
        返回 True/False；读不到标题文字返回 None（调用方自行兜底）。"""
        import re as _re
        from ..perception import layout_consts as LC
        try:
            img = self.dev.capture_bytes()
            crop = img[LC.TITLE_Y0 - 20:LC.TITLE_Y1 + 20, 150:950]
            items = _v2_run_ocr(crop)
        except Exception:
            log.exception("chat_is_group ocr failed")
            return None
        text = "".join((it.get("text") or "") for it in items)
        if not text.strip():
            return None
        return bool(_re.search(r"[（(]\s*\d+\s*[)）]", text))

    def _input_bar_rect(self):
        """智能定位输入框点按区（类似发送键的动态定位，不认固定坐标）：
        OCR 底部 "ADB Keyboard {ON}" 细条（y>=2100）出现 = 聚焦态，
        输入栏被顶起 ~115px，点聚焦坐标；否则点未聚焦坐标。
        上轮发送失败把输入栏留在聚焦态时，死点未聚焦坐标会按空
        （2026-08-08 用户实测"点不中输入框"）。OCR 异常兜底未聚焦坐标。"""
        try:
            for it in _v2_run_ocr(self.dev.capture_bytes()):
                if "ADB Keyboard" in (it.get("text") or "") \
                        and it.get("cy", 0) >= 2100:
                    return layout.CHAT_INPUT_BAR_FOCUSED
        except Exception:
            log.exception("input bar locate failed, fallback 未聚焦坐标")
        return layout.CHAT_INPUT_BAR

    def _in_voice_mode(self):
        """输入栏是否语音模式：语音态中间显示"按住说话"，无文本输入框，
        ADBKeyBoard 广播无处落。OCR 输入栏条带区域判断。"""
        try:
            import cv2
            img = self.dev.capture_bytes()
            y0 = min(2120, img.shape[0] - 220)
            crop = img[y0:y0 + 160, 80:1000]
            items = _v2_run_ocr(crop)
            return any("按住说话" in it["text"] for it in items)
        except Exception:
            return False

    def _find_send_btn(self):
        """动态定位绿色发送按钮中心 (cx, cy)；找不到返回 None。
        认颜色/文字不认坐标：引用预览条、聚焦态、面板态都会顶起按钮。"""
        from .quote_reply import _find_send_btn as _find
        from ..perception.ocr_engine import run_ocr
        img = self.dev.capture_bytes()
        return _find(img, run_ocr(img))

    def _send_btn_visible(self):
        """发送按钮存在性（动态定位版，保留旧接口名）。"""
        return self._find_send_btn() is not None

    def _full_ocr(self):
        """内存截图 -> 全图 OCR，返回 ocr_items（不落盘）。"""
        return _v2_run_ocr(self.dev.capture_bytes())

