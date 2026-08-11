# -*- coding: utf-8 -*-
"""moments_poster.py — 微信朋友圈纯文本发布（action 层）。

真机标定环境：OnePlus 6T (1080x2340) + 微信 8.0.76 + 深色模式 + ADBKeyBoard IME。

设计目标：从任意起始界面（锁屏/Android 首页/其他应用/微信任意页）出发，
都能稳定发布一条纯文本朋友圈。

所有触控均走项目 RandomTouch 随机化框架（高斯偏中心落点、随机按压时长、
随机等待间隔），仅 KEYCODE_BACK 等系统键事件不随机。

主流程：
    1. 唤醒 + 解锁 + 拉起微信
    2. 确保在微信首页
    3. 点「发现」tab
    4. 点「朋友圈」
    5. 长按右上角相机 → 纯文字发表页
    6. 处理首次「我知道了」提示
    7. 输入文案
    8. 点「发表」
    9. 校验页面离开发表页 → 确认成功
   10. 返回微信首页
"""

import logging
import time

from ..device import layout
from ..device.random_touch import Rect
from ..perception import layout_consts as LC
from ..perception.ocr_engine import run_ocr

log = logging.getLogger("action.moments_poster")

# ------------------------------------------------------------------ 常量
# 发现页「朋友圈」入口行（整行可点，y 范围由真机截图标定）
DISCOVER_MOMENTS_ROW = Rect(0, 110, 1080, 160)
# 朋友圈页右上角相机图标
MOMENTS_CAMERA = Rect(980, 80, 80, 80)
# 文字发表页输入框
MOMENT_INPUT = Rect(80, 200, 920, 300)
# 右上角「发表」按钮
MOMENT_POST_BTN = Rect(900, 80, 130, 80)
# 首次实验功能提示的「我知道了」按钮
FIRST_TIME_OK = Rect(300, 1700, 480, 160)

# OCR 等待/重试
OCR_RETRY = 3
STEP_RETRY = 2


# ------------------------------------------------------------------ 纯函数（可单测）
def _find_text(ocr_items, text, region=None, conf_threshold=0.5):
    """在 OCR 结果中找包含 text 的项，返回最近一个（cy 最小）。"""
    hits = []
    for it in ocr_items:
        t = (it.get("text") or "").strip()
        if text not in t:
            continue
        if it.get("conf", 0) < conf_threshold:
            continue
        cx, cy = float(it.get("cx", 0)), float(it.get("cy", 0))
        if region is not None:
            x0, y0, x1, y1 = region
            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                continue
        hits.append((cy, cx, it))
    if not hits:
        return None
    hits.sort()
    return hits[0][2]


def _is_wechat_home(ocr_items):
    """当前是否为微信首页（底部「微信」tab 高亮）。"""
    return _find_text(ocr_items, "微信", region=(0, 2050, 270, 2340)) is not None


def _is_discover_page(ocr_items):
    """当前是否为发现页。"""
    return _find_text(ocr_items, "发现", region=(400, 2050, 800, 2340)) is not None \
        and _find_text(ocr_items, "朋友圈", region=(0, 80, 1080, 300)) is not None


def _is_moment_text_page(ocr_items):
    """当前是否为「发表文字」页。"""
    return (
        _find_text(ocr_items, "发表文字", region=(300, 50, 780, 180)) is not None
        or _find_text(ocr_items, "这一刻的想法", region=(50, 150, 600, 350)) is not None
    )


def _is_moments_feed_page(ocr_items):
    """当前是否为朋友圈时间线页。"""
    has_camera = _find_text(ocr_items, "相机", region=(900, 50, 1080, 150)) is not None
    has_cover_tip = _find_text(ocr_items, "轻触更换封面", region=(300, 300, 780, 600)) is not None
    return has_camera or has_cover_tip


def _norm_for_match(s):
    """验证时忽略空格、换行、英文大小写差异。"""
    return "".join(str(s or "").split()).lower()


# ------------------------------------------------------------------ 动作封装
def _snap(dev):
    """截图并 OCR，返回 (img, ocr_items)。"""
    img = dev.capture_bytes()
    items = run_ocr(img)
    return img, items


