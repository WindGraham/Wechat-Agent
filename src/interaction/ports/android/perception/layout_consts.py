#!/usr/bin/env python3
"""layout_consts.py - v2 识别层全部标定常量（唯一事实源）

仅适配 OnePlus 6T (1080x2340, density 450, 深色模式) + 微信 8.0.76。
所有值由 tools/calibrate_layout.py 对 /tmp/wx_samples2 样本实测产出/回归校验。
标定日期：2026-08-04。改动本文件后请重跑 calibrate_layout.py 确认回归通过。
"""

# ---------------------------------------------------------------- 屏幕分区
SCREEN_W, SCREEN_H = 1080, 2340

STATUS_BAR_BOTTOM = 90          # 状态栏（忽略）
TITLE_Y0, TITLE_Y1 = 110, 195   # 标题栏文字区
CONTENT_Y0 = 200                # 内容区顶（聊天页置顶条也在其内）
CONTENT_Y1 = 2090               # 内容区底
INPUT_BAR_Y0 = 2110             # 聊天页输入栏顶
TAB_BAR_Y0 = 2140               # 首页底部 Tab 栏顶

# ---------------------------------------------------------------- 颜色（深色模式实测）
BG_CHAT = 17                    # 聊天页背景灰度众数
BG_HOME = 25                    # 首页背景灰度众数
# TODO(缺样本): 置顶条目底色。样本 h0/h1 中无置顶会话（风图不在顶部），
# 需补采"风图置顶在顶"的首页样本后由 calibrate_layout 标定。
BG_PINNED = None                # 预期 ≈ BG_HOME + 6~10
PINNED_DELTA = 4                # 置顶判定阈值：条目底色众数 >= BG_HOME + PINNED_DELTA

BUBBLE_GRAY_LO, BUBBLE_GRAY_HI = 30, 56   # 灰气泡/卡片灰度区间
QUOTE_GRAY = 36                 # 引用块均值（< 40 判引用）
BUBBLE_GRAY = 44                # 普通气泡均值

# 绿色（微信绿 #07C160；OpenCV H 刻度 0-179）
# 标定来源：h0_home 微信 Tab 图标 (H70-85/S91-246/V81-193)
#          p_fengtu 绿泡     (H73-74/S144-169/V81-181)
GREEN_H_LO, GREEN_H_HI = 60, 90
GREEN_S_MIN, GREEN_V_MIN = 90, 80

# 红色（未读角标 / [有人@我] 前缀）
RED_H_LO, RED_H_HI = 10, 170    # h < 10 or h > 170
RED_S_MIN, RED_V_MIN = 100, 150

# 橙红（红包/转账卡片，无样本未验证，按已知样式预留）
ORANGE_H_LO, ORANGE_H_HI = 5, 25
ORANGE_S_MIN, ORANGE_V_MIN = 120, 140

# 白色描边图标（Tab 非当前项 / 底栏圆钮 / 头部按键）：S 低 V 高
ICON_S_MAX, ICON_V_MIN = 90, 150

# ---------------------------------------------------------------- 底部 Tab（首页等 tab 页）
# x 按 4 等分中心 ≈ 135/405/675/945；图标行 y2130-2210，文字行 y2210-2280
TAB_ROIS = {  # name: (icon_roi, text_roi)  roi=(x0,y0,x1,y1)
    "微信":   ((75, 2130, 195, 2210),  (75, 2210, 195, 2280)),
    "通讯录": ((345, 2130, 465, 2210), (345, 2210, 465, 2280)),
    "发现":   ((615, 2130, 735, 2210), (615, 2210, 735, 2280)),
    "我":     ((885, 2130, 1005, 2210), (885, 2210, 1005, 2280)),
}
# 标定：h0_home 当前微信 Tab icon=0.249 text=0.096，其余=0.000
TAB_GREEN_MIN = 0.04            # 图标+文字 ROI 绿色占比合计阈值
# 标定：h0_home 四个图标 ROI 白色描边占比 0.030~0.093
TAB_ICON_LIKE_MIN = 0.02        # "图标状"白色像素占比阈值
TAB_PRESENT_MIN = 3             # 至少 3 个图标状 ROI 才认为是 tab 页
TAB_PAGE_MAP = {"微信": "wechat_home", "通讯录": "wechat_contacts",
                "发现": "wechat_discover", "我": "wechat_me"}

# ---------------------------------------------------------------- 首页顶部功能区
HOME_TITLE_ROI = (300, 100, 780, 200)     # "微信(N)" 标题区
HOME_SEARCH_CENTER = (890, 148)           # 搜索图标
HOME_PLUS_CENTER = (1012, 148)            # 加号圆圈
HOME_BTN_R = 55                           # 按键 bbox 半径（可视化用）

