#!/usr/bin/env python3
"""page_detector.py - v2 页面分类（重写）。

主路径（纯掩膜，<5ms，不依赖 OCR）：
1. 底部 4 Tab 图标+文字双 ROI 绿色占比 -> tab 页 + 当前栏目；
2. 聊天页：底栏三圆钮（语音/表情/加号）白色描边检测；
3. 首页第一页上滑拉出的小程序面板（无样本，预判特征，常量 TODO）；
4. 新增页面分类器（OCR + 几何）：搜索页、添加朋友、扫一扫、朋友圈、
   个人资料、设置页、弹窗/对话框；
5. OCR 兜底（v1 方案降级）。
"""

import cv2
import numpy as np

from . import layout_consts as LC
from .img_utils import comps_from_mask, estimate_bg

TAB_LABELS = set(LC.TAB_ROIS.keys())

# ------------------------------------------------------------------ 新增页面常量
SEARCH_INPUT_ROI = (80, 100, 1000, 260)          # 顶部大搜索框
PROFILE_TOP_ROI = (200, 240, 800, 520)           # 个人资料昵称区
PROFILE_KEYWORDS = ("微信号", "地区", "朋友资料", "朋友圈", "发消息", "音视频通话")
MOMENTS_HINTS = ("轻触更换封面", "详情", "昨天")
SETTINGS_KEYWORDS = ("功能介绍", "版本更新", "投诉", "隐私保护",
                     "客服电话", "关于微信", "通用", "账号与安全",
                     "消息通知", "聊天", "存储空间", "帮助与反馈")
DIALOG_BUTTON_KEYWORDS = ("我知道了", "确定", "确认", "取消", "删除",
                          "是的", "好", "同意", "拒绝", "不再提示")
DIALOG_MESSAGE_MIN_WIDTH = 400

# 常见模态菜单/悬浮菜单选项（首页加号、会话长按、聊天加号等）
POPUP_MENU_KEYWORDS = (
    "发起群聊", "添加朋友", "扫一扫", "收付款", "标为未读", "置顶该聊天",
    "不显示该聊天", "删除该聊天", "消息免打扰", "折叠该群聊", "退出群聊",
    "投诉", "屏蔽", "清空聊天记录", "相册", "拍摄", "视频通话", "位置",
    "红包", "转账", "文件", "我的收藏", "名片", "语音输入",
)
POPUP_MENU_MIN_ITEMS = 3

# 已知次级列表页标题（空列表也要按列表页处理，避免被 OCR 兜底误判为聊天页）
KNOWN_LIST_TITLES = {
    "群聊", "通讯录标签", "标签", "新的朋友", "公众号", "服务号",
    "仅聊天的朋友", "企业微信联系人", "我的企业及企业联系人",
    "听一听", "搜一搜", "附近", "更改名字", "修改昵称", "设置备注", "备注",
    "聊天信息", "个人信息", "朋友权限", "来电铃声", "我的地址",
    "我的发票抬头", "微信豆", "通用", "帮助与反馈",
}

# 语音模式聊天页（无文字输入栏，只有"按住说话"大按钮）
VOICE_MODE_KEYWORDS = ("按住说话",)


class Page:
    def __init__(self, type_, current_tab=None, title="", reason=""):
        self.type = type_
        self.current_tab = current_tab
        self.title = title
        self.reason = reason

    def __repr__(self):
        return (f"Page({self.type!r}, tab={self.current_tab!r}, "
                f"title={self.title!r}, reason={self.reason!r})")


def _green_mask(hsv):
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    return ((h >= LC.GREEN_H_LO) & (h <= LC.GREEN_H_HI)
            & (s > LC.GREEN_S_MIN) & (v > LC.GREEN_V_MIN))


def _icon_like_ratio(hsv, roi):
    """ROI 内白色描边图标像素占比"""
    x0, y0, x1, y1 = roi
    s = hsv[y0:y1, x0:x1, 1]
    v = hsv[y0:y1, x0:x1, 2]
    if s.size == 0:
        return 0.0
    return float(((s < LC.ICON_S_MAX) & (v > LC.ICON_V_MIN)).mean())


def _green_ratio(green, roi):
    x0, y0, x1, y1 = roi
    sub = green[y0:y1, x0:x1]
    return float(sub.mean()) if sub.size else 0.0


