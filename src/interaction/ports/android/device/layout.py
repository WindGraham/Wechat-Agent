# -*- coding: utf-8 -*-
"""layout.py — 组件 Rect 常量注册表（OnePlus 6T 1080x2340 + 微信 8.0.76 深色模式专用）。

IME 已锁定为 ADBKeyBoard（无键盘弹起态），底部栏坐标为真常量，不再有键盘挤压漂移。
数值来源：src/v2/layout_consts.py（样本标定）+ docs/V2_RESEARCH_NOTES.md 方向4 §5 初值，
经真机截图核对修正（2026-08-04）。

工具层只准出现 tap_rect(layout.XXX) / swipe_zone(layout.XXX_ZONE, ...)，
任何裸坐标 tap(x, y) 视为违规。
"""

from .random_touch import Rect

# ------------------------------------------------------------------ 聊天页
# 输入栏有两态（2026-08-04 真机实测）：未聚焦在底部 y2140~2230；聚焦后底部出现
# "ADB Keyboard {ON}" 细条（y2130~2210），整条输入栏被顶起 ~115px。
CHAT_INPUT_BAR = Rect(132, 2140, 708, 90)     # 输入框·未聚焦（点它聚焦）
CHAT_INPUT_BAR_FOCUSED = Rect(132, 2015, 708, 105)  # 输入框·聚焦后（细条上方）
IME_STRIP = Rect(0, 2130, 1080, 100)          # "ADB Keyboard {ON}" 细条（识别层需忽略）
SEND_BTN = Rect(917, 2033, 135, 86)           # 绿色"发送"按钮（聚焦有字时，真机实测）
SEND_SCAN_ZONE = Rect(860, 2000, 220, 140)    # 发送按钮绿掩膜扫描区（聚焦态）
CHAT_SCROLL_ZONE = Rect(0, 300, 1080, 1650)   # 聊天页滑动区（避标题栏/输入栏，兼容聚焦态）

CHAT_BACK = Rect(3, 95, 110, 110)             # 返回箭头（中心 58,150）
CHAT_MORE = Rect(950, 95, 110, 110)           # 更多 "..."
CHAT_VOICE = Rect(11, 2130, 110, 110)         # 语音切换·未聚焦（中心 66,2185）
CHAT_EMOJI = Rect(850, 2130, 110, 110)        # 表情·未聚焦（中心 905,2185）
CHAT_PLUS = Rect(960, 2130, 110, 110)         # 扩展加号·未聚焦（中心 1015,2185）

# ------------------------------------------------------------------ 首页
HOME_LIST_ZONE = Rect(0, 250, 1080, 1850)     # 首页会话列表滑动区（y250~2100）
HOME_SEARCH = Rect(835, 93, 110, 110)         # 搜索图标（中心 890,148）
HOME_PLUS = Rect(957, 93, 110, 110)           # 加号圆圈（中心 1012,148）

# 底部四 Tab（图标行+文字行整体），中心 x = 135/405/675/945
TAB_WECHAT = Rect(75, 2130, 120, 150)
TAB_CONTACTS = Rect(345, 2130, 120, 150)
TAB_DISCOVER = Rect(615, 2130, 120, 150)
TAB_ME = Rect(885, 2130, 120, 150)

# 首页会话条目：ITEM_H=194（v2 标定），首条目顶 y=208，头像列 x30~190
HOME_ITEM_Y0 = 208
HOME_ITEM_H = 194

# ------------------------------------------------------------------ 搜索页
SEARCH_INPUT = Rect(120, 100, 840, 100)   # 搜索页输入框（中心 450,150）


def home_item_rect(index, avatar_only=False):
    """首页第 index 条会话（0 起）的 Rect；avatar_only=True 只含头像列。"""
    y = HOME_ITEM_Y0 + HOME_ITEM_H * index
    if avatar_only:
        return Rect(30, y, 160, HOME_ITEM_H)
    return Rect(0, y, 1080, HOME_ITEM_H)
