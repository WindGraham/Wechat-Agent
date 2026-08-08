# -*- coding: utf-8 -*-
"""wechat_tools.py — Phase 3 工具层：把微信封装成 6 个语义化工具供 LLM agent 调用。

每个工具 = 动作 + 状态查询，返回 ToolResult（含页面文字描述 + 可用动作）。
仅适配 OnePlus 6T (1080x2340, 深色模式) + 微信 8.0.76。

依赖：src/device_ctl.py（DeviceCtl）、src/v2/state_builder.py（V2 感知层）。
硬性约束：截图即读即删（capture_bytes 内存截图 + 内存解析，全程不落盘）。
"""

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
            self.dev.tap_rect(layout.CHAT_INPUT_BAR_FOCUSED)
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
        self.dev.tap_rect(layout.CHAT_INPUT_BAR)
        self.dev.wait_random(400, 800)
        self.dev.clear_text()
        self.dev.wait_random(200, 400)

        self.dev.input_text(text)
        self.dev.wait_random(400, 800)

        if not self._send_btn_visible():
            # 文本仍没上屏（罕见：语音模式切换未生效/输入连接未建立）：
            # 重进一次文本模式 + 补一次广播
            self.dev.tap_rect(layout.CHAT_VOICE)
            self.dev.wait_random(600, 1000)
            self.dev.tap_rect(layout.CHAT_INPUT_BAR)
            self.dev.wait_random(400, 700)
            self.dev.input_text(text)
            self.dev.wait_random(500, 900)
        if not self._send_btn_visible():
            return self._result(self._snap(), success=False,
                                error="发送按钮未出现（输入未上屏？）")
        self.dev.tap_rect(layout.SEND_BTN)
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

    def _send_btn_visible(self):
        """发送按钮校验：绿掩膜扫描区内有绿色块、且 SEND_BTN 本体区域内绿色
        占比达标（输入有字时 "+" 圆钮变为绿色"发送"按钮）。内存截图不落盘。"""
        import cv2
        img = self.dev.capture_bytes()
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        green = (h >= 60) & (h <= 90) & (s > 90) & (v > 80)
        z = layout.SEND_SCAN_ZONE
        zone_ratio = float(green[z.y:z.y + z.h, z.x:z.x + z.w].mean())
        b = layout.SEND_BTN
        btn_ratio = float(green[b.y:b.y + b.h, b.x:b.x + b.w].mean())
        log.info("send btn green ratio: zone=%.3f btn=%.3f", zone_ratio, btn_ratio)
        return zone_ratio > 0.02 and btn_ratio > 0.15

    def _full_ocr(self):
        """内存截图 -> 全图 OCR，返回 ocr_items（不落盘）。"""
        return _v2_run_ocr(self.dev.capture_bytes())


# --------------------------------------------------------------------- 自测
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    tools = WeChatTools()

    def show(tag, r, max_lines=14):
        print(f"\n{'=' * 60}\n{tag}\n{r.summary()}")
        lines = r.description.splitlines()
        for ln in lines[:max_lines]:
            print("  " + ln)
        if len(lines) > max_lines:
            print(f"  ... ({len(lines) - max_lines} more lines)")

    # 1. open_wechat -> 首页 7 个会话
    r1 = tools.open_wechat()
    show("STEP 1: open_wechat", r1)
    sessions = [ln for ln in r1.description.splitlines()
                if ln and ln[0].isdigit() and ". " in ln]
    print(f"  >> 会话数: {len(sessions)}")
    assert r1.success and r1.page == "wechat_home", "step1 failed"

    # 2. enter_session("风图")
    r2 = tools.enter_session("风图")
    show("STEP 2: enter_session(风图)", r2)
    assert r2.success and r2.page == "wechat_chat", "step2 failed"

    # 3. scroll_up -> 更早消息
    r3 = tools.scroll_up()
    show("STEP 3: scroll_up", r3)
    changed = r3.description != r2.description
    print(f"  >> 内容变化: {changed}")

    # 4. scroll_down -> 回底部
    r4 = tools.scroll_down()
    show("STEP 4: scroll_down", r4)

    # 5. send_text 测试消息（仅风图，仅此 1 条）
    r5 = tools.send_text("自动回复功能测试，请忽略")
    show("STEP 5: send_text", r5)
    assert r5.success, "step5 failed: " + str(r5.error)

    # 6. back -> 首页
    r6 = tools.back()
    show("STEP 6: back", r6)
    assert r6.page == "wechat_home", f"step6: not home, got {r6.page}"

    # 7. enter_session("特高课") 群聊 member_count=6
    r7 = tools.enter_session("特高课")
    show("STEP 7: enter_session(特高课)", r7)
    assert r7.success, "step7 failed"

    # 8. back -> 长群名会话（列表第 5 个）
    r8 = tools.back()
    show("STEP 8a: back", r8, max_lines=6)
    r9 = tools.enter_session("怨憎会 爱别离 要风得风要雨得雨")
    show("STEP 8b: enter_session(长群名)", r9)
    assert r9.success, "step8 failed"

    # 9. 搜索 fallback：enter_session("Leisure")（列表没有，可能搜不到）
    r10 = tools.back()
    r11 = tools.enter_session("Leisure")
    show("STEP 9: enter_session(Leisure) 搜索fallback", r11)
    print(f"  >> 搜索 fallback 结果: success={r11.success} error={r11.error}")
    if r11.success:
        tools.back()   # 真进去了就退出来，不发任何消息

    # 清理残留截图
    leftover = [f for f in os.listdir("/tmp") if f.startswith("wx_cap_")]
    for f in leftover:
        os.remove(os.path.join("/tmp", f))
    print(f"\nALL DONE. leftover screenshots cleaned: {len(leftover)}")
