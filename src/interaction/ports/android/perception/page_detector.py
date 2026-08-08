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