def tab_scores(hsv):
    """每个 Tab 的绿色得分（图标 ROI + 文字 ROI 双判，防串扰）"""
    green = _green_mask(hsv)
    return {name: _green_ratio(green, icon) + _green_ratio(green, text)
            for name, (icon, text) in LC.TAB_ROIS.items()}


def count_tab_icons(hsv):
    return sum(1 for icon, _ in LC.TAB_ROIS.values()
               if _icon_like_ratio(hsv, icon) > LC.TAB_ICON_LIKE_MIN)


def chat_buttons_state(hsv):
    """底栏三圆钮（语音/表情/加号）检测，返回 'unfocused' / 'focused' / None。

    两态（layout_consts 头部注释）：未聚焦 y2185；输入框聚焦后 IME 细条把
    输入栏顶起 ~115px，圆钮移到 y2070。有字时 "+" 变为绿色发送按钮，
    绿底白字同样过白描边占比阈值（f_focused_text 实测 0.067），两态各查一次即可。"""
    for state, centers in (
            ("unfocused", (LC.CHAT_VOICE_CENTER, LC.CHAT_EMOJI_CENTER,
                           LC.CHAT_PLUS_CENTER)),
            ("focused", (LC.CHAT_VOICE_CENTER_FOCUSED,
                         LC.CHAT_EMOJI_CENTER_FOCUSED,
                         LC.CHAT_PLUS_CENTER_FOCUSED))):
        ok = True
        for cx, cy in centers:
            r = LC.CHAT_BTN_R
            roi = (cx - r, cy - r, cx + r, cy + r)
            if _icon_like_ratio(hsv, roi) < LC.CHAT_BTN_LIKE_MIN:
                ok = False
                break
        if ok:
            return state
    return None


def has_chat_buttons(hsv):
    """底栏三圆钮全部存在（未聚焦或聚焦态任一）-> 聊天页"""
    return chat_buttons_state(hsv) is not None


def _detect_input_bar_top_buttons(hsv):
    """兜底：三圆钮（语音/表情/加号）白色描边圆检测输入栏顶。

    扫描三列同 y 取最小值——三钮必须同时是环才可信（内容区头像只影响一列）。
    返回输入栏顶 y 或 None。
    """
    r = LC.CHAT_BTN_R
    centers = (LC.CHAT_VOICE_CENTER[0], LC.CHAT_EMOJI_CENTER[0],
               LC.CHAT_PLUS_CENTER[0])
    best_y, best_score = None, 0.0
    # 扫描区间：输入栏顶在 [聚焦+引用最高 ~1800, 圆钮下缘 SCREEN_H-r] 之间
    for cy in range(1800, LC.SCREEN_H - r, 5):
        score = min(_icon_like_ratio(hsv, (cx - r, cy - r, cx + r, cy + r))
                    for cx in centers)
        if score > best_score:
            best_score, best_y = score, cy
    if best_y is None or best_score < LC.CHAT_BTN_LIKE_MIN:
        return None
    # 圆钮是「白描边环」，占比实测 0.142~0.175；整块白（白输入栏/白头像）占比会
    # 远高于此（≥0.5）。峰值太亮 = 不是圆环而是整块亮色区域，拒绝，否则会把
    # 内容区的白头像误判成输入栏。
    if best_score > 0.5:
        return None
    offset = LC.CHAT_VOICE_CENTER[1] - LC.INPUT_BAR_Y0   # 2185 - 2110 = 75
    return int(best_y - offset)


