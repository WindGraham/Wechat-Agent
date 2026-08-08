# -*- coding: utf-8 -*-
"""navigator.py — UI 导航：从 wechat_tools 提取的薄封装（action 层）。

只做导航动作，不做截图解析（那是 perception 的职责）：
所有 ADB 操作委托给注入的 WeChatTools 实例（open_wechat / enter_session /
back / scroll_up / scroll_down / send_text / dev / _snap）。
页面判断只依赖 _snap() 返回 state 里的 page.type（如 "wechat_home"）。
"""

import logging

log = logging.getLogger("action.navigator")


class Navigator:
    """WeChatTools 的 UI 导航薄封装，返回 ToolResult 或 bool。"""

    def __init__(self, tools):
        self.tools = tools

    # ---------------------------------------------------------- 直接转发
    def open_wechat(self) -> "ToolResult":
        """打开微信（已在前台则直接用）。"""
        return self.tools.open_wechat()

    def enter_session(self, name) -> "ToolResult":
        """进入指定会话（列表查找 + 搜索 fallback）。"""
        return self.tools.enter_session(name)

    def back(self) -> "ToolResult":
        """返回上一页。"""
        return self.tools.back()

    def scroll_up(self) -> "ToolResult":
        """聊天页看更早消息 / 列表看更靠上。"""
        return self.tools.scroll_up()

    def scroll_down(self) -> "ToolResult":
        """聊天页看更新消息 / 列表下翻。"""
        return self.tools.scroll_down()

    def send_text(self, text) -> "ToolResult":
        """当前聊天页发送文本（ADBKeyBoard 广播输入，含随机化）。"""
        return self.tools.send_text(text)

    # ---------------------------------------------------------- 页面状态
    def is_on_page(self, page_type) -> bool:
        """当前是否在某类页面（如 "wechat_home" / "wechat_chat"）。"""
        try:
            state = self.tools._snap()
        except Exception as e:
            log.warning("is_on_page snap failed: %s", e)
            return False
        return state.get("page", {}).get("type") == page_type

    # ---------------------------------------------------------- 回首页
    def back_to_home(self) -> bool:
        """尽量回到微信首页：最多 3 次 back，仍失败则 open_wechat 兜底。"""
        for _ in range(3):
            try:
                state = self.tools._snap()
            except Exception as e:
                log.warning("back_to_home snap failed: %s", e)
                break
            if state.get("page", {}).get("type") == "wechat_home":
                return True
            self.tools.back()
        # 3 次 back 没回首页：兜底直接打开微信并确认页面
        try:
            r = self.tools.open_wechat()
            return bool(r.success) and r.page == "wechat_home"
        except Exception as e:
            log.warning("back_to_home open_wechat fallback failed: %s", e)
            return False

    def ensure_home(self) -> bool:
        """先 open_wechat 再回首页（等价于 back_to_home 但以打开微信开头）。"""
        try:
            self.tools.open_wechat()
        except Exception as e:
            log.warning("ensure_home open_wechat failed: %s", e)
        return self.back_to_home()