# ---------------------------------------------------------------- 首页会话列表
DIV_X0, DIV_X1 = 195, 1050      # 分割线检测条带（避开头像列与时间戳右缘）
# 标定：分割线行"略亮像素"（bg+2 < px < bg+40）计数 = 845/855；
#       文字行该计数 5~67 且含大量极亮像素。阈值取 600 很宽裕。
DIV_BAND_MIN = 600              # 一行内略亮像素数 >= 此值判为分割线
DIV_MERGE_GAP = 3               # 相邻分割线行合并间隔
ITEM_H = 194                    # 条目高（标定：h0 分割线间距恒为 194）
ITEM_PARTIAL_RATIO = 0.6        # 不足 0.6 条目高 = 残缺条目
HOME_ITEM_Y0 = 208              # 第一个条目顶（标题栏下）

HOME_AVATAR_X0, HOME_AVATAR_X1 = 30, 190   # 头像列
# 标定：h0 头像 x=43, w≈130-140, h≈130-140, area≈1.7e4
HOME_AVATAR_MIN_W, HOME_AVATAR_MIN_H = 80, 90
HOME_AVATAR_MIN_AREA = 5000
HOME_NAME_X0, HOME_NAME_X1 = 185, 700      # 名字区
HOME_PREVIEW_X1 = 1055                     # 预览右缘
HOME_TIME_X1 = 1055                        # 时间右缘
HOME_NAME_SPLIT = 0.55                     # 条目上部 55% 为名字行

# 未读角标：位于头像右上角。标定 h0：红点 ≈27x27 area~570；数字圈 w49-68 area 1700-2500
BADGE_ZONE_X0 = 100             # 角标中心 x ∈ [100, 200]
BADGE_ZONE_X1 = 200
BADGE_DOT_MIN_SIZE = 15         # 红点尺寸/面积窗口（过滤头像图里的红色杂点）
BADGE_DOT_MAX_SIZE = 32
BADGE_DOT_MIN_AREA = 200
BADGE_DOT_MAX_AREA = 700        # 红点 → unread = -1
# 数字红圈上限：超出即头像里的红色内容，不是角标
# （2026-08-10 摸鱼酱橙红帽头像 130x81/area3261 被误判为数字角标 → unread=-1
#  幻影未读 → 盯屏死循环每 14s 一次空旅程）
BADGE_NUM_MAX_W = 90            # 标定数字圈 w49-68，"99+" 胶囊更宽些
BADGE_NUM_MAX_H = 60
BADGE_NUM_MAX_AREA = 2600       # 标定 area 1700-2500
MUTE_BELL_X0, MUTE_BELL_X1 = 940, 1065   # 免打扰铃铛窗口（预览行右端）

# ---------------------------------------------------------------- 首页置顶 / 第一页
PINNED_SAMPLE_COL = (200, 380)  # 条目底色采样列（名字左侧文字间行）
# 第一页判定：主信号=顶部完整条目为置顶（依赖 BG_PINNED，TODO）；
# 备用=过滑探针（识别层只暴露接口 is_first_page_probe_needed，由上层滑动后重判）

# ---------------------------------------------------------------- 小程序面板页（无样本，预判特征，常量 TODO）
# TODO(缺样本): 补采"首页第一页继续上滑拉出的小程序面板"截图后标定。
MINIAPP_GRID_MIN_ICONS = 4      # 弱特征：内容区上部近圆形等大图标网格数量
MINIAPP_OCR_HINTS = ("最近使用", "我的小程序", "搜索小程序")   # 弱特征关键词

# ---------------------------------------------------------------- 聊天页头部/底栏按键
# 标定：返回箭头中心 (58,150)，更多 (1005,150)；底栏圆钮 y 中心 2185
CHAT_BACK_CENTER = (58, 150)
CHAT_MORE_CENTER = (1005, 150)
CHAT_VOICE_CENTER = (66, 2185)
CHAT_EMOJI_CENTER = (905, 2185)
CHAT_PLUS_CENTER = (1015, 2185)
# 聚焦态（2026-08-04 f_focused 实测）：输入框聚焦后 "ADB Keyboard {ON}" 细条
# 把整条输入栏顶起 ~115px，三圆钮 y 中心 2185 -> 2070（白描边占比 0.099~0.121）。
# 页面检测必须两态都查，否则聚焦态聊天页误判 unknown。
CHAT_BTN_FOCUS_DY = -115
CHAT_VOICE_CENTER_FOCUSED = (66, 2070)
CHAT_EMOJI_CENTER_FOCUSED = (905, 2070)
CHAT_PLUS_CENTER_FOCUSED = (1015, 2070)
CHAT_BTN_R = 55                 # 按键检测 ROI 半径 / bbox 半径
# 标定：p_fengtu 三圆钮白色描边占比 0.142~0.175
CHAT_BTN_LIKE_MIN = 0.05
CHAT_INPUT_BOX = (132, 2140, 840, 2230)   # 输入框·未聚焦 (x0,y0,x1,y1)
# 发送按钮（输入有字时出现，只存在于聚焦态）：绿色色块 + OCR "发送" 双判。
# 2026-08-04 f_focused_text 实测：绿块 y2033..2118 x917..1051（聚焦态输入栏
# 被 IME 细条顶起 ~115px，旧常量 y2130~2240 永远扫不到）。x0=915 起扫，
# 避开内容区绿泡右缘（~924，残留条带 <10px 宽过不了 min_w=40）。
SEND_SCAN_ZONE = (915, 2000, 1080, 2140)