def _pinned_bar_rect(img, gray=None, hsv=None):
    """复用 chat_parser 的置顶条学习方案：气泡掩膜里的顶部全宽细条。

    灰泡掩膜（低饱和 + 灰阶 [BUBBLE_GRAY_LO, BUBBLE_GRAY_HI]）连通域中
    w>900、h<170、x<30、y<600 的条 = 置顶消息条。置顶条文字行被灰色底
    包围，闭运算（close_ksize=9）填洞后是一个完整连通域，bbox 精确覆盖
    整条（行投影会被文字行凹陷骗到，见 2026-08-14 实测）。
    返回 (x, y, w, h) 或 None。
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    Y0, Y1 = LC.CONTENT_Y0, LC.INPUT_BAR_Y0
    band = img[Y0:Y1]
    intense = band.max(axis=2)
    neutral = (intense.astype(np.int16) - band.min(axis=2)) < 12
    bm = (neutral & (intense >= LC.BUBBLE_GRAY_LO)
          & (intense <= LC.BUBBLE_GRAY_HI)).astype(np.uint8)
    full = np.zeros(gray.shape, np.uint8)
    full[Y0:Y1] = bm
    for c in comps_from_mask(full, min_area=LC.BUBBLE_MIN_AREA,
                             close_ksize=9, min_w=LC.BUBBLE_MIN_W,
                             min_h=LC.BUBBLE_MIN_H):
        x, y, w, h, area = c
        if w > 900 and h < 170 and x < 30 and y < 600:
            return (int(x), int(y), int(w), int(h))
    return None


def detect_pinned_bar_end(img, gray=None, hsv=None):
    """聊天页顶部「置顶消息条」的下边界（消息内容区从此开始）。

    深色背景方案（2026-08-14 新聊天背景定稿）：置顶条底部是一条贯穿左右、
    行内 std≈0 的【纯黑分隔带】（如 y=344~366，mean=17、std=0.00），内容区
    从该带【结束】位置开始（y=367）。找「最后一个」纯黑带（std<1.5、
    mean<25、连续≥3 行），返回其结束 y；比旧气泡掩膜方案精确（旧方案返回
    纯黑带起点 345，把 22px 分隔带误裁进内容区 → 图片间出现间隙）。

    浅色背景回退：气泡掩膜全宽细条（w>900、h<170、x<30、y<600）→ 置顶条
    rect，返回其下边界 y+h+1。无置顶条返回 None（调用方回退 CONTENT_Y0）。
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    row_mean = gray.mean(axis=1).astype(float)
    row_std = gray.std(axis=1)
    # 深色背景：最后一个纯黑分隔带（置顶条底部）。
    # std<3.0（2026-08-15 放宽）：纯黑带尾部常有压缩噪声 std 1.5~1.7，
    # 旧阈值 1.5 会截断黑带（56 屏真机采样 4 屏顶部误判 361/363）→ 放宽到 3.0
    # 后 56/56 全部命中 367。
    bands = []
    y = LC.CONTENT_Y0 - 50
    while y < min(900, gray.shape[0]):
        if row_std[y] < 3.0 and row_mean[y] < 25:
            e = y
            while e < gray.shape[0] and row_std[e] < 3.0 and row_mean[e] < 25:
                e += 1
            if e - y >= 3:
                bands.append((y, e))
            y = e
        else:
            y += 1
    if bands:
        # 内容区顶 = 最后一个纯黑带【结束位置】。黑带是置顶条底部固定
        # 分隔带（位置恒定，如 344-366），其后即内容区——内容区首行可能是
        # 消息泡(mean 60-68)而非背景(91)，不能再要求首行 mean>70。
        #
        # 2026-08-15 修黑缝隙：黑带结束行(如 366)本身还是暗过渡行
        # (mean≈29，黑带残留)，若作为裁切起点，每张 crop 顶部第一行都是
        # 这条暗线；stitch 拼接时 B1(=prev_crop[0:split]) 的这条暗线会成为
        # 接缝处的黑缝隙（实测 screen_02 接缝 y=1367 mean=29/dark=100%）。
        # → 跳过过渡行：从黑带结束+1（第一个内容行，mean 60~110）起裁。
        e = bands[-1][1]
        if e + 1 < gray.shape[0] and row_mean[e + 1] > row_mean[e] + 10:
            return int(e + 1)
        return int(e)
    # 浅色背景回退：气泡掩膜全宽细条
    r = _pinned_bar_rect(img, gray=gray, hsv=hsv)
    if r is None:
        return None
    return int(r[1] + r[3]) + 1