def _tap_ocr_item(dev, item, fallback_rect, sigma_ratio=0.25):
    """优先用 OCR 项中心落点（小范围随机），找不到用兜底 rect。

    所有点击都走 RandomTouch，sigma_ratio 控制落点离散程度。
    """
    if item is not None:
        cx = float(item.get("cx", 0))
        cy = float(item.get("cy", 0))
        if cx > 0 and cy > 0:
            # 以 OCR 中心为基准，构造 80x80 点击区，内部高斯随机
            dev.tap_rect(Rect(int(cx) - 40, int(cy) - 40, 80, 80),
                         sigma_ratio=sigma_ratio)
            return
    dev.tap_rect(fallback_rect, sigma_ratio=sigma_ratio)


def _ensure_wechat_home(tools, max_back=5):
    """确保回到微信首页；支持从微信内任意页或微信外启动。"""
    # 先尝试识别当前是否已在微信首页
    for _ in range(2):
        try:
            _, items = _snap(tools.dev)
            if _is_wechat_home(items):
                return True
        except Exception:
            pass
        tools.dev.wait_random(300, 600)

    # 如果不在微信首页，按 back 若干次（处理微信内子页面）
    for _ in range(max_back):
        try:
            _, items = _snap(tools.dev)
            if _is_wechat_home(items):
                return True
        except Exception:
            pass
        tools.dev.back()
        tools.dev.wait_random(500, 900)

    # back 没回首页：点底部微信 tab 兜底
    for _ in range(2):
        try:
            tools.dev.double_tap_rect(layout.TAB_WECHAT)
            tools.dev.wait_random(800, 1200)
            _, items = _snap(tools.dev)
            if _is_wechat_home(items):
                return True
        except Exception:
            log.warning("点微信 tab 兜底失败，重试")
            tools.dev.wait_random(500, 800)

    return False


def _tap_discover_tab(dev):
    """点底部「发现」tab，并校验页面切到发现页。"""
    for _ in range(STEP_RETRY):
        dev.tap_rect(layout.TAB_DISCOVER)
        dev.wait_random(800, 1200)
        try:
            _, items = _snap(dev)
            if _is_discover_page(items) or _find_text(items, "朋友圈", region=(0, 80, 1080, 300)):
                return
        except Exception:
            pass
    raise RuntimeError("无法切换到发现页")


def _enter_moments(tools):
    """在发现页点「朋友圈」进入时间线。"""
    for attempt in range(OCR_RETRY):
        try:
            _, items = _snap(tools.dev)
            row = _find_text(items, "朋友圈", region=(0, 80, 1080, 300))
            if row:
                _tap_ocr_item(tools.dev, row, DISCOVER_MOMENTS_ROW)
            else:
                tools.dev.tap_rect(DISCOVER_MOMENTS_ROW)
            tools.dev.wait_random(1200, 1800)

            # 校验是否已进入朋友圈
            _, items = _snap(tools.dev)
            if _is_moments_feed_page(items):
                return
        except Exception:
            log.warning("enter_moments attempt %d failed", attempt + 1)
            tools.dev.wait_random(500, 800)
    raise RuntimeError("无法进入朋友圈")


def _open_text_input(tools):
    """长按右上角相机，进入纯文字发表页；处理首次提示。"""
    for _ in range(STEP_RETRY):
        tools.dev.long_press_rect(MOMENTS_CAMERA)
        tools.dev.wait_random(1200, 1800)

        try:
            _, items = _snap(tools.dev)
        except Exception:
            continue

        # 已经是发表文字页
        if _is_moment_text_page(items):
            return

        # 出现「我知道了」提示
        ok = _find_text(items, "我知道了", region=(200, 1500, 880, 2000))
        if ok:
            _tap_ocr_item(tools.dev, ok, FIRST_TIME_OK)
            tools.dev.wait_random(600, 1000)
            _, items = _snap(tools.dev)
            if _is_moment_text_page(items):
                return

    raise RuntimeError("无法进入纯文字发表页")