# ---------------------------------------------------------------- 聊天页头像列
# 标定：左列头像 x=32, w≈74-108（暗头像右缘会缩水），h=108；
#       右列头像 x≈935-1058。列边界收紧以避开气泡左缘 156 / 绿泡右缘 ~924。
AVATAR_COL_L = (20, 150)
AVATAR_COL_R = (935, 1070)
AVATAR_MIN_W, AVATAR_MAX_W = 55, 145
AVATAR_MIN_H, AVATAR_MAX_H = 85, 145
AVATAR_MIN_AREA = 4000
AVATAR_ASPECT_LO, AVATAR_ASPECT_HI = 0.55, 1.45

# ---------------------------------------------------------------- 聊天页气泡
# 标定：灰泡左缘 x≈156（含 ~12px 尾巴），绿泡右缘 ≈924（含尾巴）
BUBBLE_MIN_AREA = 2500
BUBBLE_MIN_W, BUBBLE_MIN_H = 40, 40
GREEN_MIN_AREA = 3000
IMG_COMP_MIN_AREA = 15000       # 非文字泡（图片/视频/表情）最小面积
IMG_COMP_MIN_W, IMG_COMP_MIN_H = 120, 120

# 头像-气泡配对窗口（标定常量）
PAIR_PRIVATE_DY = 70            # 私聊：|cy_bubble - cy_avatar| < 70
NICK_ROW = 50                   # 群聊：bubble.top ≈ avatar.top + NICK_ROW（标定 39~52，均值 50）
PAIR_GROUP_TOL = 30             # 群聊配对容差 ±30

# 骨架推断：同侧相邻头像间距超过此值且中间无文字泡 → 藏有非文字消息
SKELETON_GAP_MIN = 108 + 120    # 单条消息典型高 + 气泡间距

# ---------------------------------------------------------------- 胶囊按钮
# "有人@我" 绿字灰底胶囊：右侧 x>780，y 在内容区上部；约 280x110
CAPSULE_SCAN = (600, CONTENT_Y0, 1080, 2000)   # OCR 文本落在此区且命中关键词
CAPSULE_PAD_L, CAPSULE_PAD_T = 95, 40          # 从文字框外扩得到胶囊整区（挖掩膜用）
CAPSULE_PAD_R, CAPSULE_PAD_B = 40, 35

# ---------------------------------------------------------------- 轮廓多边形
OUTLINE_EPSILON = 4.0           # approxPolyDP  epsilon（凸角保留，标定起点）

# ---------------------------------------------------------------- 手势条/IME 提示条
# IME 已锁定为 ADBKeyBoard（2026-08-04）：输入框聚焦时屏幕底部出现一条深色
# 细条 "ADB Keyboard {ON}"（y≈2260~2320）。它不是 UI 组件也不是消息：
# 各掩膜 ROI 均不覆盖该区（底栏圆钮 y≤2240、Tab 文字行 y≤2280 内无绿色），
# OCR 文本由 state_builder 按关键词过滤，见 ADB_IME_BAR。
ADB_IME_BAR = (0, 2245, 1080, 2340)      # (x0,y0,x1,y1)
ADB_IME_BAR_KEYWORDS = ("ADB Keyboard", "Keyboard {ON", "Keyboard {0N")

# ---------------------------------------------------------------- 正则（与 v1 一致）
import re as _re
TIME_RE = _re.compile(
    r"^(昨天\s*)?\d{1,2}[:：]\d{2}$"
    r"|^星期[一二三四五六日天]"
    r"|^\d{1,2}月\d{1,2}日"
    r"|^(上午|下午|凌晨|中午)\s*\d{1,2}[:：]\d{2}$"
)
MEMBER_RE = _re.compile(r"^(.*?)[(（](\d+)[)）]\s*$")
MENTION_RE = _re.compile(r"@([^\s@，,：:]+)")
NEW_MSG_RE = _re.compile(r"(\d+)\s*条新消息")
VOICE_RE = _re.compile(r'^(\d{1,3})\s*["″”]$')     # 语音气泡时长（无样本未验证）
VIDEO_DUR_RE = _re.compile(r"^\d{1,3}:\d{2}$")
RECALL_RE = _re.compile(r"撤回了一条消息")
HOME_TITLE_RE = _re.compile(r"^微信\s*[(（](\d+)[)）]?$")

PREVIEW_TYPE_MAP = {
    "链接": "link", "图文": "link", "图片": "image", "语音": "voice",
    "视频": "video", "红包": "redpacket", "转账": "transfer",
    "表情": "sticker", "动画表情": "sticker", "位置": "location",
    "文件": "file", "名片": "contact", "小程序": "miniapp", "音乐": "music",
}