def detect_input_bar_top(img, gray=None, hsv=None):
    """CV 检测聊天页输入栏顶 y（不依赖固定坐标）。

    方案（2026-08-14 用户定稿）：输入栏顶是一条【贯穿屏幕左右的分界线】。
    输入栏本体（含引用预览条）的每一行「略亮像素」(内容区背景 bg+2 ~ bg+40)
    占比 ≈100%，而消息泡/卡片最多 ~80%——取「从某行起直到屏幕底部占比都
    ≥90% 的连续全宽带」的最上沿，即输入栏顶。比三圆钮方案稳：内容区头像
    （x≈32-108 与语音列重叠）永远凑不出全宽带，不会误判。

    兜底：全宽带检测失败（输入栏底色与内容区背景接近等）时，退回三圆钮
    白色描边检测（_detect_input_bar_top_buttons，三列同时为环才可信）。

    返回 int 输入栏顶 y；检测不到返回 None，调用方回退固定值。
    """
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    bg = estimate_bg(gray, LC.CONTENT_Y0, 2000)      # 内容区背景（深色模式≈92）
    x0, x1 = LC.DIV_X0, LC.DIV_X1                    # 195..1050 避开左右头像列
    strip = gray[:, x0:x1].astype(int)

    # ---- 方案 A（亮色背景）：输入栏本体「略亮像素」全宽带 ----
    bright = ((strip > bg + 2) & (strip < bg + 40)).sum(axis=1)
    th = int((x1 - x0) * 0.85)                       # 全宽带占比阈值
    # 输入栏顶 = 从上往下第一条「本行及后续 15 行占比都 ≥85%」的行。
    # 消息泡/卡片实测最高 678/855≈79% 过不了 85%；输入栏本体 100%、按钮行
    # ~86%，都能过。从上往下找，先命中输入栏顶（2119），不会扫到底部手势
    # 导航带（2311，在输入栏下方）。
    for y in range(1800, LC.SCREEN_H - 15):
        if int(bright[y:y + 15].min()) >= th:
            return int(y)

    # ---- 方案 B（深色背景）：输入栏是「均匀暗带」全宽带 ----
    # 2026-08-14 换深色聊天背景后实测：输入栏纯黑带 mean≈30~41、行内 std≈0，
    # 与内容区背景(≈92)差异显著，整行贯穿。
    #   输入栏顶 = 从上往下第一条「本行 mean<50 且本行起连续 15 行 std<4」
    #   的行。
    # - mean<50 绝对暗阈值只看【当前行】：内容区背景(mean≈91)均匀也不算，
    #   底部消息泡(mean≈57-63, std≈22)不过 std<4；
    # - 连续 15 行 std<4：消息行 std>15 过不了；手势导航带(2311)在输入栏
    #   下方，从上往下先命中输入栏(2119)。
    # - 2026-08-15 尾暗约束：消息内容里也有深色均匀块（图片/引用卡，如
    #   screen_45 的 y1915-1990 mean≈41 std≈2 被误判为输入栏顶）→ 增加
    #   「从该行直到底部暗像素占比>80%」约束（输入栏+底部导航都是暗，
    #   消息块下方会回到亮背景）。56 屏真机采样后全部命中 2119。
    row_std = strip.std(axis=1)
    row_mean = strip.mean(axis=1)
    H_g = gray.shape[0]
    for y in range(1800, H_g - 15):
        if row_mean[y] < 50 and row_std[y:y + 15].max() < 4.0:
            tail_dark = float((row_mean[y:H_g] < 60).mean())
            if tail_dark > 0.8:
                return int(y)

    return _detect_input_bar_top_buttons(hsv)


def detect_miniapp_panel(img, ocr_items, gray=None, hsv=None):
    """小程序面板页（首页第一页继续上滑拉出）。无样本，按已知形态预判：
    强特征 = tab 栏在，但内容区既无会话分割线也无头像列连通域；
    弱特征 = OCR 命中"最近使用/我的小程序/搜索小程序"，
             或内容区上部 >=4 个近圆形等大图标规则网格。
    强 + 任一弱 -> True。TODO(缺样本): 补采后校准常量。"""
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    bg = estimate_bg(gray, LC.CONTENT_Y0, 2000)
    # 强特征 a：无分割线（复用首页的"略亮像素行"判据）
    strip = gray[LC.CONTENT_Y0:LC.TAB_BAR_Y0, LC.DIV_X0:LC.DIV_X1].astype(int)
    band = ((strip > bg + 2) & (strip < bg + 40)).sum(axis=1)
    if (band >= LC.DIV_BAND_MIN).any():
        return False
    # 强特征 b：无会话头像列
    intense = img.max(axis=2)
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    nonbg = ((intense > bg + 11) | (hsv[:, :, 1] > 60)).astype(np.uint8)
    col = np.zeros_like(nonbg)
    col[LC.CONTENT_Y0:LC.TAB_BAR_Y0, LC.HOME_AVATAR_X0:LC.HOME_AVATAR_X1] = \
        nonbg[LC.CONTENT_Y0:LC.TAB_BAR_Y0, LC.HOME_AVATAR_X0:LC.HOME_AVATAR_X1]
    if comps_from_mask(col, min_area=LC.HOME_AVATAR_MIN_AREA, close_ksize=15,
                       min_w=LC.HOME_AVATAR_MIN_W, min_h=LC.HOME_AVATAR_MIN_H):
        return False
    # 弱特征 a：OCR 关键词
    texts = " ".join(it["text"] for it in (ocr_items or []))
    if any(k in texts for k in LC.MINIAPP_OCR_HINTS):
        return True
    # 弱特征 b：内容区上部近圆形等大图标网格（圆形连通域 >=4）
    zone = nonbg[LC.CONTENT_Y0:1000, :]
    roundish = 0
    for x, y, w, h, area in comps_from_mask(zone, min_area=3000, close_ksize=9,
                                            min_w=60, min_h=60):
        if 0.75 < w / max(h, 1) < 1.33 and 60 <= w <= 200:
            roundish += 1
    return roundish >= LC.MINIAPP_GRID_MIN_ICONS