def _type_and_post(tools, text):
    """在发表文字页输入并点击发表。

    长文本分两半用 ADB_INPUT_TEXT 广播发送（不分细块，避免丢块）。
    """
    tools.dev.tap_rect(MOMENT_INPUT)
    tools.dev.wait_random(300, 600)

    tools.dev.clear_text()
    tools.dev.wait_random(200, 400)

    # 分两半发送：热身 + 两半，每半之间充分等待
    import shlex
    tools.dev._shell("am broadcast -a ADB_INPUT_TEXT --es msg ''")
    tools.dev.wait_random(400, 700)

    half = len(text) // 2
    for part in (text[:half], text[half:]):
        if not part.strip():
            continue
        tools.dev._shell(
            f"am broadcast -a ADB_INPUT_TEXT --es msg {shlex.quote(part)}")
        tools.dev.wait_random(600, 1000)

    tools.dev.wait_random(800, 1200)

    # 找发表按钮
    _, items = _snap(tools.dev)
    btn = _find_text(items, "发表", region=(800, 50, 1080, 170))
    _tap_ocr_item(tools.dev, btn, MOMENT_POST_BTN)
    tools.dev.wait_random(1500, 2500)


def _verify_posted(tools, text, timeout_s=12):
    """发表后校验：优先 OCR 找文案，找不到则校验页面已离开发表页。"""
    target = _norm_for_match(text)
    tools.dev.wait_random(1500, 2500)
    deadline = time.time() + timeout_s
    left_editor = False
    while time.time() < deadline:
        try:
            _, items = _snap(tools.dev)
            # 1) 如果能 OCR 到文案，直接成功
            for it in items:
                if target in _norm_for_match(it.get("text", "")):
                    cy = float(it.get("cy", 0))
                    if 350 < cy < 1200:
                        return True
            # 2) 记录是否已离开发表文字页
            if not _is_moment_text_page(items):
                left_editor = True
            # 3) 已离开发表页且当前是朋友圈 feed 或微信首页，视为成功
            if left_editor and (_is_moments_feed_page(items) or _is_wechat_home(items)):
                return True
        except Exception:
            pass
        tools.dev.wait_random(800, 1200)
    return False


def _back_to_home(tools):
    """从朋友圈返回微信首页：先 back 到发现页，再点微信 tab。"""
    tools.dev.back()
    tools.dev.wait_random(800, 1200)
    try:
        tools.dev.tap_rect(layout.TAB_WECHAT)
        tools.dev.wait_random(800, 1200)
    except Exception:
        pass
    for _ in range(2):
        try:
            _, items = _snap(tools.dev)
            if _is_wechat_home(items):
                return True
        except Exception:
            pass
        tools.dev.tap_rect(layout.TAB_WECHAT)
        tools.dev.wait_random(600, 1000)
    return False


# ------------------------------------------------------------------ 主入口
def post_text_moments(tools, text):
    """发布一条纯文本朋友圈。

    Args:
        tools: WeChatTools 实例（提供 .dev.capture_bytes/tap_rect/back/...）
        text: 要发布的文案

    Returns:
        dict: {"ok": bool, "posted": bool, "error": str|None}
    """
    log.info("开始发朋友圈：%r", text)
    try:
        # 点亮屏幕 + 解 keyguard + 拉起微信，确保从任意起点都能开始
        tools.dev.wake_and_dim()
        tools.dev.open_wechat()
        tools.dev.wait_random(800, 1500)

        if not _ensure_wechat_home(tools):
            raise RuntimeError("无法回到微信首页")
        _tap_discover_tab(tools.dev)
        _enter_moments(tools)
        _open_text_input(tools)
        _type_and_post(tools, text)
        posted = _verify_posted(tools, text)
        _back_to_home(tools)
        return {"ok": True, "posted": posted, "error": None}
    except Exception as e:
        log.exception("发朋友圈失败")
        try:
            _back_to_home(tools)
        except Exception:
            pass
        return {"ok": False, "posted": False, "error": str(e)}


# 向后兼容的简短别名
post_moments = post_text_moments