def _title_from_ocr(img, ocr_items):
    """标题栏文字：优先从已有 OCR 结果取，否则对标题区单独跑一次"""
    cands = [it for it in (ocr_items or [])
             if LC.TITLE_Y0 <= it["cy"] <= LC.TITLE_Y1
             and 200 <= it["cx"] <= 880 and it["h"] >= 28]
    if cands:
        return max(cands, key=lambda it: it["conf"])["text"]
    if ocr_items is None:
        from .ocr_engine import run_ocr
        x0, y0, x1, y1 = LC.HOME_TITLE_ROI
        crop = img[y0:y1, x0:x1]
        items = run_ocr(crop)
        if items:
            return max(items, key=lambda it: it["conf"])["text"]
    return ""


# ------------------------------------------------------------------ 新增分类器

def _no_tab_page(hsv):
    """显式不是 4 Tab 页（tab 图标未达标）。部分新增页底部有杂项图标，
    但 tab 检测要求 >=3 个才成立，因此用 <3 而非 ==0 放行。"""
    return count_tab_icons(hsv) < LC.TAB_PRESENT_MIN


def _has_large_qr(img, gray=None):
    """图片下半区是否存在大面积的浅色二维码/名片区域（添加朋友页）。"""
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, _ = gray.shape
    roi = gray[int(h * 0.55):, :]
    mask = (roi > 180).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            np.ones((9, 9), np.uint8))
    comps = comps_from_mask(mask, min_area=40000,
                            min_w=150, min_h=150)
    return bool(comps)


def _has_top_avatar(img, gray=None, bg=None):
    """个人资料页顶部左侧的大头像连通域。"""
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if bg is None:
        bg = estimate_bg(gray, LC.CONTENT_Y0, LC.TAB_BAR_Y0)
    x0, y0, x1, y1 = 0, LC.STATUS_BAR_BOTTOM, 350, 600
    top = gray[y0:y1, x0:x1]
    mask = (top > bg + 25).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            np.ones((11, 11), np.uint8))
    comps = comps_from_mask(mask, min_area=3000,
                            min_w=50, min_h=50)
    for _, cy, w, h, area in comps:
        if 0.55 <= w / max(h, 1) <= 1.55 and cy < 400 and area < 50000:
            return True
    return False


def _ocr_text(ocr_items):
    """拼接全部 OCR 文本，便于关键词匹配。"""
    if not ocr_items:
        return ""
    return " ".join(it["text"] for it in ocr_items)


def _has_modal_menu(ocr_items):
    """检测覆盖在 Tab 页上的模态菜单（首页加号、会话长按等）。

    判据：内容区中部存在 >=3 个菜单关键词，且横向中心集中在一个窄列内，
    纵向跨度 >=120px。返回 True 表示应优先按 popup_menu 处理。"""
    if not ocr_items:
        return False
    hits = []
    for it in ocr_items:
        txt = it.get("text", "")
        if not any(k in txt for k in POPUP_MENU_KEYWORDS):
            continue
        box = it.get("box", [])
        if not box:
            continue
        if isinstance(box[0], (list, tuple)):
            xs = [float(p[0]) for p in box]
            cx = (min(xs) + max(xs)) / 2.0
        else:
            cx = (float(box[0]) + float(box[2])) / 2.0
        # 排除底部 Tab 区和顶部状态栏的误命中
        cy = it.get("cy", 0)
        if cy < LC.STATUS_BAR_BOTTOM + 50 or cy > LC.TAB_BAR_Y0:
            continue
        hits.append((cy, cx))
    if len(hits) < POPUP_MENU_MIN_ITEMS:
        return False
    hits.sort()
    centers = [c for _, c in hits]
    # 菜单项通常集中在屏幕右侧弹出菜单区（x 380~980）
    in_col = [c for c in centers if 380 <= c <= 980]
    if len(in_col) < POPUP_MENU_MIN_ITEMS:
        return False
    ys = [y for y, _ in hits]
    return max(ys) - min(ys) >= 120


def is_voice_mode_page(img, hsv, ocr_items):
    """语音输入模式：无 Tab、无聊天圆钮，但有聊天标题和"按住说话"。"""
    if ocr_items is None or not _no_tab_page(hsv) or has_chat_buttons(hsv):
        return False
    title = _title_from_ocr(img, ocr_items)
    if not title:
        return False
    texts = _ocr_text(ocr_items)
    return any(k in texts for k in VOICE_MODE_KEYWORDS)


def is_search_page(img, hsv, ocr_items):
    """搜索页：无 Tab 栏、无真正标题、顶部大搜索框含"搜索"关键词。

    注意：搜索框占位文案本身落在标题栏高度内，需要把它和真实标题区分开。"""
    if ocr_items is None or not _no_tab_page(hsv):
        return False
    # 标题栏内若存在非搜索文案，则不是独立搜索页
    title_items = [it for it in ocr_items
                   if LC.TITLE_Y0 <= it["cy"] <= LC.TITLE_Y1
                   and 200 <= it["cx"] <= 880 and it["h"] >= 28]
    for it in title_items:
        if "搜索" not in it["text"]:
            return False
    x0, y0, x1, y1 = SEARCH_INPUT_ROI
    for it in ocr_items:
        if x0 <= it["cx"] <= x1 and y0 <= it["cy"] <= y1:
            if "搜索" in it["text"]:
                return True
    return False


def is_add_friend_page(img, hsv, ocr_items):
    """添加朋友页：标题含"添加朋友"，且有菜单项/二维码区。"""
    if ocr_items is None or not _no_tab_page(hsv):
        return False
    title = _title_from_ocr(img, ocr_items)
    if "添加朋友" not in title:
        return False
    texts = _ocr_text(ocr_items)
    has_menu = "扫一扫" in texts and "手机联系人" in texts
    return has_menu or _has_large_qr(img)


def is_scan_page(img, hsv, ocr_items):
    """扫一扫页：无 Tab 栏，底部有"扫一扫/翻译"，背景极暗。"""
    if ocr_items is None or not _no_tab_page(hsv):
        return False
    texts = _ocr_text(ocr_items)
    if "扫一扫" not in texts or "翻译" not in texts:
        return False
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    bg = estimate_bg(gray, LC.CONTENT_Y0, LC.TAB_BAR_Y0)
    return bg < 8


def is_moments_page(img, hsv, ocr_items):
    """朋友圈：无 Tab 栏、标题为空或"朋友圈"，有封面/时间线提示。"""
    if ocr_items is None or not _no_tab_page(hsv):
        return False
    title = _title_from_ocr(img, ocr_items)
    if title and title != "朋友圈":
        return False
    texts = _ocr_text(ocr_items)
    # 强提示：更换封面文字；时间线常见元素组合
    hints = MOMENTS_HINTS
    if "轻触更换封面" in texts:
        return True
    return sum(1 for h in hints if h in texts) >= 2


def is_profile_page(img, hsv, ocr_items):
    """个人资料：无 Tab 栏、无聊天按钮，标题为空或资料相关，顶部大头像+资料关键词。"""
    if ocr_items is None or not _no_tab_page(hsv) or has_chat_buttons(hsv):
        return False
    title = _title_from_ocr(img, ocr_items)
    if title and title not in ("个人资料", "详细资料", "资料"):
        return False
    texts = [it["text"] for it in ocr_items]
    hits = sum(1 for k in PROFILE_KEYWORDS
               if any(k in t for t in texts))
    return hits >= 2 or (hits >= 1 and _has_top_avatar(img))


def is_settings_page(img, hsv, ocr_items):
    """设置页：无 Tab 栏，标题含"设置"，或内容区命中设置关键词。

    注意：要排除聊天信息/个人资料等含零星设置关键词的次级页面。"""
    if ocr_items is None or not _no_tab_page(hsv):
        return False
    title = _title_from_ocr(img, ocr_items)
    if title and ("聊天信息" in title or "个人资料" in title
                  or "朋友资料" in title or "资料" in title):
        return False
    if "设置" in title:
        return True
    texts = [it["text"] for it in ocr_items]
    hits = sum(1 for k in SETTINGS_KEYWORDS if any(k in t for t in texts))
    # 非“设置”标题时，需要更多关键词才稳，避免聊天信息页误检
    return hits >= 3


def is_dialog_page(img, hsv, ocr_items):
    """弹窗/对话框：无 Tab 栏，底部有对话框按钮，上方有宽居中文本。"""
    if ocr_items is None or not _no_tab_page(hsv):
        return False
    h = img.shape[0]
    btn_y0 = h - 340
    btns = []
    for it in ocr_items:
        if it["cy"] < btn_y0 or not (150 <= it["cx"] <= 930):
            continue
        if any(k in it["text"] for k in DIALOG_BUTTON_KEYWORDS):
            btns.append(it)
    if not btns:
        return False
    btn_top = min(it["cy"] - it["h"] / 2 for it in btns)
    for it in ocr_items:
        y = it["cy"]
        if btn_top - 700 <= y < btn_top:
            x0, _, x1, _ = it["box"]
            if x1 - x0 >= DIALOG_MESSAGE_MIN_WIDTH and 150 <= it["cx"] <= 930:
                return True
    return False


def is_generic_list_page(img, hsv, ocr_items):
    """兜底：无真正选中的 Tab、无聊天按钮，按标题或列表项密度判为次级列表页。

    覆盖 Discover/聊天信息/公众号/表情商店等次级列表页，避免被 OCR 兜底误判为聊天页。"""
    if ocr_items is None or has_chat_buttons(hsv):
        return False
    # 聊天页救援（2026-08-08 真机）：输入栏残留文字/引用预览时 ⊕ 变成
    # "发送"按钮，底栏只剩 2 个圆钮，has_chat_buttons 判不出聊天页——
    # 此时 ADB Keyboard 细条或"群名(N)标题+右下发送按钮"是强聊天证据，
    # 绝不能按次级列表页归类（否则 send_text 误判"不在聊天页"拒绝发送）
    import re as _re
    title0 = _title_from_ocr(img, ocr_items)
    group_title = bool(title0 and _re.search(r"[（(]\d+[)）]\s*$", title0))
    for it in ocr_items:
        if it["cy"] < 1900:
            continue
        t = it["text"] or ""
        if "ADB Keyboard" in t:
            return False
        if group_title and "发送" in t and it["cx"] > 880:
            return False
    # 若存在真正的 Tab 页（有绿色选中 Tab），则不是次级列表页
    if count_tab_icons(hsv) >= LC.TAB_PRESENT_MIN:
        scores = tab_scores(hsv)
        if max(scores.values()) >= LC.TAB_GREEN_MIN:
            return False
    title = _title_from_ocr(img, ocr_items)
    # 已知次级列表页标题：即使当前内容为空/占位提示，也按列表页处理
    if title and any(t in title for t in KNOWN_LIST_TITLES):
        return True
    from . import list_parser
    items = list_parser.parse_list_items(ocr_items, page_type="wechat_generic_list")
    if title and len(items) >= 3:
        return True
    # 无标题但列表项密集（如表情商店、发现子页），也按列表页处理
    if not title and len(items) >= 5:
        return True
    return False


def detect_page(img, ocr_items=None, hsv=None, gray=None):
    """返回 Page。ocr_items 可空（空则 OCR 兜底路径不可用，纯掩膜判定）。
    hsv/gray 可由调用方（build_state）预先算好传入，避免重复 cvtColor。"""
    if hsv is None:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # 0) 模态菜单优先于 Tab 页（它在 Tab 页上方弹出）
    if ocr_items is not None and _has_modal_menu(ocr_items):
        return Page("wechat_popup_menu", title="菜单", reason="modal_menu")

    # 1) 聊天页：底栏三圆钮（必须在 Tab 检测之前，语音模式底部图标可能被误检为 Tab）
    if has_chat_buttons(hsv):
        return Page("wechat_chat", title=_title_from_ocr(img, ocr_items))

    # 2) tab 页：>=3 个图标状 ROI，且至少有一个 Tab 呈绿色（当前选中）
    if count_tab_icons(hsv) >= LC.TAB_PRESENT_MIN:
        scores = tab_scores(hsv)
        current = max(scores, key=scores.get)
        if scores[current] >= LC.TAB_GREEN_MIN:
            page_type = LC.TAB_PAGE_MAP[current]
            # 首页 tab 下可能是上滑拉出的小程序面板
            if page_type == "wechat_home" and detect_miniapp_panel(
                    img, ocr_items, gray=gray, hsv=hsv):
                return Page("wechat_miniapp_panel", current_tab=current,
                            title="小程序面板", reason="miniapp_features")
            return Page(page_type, current_tab=current,
                        title=current if current != "微信" else "微信")
        # 没有绿色 Tab：可能是 Discover 搜索等无真正 Tab 的页面，继续下游分类

    # 3) 新增页面分类器（OCR + 几何启发式）
    if gray is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    if is_search_page(img, hsv, ocr_items):
        return Page("wechat_search", title="搜索", reason="search_input")

    if is_add_friend_page(img, hsv, ocr_items):
        return Page("wechat_add_friend", title="添加朋友",
                    reason="add_friend_title")

    if is_scan_page(img, hsv, ocr_items):
        return Page("wechat_scan", title="扫一扫", reason="scan_tabs")

    if is_moments_page(img, hsv, ocr_items):
        return Page("wechat_moments", title="朋友圈", reason="moments_features")

    if is_profile_page(img, hsv, ocr_items):
        # 尝试提取昵称：取顶部区域最大字号且不含资料关键词的文本
        nickname = ""
        best_h = 0
        x0, y0, x1, y1 = PROFILE_TOP_ROI
        for it in (ocr_items or []):
            if x0 <= it["cx"] <= x1 and y0 <= it["cy"] <= y1:
                txt = it["text"]
                if (txt and len(txt) >= 1 and not txt.isdigit()
                        and not any(k in txt for k in PROFILE_KEYWORDS)
                        and it["h"] >= 35):
                    if it["h"] > best_h:
                        best_h = it["h"]
                        nickname = txt
        return Page("wechat_profile",
                    title=nickname or "个人资料",
                    reason="profile_features")

    if is_settings_page(img, hsv, ocr_items):
        return Page("wechat_settings", title="设置", reason="settings_title")

    if is_dialog_page(img, hsv, ocr_items):
        # 标题用第一个按钮文本，便于识别弹窗意图
        btn_text = ""
        h = img.shape[0]
        for it in ocr_items:
            if it["cy"] >= h - 340 and 150 <= it["cx"] <= 930:
                if any(k in it["text"] for k in DIALOG_BUTTON_KEYWORDS):
                    btn_text = it["text"]
                    break
        return Page("wechat_dialog", title=btn_text or "弹窗",
                    reason="dialog_panel")

    if is_voice_mode_page(img, hsv, ocr_items):
        return Page("wechat_chat", title=_title_from_ocr(img, ocr_items),
                    reason="voice_mode")

    if is_generic_list_page(img, hsv, ocr_items):
        return Page("wechat_generic_list",
                    title=_title_from_ocr(img, ocr_items),
                    reason="list_fallback")

    # 4) OCR 兜底（v1 方案）
    if ocr_items:
        tab_hits = {it["text"] for it in ocr_items
                    if it["cy"] > LC.TAB_BAR_Y0 and it["text"] in TAB_LABELS}
        if len(tab_hits) >= 3:
            title = _title_from_ocr(img, ocr_items)
            t = title.split("(")[0].split("（")[0]
            return Page(LC.TAB_PAGE_MAP.get(t, "wechat_home"),
                        current_tab=t or "微信", title=title or "微信",
                        reason="ocr_fallback")
        title = _title_from_ocr(img, ocr_items)
        if title:
            return Page("wechat_chat", title=title, reason="ocr_fallback")

    return Page("wechat_unknown", reason="no_features")
