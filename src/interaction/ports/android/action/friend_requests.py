# -*- coding: utf-8 -*-
"""friend_requests.py — 好友申请识别与通过（action 层）。

背景（2026-08-09 用户要求）：好友申请被点开后系统通知和红点都会消失，
不能再依赖通知通道——必须主动巡检：通讯录 → 新的朋友 → 识别"查看"按钮
（待通过的申请右侧是"查看"，已处理的是"已添加"/"已过期"文字）。

两个入口：
- probe_pending(tools)：导航到"新的朋友"页，数待通过数量（带滚动去重），
  数完回首页。供主循环空闲巡检用。
- accept_all(tools)：逐条点"查看"进验证页 → "通过验证"/"添加到通讯录"
  → 收尾弹窗（完成/发送）→ 回列表，直到没有"查看"或达到上限。

页面流约定（OnePlus 6T + 微信 8.0.76 深色模式实测）：
- 通讯录页 "新的朋友" 是列表第一行
- "新的朋友"页标题栏文字即"新的朋友"
- 点"查看"进验证/资料页；通过动作按钮文案为 "通过验证" 或 "添加到通讯录"
- 通过后可能弹打招呼页（"发送"）或确认页（"完成"/"确定"），也可能直接
  留在资料页（出现"发消息"即已是好友）

tools 协议：WeChatTools（dev.capture_bytes/tap_rect/swipe_zone/back/
wait_random、open_wechat、_snap）。纯函数不碰设备，可单测。
"""

import logging
import os
import random
import time

from ..device import layout
from ..device.random_touch import Rect
from ..perception import layout_consts as LC

log = logging.getLogger("action.friend_requests")

# ------------------------------------------------------------------ 常量
VIEW_BTN_TEXT = "查看"              # 待通过申请右侧按钮
DONE_MARKS = ("已添加", "已过期")    # 已处理标记
ACCEPT_KEYWORDS = ("通过验证", "添加到通讯录", "通过朋友验证")
DIALOG_KEYWORDS = ("完成", "确定", "发送", "我知道了")
# 通讯录页入口行文案：有待处理申请时是"新的朋友"；清空后入口变成
# "朋友推荐"（2026-08-09 实测——全通过后备份入口文案切换导致巡检导航失败）
ENTRY_KEYWORDS = ("新的朋友", "朋友推荐")
FRIEND_PAGE_TITLE = "新的朋友"

LIST_ZONE = Rect(0, 250, 1080, 1750)     # 新的朋友列表滑动区（避标题栏/tab）
ROW_BAND = 130                            # 同一行 OCR 项 cy 容差
MAX_PROBE_SCROLLS = 5                     # 巡检最多下翻屏数
NAV_MAX_STEPS = 8                         # 导航容错步数


# ------------------------------------------------------------------ 纯函数（可单测）
def find_view_buttons(ocr_items):
    """OCR 项里找所有"查看"按钮（列表区、右侧），按 cy 排序返回。"""
    hits = []
    for it in ocr_items:
        if (it.get("text") or "").strip() != VIEW_BTN_TEXT:
            continue
        cx, cy = float(it.get("cx", 0)), float(it.get("cy", 0))
        if not (150 < cy < LC.TAB_BAR_Y0 - 40):
            continue
        if cx < LC.SCREEN_W * 0.55:         # "查看"按钮恒在列表右侧
            continue
        hits.append({"cx": cx, "cy": cy})
    hits.sort(key=lambda h: h["cy"])
    return hits


def extract_applicant_name(ocr_items, btn):
    """从"查看"按钮所在行提取申请人昵称。

    行内布局（2026-08-09 实测）：昵称 cy ≈ 按钮 cy（粗体大字），
    验证消息在行下方（小字，常以"我是"开头）；头像列（cx<150）的
    OCR 噪声（"四"/"C"等）必须排除。
    """
    best, best_key = None, None
    for it in ocr_items:
        text = (it.get("text") or "").strip()
        if (not text or text == VIEW_BTN_TEXT or text in DONE_MARKS
                or text.startswith("我是")):
            continue
        cx, cy = float(it.get("cx", 0)), float(it.get("cy", 0))
        if cx < 150 or cx >= btn["cx"] - 60:    # 头像列/按钮右侧不算
            continue
        if not (btn["cy"] - 90 <= cy <= btn["cy"] + 45):
            continue
        key = (float(it.get("h", 0)), -cy)      # 字高优先，同高取靠上
        if best_key is None or key > best_key:
            best, best_key = text, key
    return best or ""


def dedup_names(names):
    """滚动跨屏计数的名单去重（保序）。"""
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def is_friend_page(state):
    """当前 state 是否"新的朋友"页。"""
    title = (state.get("page", {}) or {}).get("title") or ""
    return FRIEND_PAGE_TITLE in title


# ------------------------------------------------------------------ tab 红点
# 通讯录 tab 红点（用户 2026-08-09 规则）：新申请来时 tab 图标右上角出红点；
# 点一次通讯录 tab 红点即消（但申请仍在，入口行红点保留）；点开申请详情
# 未通过则连入口红点也没了。所以 tab 红点是"有新申请"的即时信号，
# 盯屏每帧可检，不需要定时巡检。
def contacts_tab_has_dot(hsv):
    """通讯录 tab 图标 ROI 内是否有红色圆点（首页帧的 HSV，纯函数可单测）。

    tab 图标是白色描边/绿色填充，文字行是白色——ROI 内任何成形的红色
    组件就是未读红点。
    """
    import numpy as np
    from ..perception.img_utils import comps_from_mask
    x0, y0, x1, y1 = LC.TAB_ROIS["通讯录"][0]
    roi = hsv[y0:y1, x0:x1]
    h, s, v = roi[:, :, 0], roi[:, :, 1], roi[:, :, 2]
    red = (((h < LC.RED_H_LO) | (h > LC.RED_H_HI))
           & (s > LC.RED_S_MIN) & (v > LC.RED_V_MIN)).astype(np.uint8)
    for bx, by, bw, bh, area in comps_from_mask(red, min_area=40,
                                                close_ksize=3,
                                                min_w=8, min_h=8):
        if bw <= 80 and bh <= 80 and 0.4 < bw / max(bh, 1) < 2.5:
            return True
    return False


# ------------------------------------------------------------------ 设备侧辅助
def _ocr(img):
    from ..perception.ocr_engine import run_ocr
    return run_ocr(img)


def _snap_ocr(tools):
    """截图 + OCR（不经过 state_builder，列表页不需要全量解析）。"""
    img = tools.dev.capture_bytes()
    if isinstance(img, (bytes, bytearray)):
        import numpy as np
        import cv2
        img = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
    return img, _ocr(img)


def _tap_text(tools, cx, cy, half_w=70, half_h=50):
    tools.dev.tap_rect(Rect(int(cx) - half_w, int(cy) - half_h,
                            half_w * 2, half_h * 2))


def _page_title(tools, img=None):
    """当前页标题（state_builder 全量解析，导航判定用）。"""
    state = tools._snap() if img is None else tools.parse_state(img)
    page = state.get("page", {}) or {}
    return page.get("type", ""), page.get("title", "") or "", state


def _ensure_home(tools):
    """回到微信首页：open_wechat + back 兜底。"""
    for _ in range(4):
        try:
            ptype, _, _ = _page_title(tools)
        except Exception:  # noqa: BLE001
            ptype = ""
        if ptype == "wechat_home":
            return True
        tools.back()
        tools.dev.wait_random(600, 1000)
    r = tools.open_wechat()
    return bool(getattr(r, "success", False))


def _goto_friend_page(tools):
    """首页 → 通讯录 → 新的朋友。成功返回 True（停在新的朋友页）。"""
    if not _ensure_home(tools):
        return False
    for _ in range(NAV_MAX_STEPS):
        tools.dev.tap_rect(layout.TAB_CONTACTS)
        tools.dev.wait_random(900, 1400)
        img, items = _snap_ocr(tools)
        # 已在新的朋友页（可能从别处回来）
        ptype, title, _ = _page_title(tools, img)
        if FRIEND_PAGE_TITLE in title:
            return True
        # 找入口行（"新的朋友"/清空后的"朋友推荐"）
        entry = None
        for it in items:
            t = (it.get("text") or "").strip()
            cy = float(it.get("cy", 0))
            if any(kw in t for kw in ENTRY_KEYWORDS) and 120 < cy < 900:
                entry = it
                break
        if entry is None:
            # 入口固定是通讯录第一行，OCR 抖动时滚回顶部再试
            tools.dev.swipe_zone(LIST_ZONE, direction="down",
                                 length_ratio=(0.4, 0.6))
            tools.dev.wait_random(600, 1000)
            continue
        _tap_text(tools, float(entry["cx"]), float(entry["cy"]),
                  half_w=200, half_h=70)
        tools.dev.wait_random(1000, 1600)
        _, title, _ = _page_title(tools)
        if FRIEND_PAGE_TITLE in title:
            return True
    log.warning("goto_friend_page: %d 步内未进入", NAV_MAX_STEPS)
    return False


def _scan_visible(tools):
    """当前屏的待通过名单：[{name, btn}...]。"""
    _, items = _snap_ocr(tools)
    out = []
    for btn in find_view_buttons(items):
        out.append({"name": extract_applicant_name(items, btn), "btn": btn})
    return out


# ------------------------------------------------------------------ 巡检
def probe_pending(tools, sleep_fn=time.sleep):
    """数待通过的好友申请数量。返回 (count, names)；导航失败返回 (None, [])。"""
    if not _goto_friend_page(tools):
        return None, []
    names = []
    no_new = 0
    for _ in range(MAX_PROBE_SCROLLS + 1):
        before = len(names)
        names = dedup_names(names + [v["name"] for v in _scan_visible(tools)])
        if len(names) == before:
            no_new += 1
        else:
            no_new = 0
        if no_new >= 2:                      # 连续两屏无新增 → 到底
            break
        tools.dev.swipe_zone(LIST_ZONE, direction="up",
                             length_ratio=(0.5, 0.7))
        tools.dev.wait_random(700, 1100)
    _ensure_home(tools)
    log.info("friend probe: %d 个待通过 %s", len(names), names[:5])
    return len(names), names


# ------------------------------------------------------------------ 通过
def _accept_in_detail(tools):
    """验证/资料页执行通过动作。返回 True=流程闭环（通过/已是好友/已回列表）。

    实测页面流（2026-08-09）：点"查看"直接进"通过朋友验证"页（底部大
    "完成"按钮，cy~2147）；部分条目进资料页（底部"添加到通讯录"，点后
    转验证页）。"完成"点后可能回列表、或进资料页（已是好友）。
    确认纪律：点过任何一个动作按钮后，若后续扫描不再有任何可点按钮，
    视为闭环（实测"发消息"OCR 不稳定，不能当唯一判据——3 个已成功
    通过的申请曾被误判失败）。
    """
    acted = False
    for attempt in range(5):
        _, items = _snap_ocr(tools)
        texts = [(it.get("text") or "").strip() for it in items]

        # 已回列表（"完成"后常见去向）——本条的闭环由列表重扫确认
        if any(FRIEND_PAGE_TITLE in t for t in texts
               if len(t) <= 8):
            return True
        # 已是好友
        if "发消息" in texts:
            return True

        # 通过按钮（资料页底部"添加到通讯录"等）
        hit = None
        for kw in ACCEPT_KEYWORDS:
            for it in items:
                t = (it.get("text") or "").strip()
                cy = float(it.get("cy", 0))
                if kw in t and 200 < cy < LC.TAB_BAR_Y0 - 40:
                    hit = it
                    break
            if hit is not None:
                break
        if hit is not None:
            log.info("  点击: %s", (hit.get("text") or "").strip())
            _tap_text(tools, float(hit["cx"]), float(hit["cy"]),
                      half_w=200, half_h=60)
            tools.dev.wait_random(1200, 1800)
            acted = True
            continue

        # 确认/收尾按钮（验证页底部"完成"、打招呼页"发送"、弹窗"确定"）
        hit = None
        for it in items:
            t = (it.get("text") or "").strip()
            cy = float(it.get("cy", 0))
            if t in DIALOG_KEYWORDS and cy > LC.SCREEN_H - 500:
                hit = it
                break
        if hit is not None:
            log.info("  收尾: %s", (hit.get("text") or "").strip())
            _tap_text(tools, float(hit["cx"]), float(hit["cy"]))
            tools.dev.wait_random(1200, 1800)
            acted = True
            continue

        # 点过按钮后页面已无动作可点 → 视为闭环（资料页"发消息"OCR 不稳定）
        if acted:
            return True
        if attempt >= 2:
            return False
        tools.dev.wait_random(700, 1100)
    return acted


def _back_to_friend_list(tools):
    """从详情/资料页回到"新的朋友"列表。"""
    for _ in range(6):
        _, title, _ = _page_title(tools)
        if FRIEND_PAGE_TITLE in title:
            return True
        tools.back()
        tools.dev.wait_random(600, 1000)
    return False


def _save_new_contact_profile(tools, name):
    """好友通过后，若当前停在个人资料页，提取档案并存档 + 打标（Phase 3）。

    复用 profile_extractor.extract_profile + roster_update.save_contact_after_accept。
    存档 key 用申请人名 name；实际主昵称以资料页提取结果为准。
    真机验证点：通过好友后是否停在资料页、页面文案（"发消息"/"音视频通话"等）。
    """
    try:
        from ..perception.profile_extractor import extract_profile, is_profile_page
        from ..perception.roster_update import save_contact_after_accept, ROSTERS_DIR
        img = tools.dev.capture_bytes()
        if not is_profile_page(run_ocr(img)):
            log.info("好友通过后未停在资料页，跳过存档")
            return
        avatar_dir = os.path.join(ROSTERS_DIR, name, "avatars")
        record = extract_profile(tools.dev, avatar_dir, session_name=name)
        if record is None:
            log.warning("资料页提取失败，跳过存档")
            return
        save_contact_after_accept(name, record)
        log.info("好友通过存档: %s -> %s", name, record.get("main_nickname"))
    except Exception as e:  # noqa: BLE001
        log.warning("好友通过后存档失败: %s", e)


def accept_all(tools, max_accept=30, sleep_fn=time.sleep,
               rand_fn=random.uniform):
    """通过全部待处理的好友申请。

    返回 {"ok", "accepted": [名字...], "remaining": 估计剩余, "error": str|None}
    结束后尽力回首页。
    """
    result = {"ok": False, "accepted": [], "remaining": 0, "error": None}
    try:
        if not _goto_friend_page(tools):
            result["error"] = "无法进入新的朋友页"
            return result

        stall = 0
        while len(result["accepted"]) < max_accept:
            visible = _scan_visible(tools)
            if not visible:
                stall += 1
                if stall >= 3:               # 连续三屏没有"查看" → 清完
                    break
                tools.dev.swipe_zone(LIST_ZONE, direction="up",
                                     length_ratio=(0.5, 0.7))
                tools.dev.wait_random(700, 1100)
                continue
            stall = 0
            target = visible[0]              # 逐条处理最上面一条
            name = target["name"] or "未识别"
            log.info("  通过申请: %s", name)
            _tap_text(tools, target["btn"]["cx"], target["btn"]["cy"])
            tools.dev.wait_random(1200, 1800)

            if _accept_in_detail(tools):
                result["accepted"].append(name)
                log.info("  ✓ %s（累计 %d）", name, len(result["accepted"]))
                # 好友通过 hook：通过后若停在资料页，提取个人档案存档 + 打标
                _save_new_contact_profile(tools, name)
            else:
                log.warning("  ✗ %s 通过失败，跳过", name)
                stall += 1                   # 防同一条死循环
                if stall >= 3:
                    result["error"] = f"连续失败，中断于 {name}"
                    break

            if not _back_to_friend_list(tools):
                result["error"] = "无法返回新的朋友列表"
                break
            # 处理完一条列表会变化，从头再扫（不滚动，新的待通过会顶上来）

        # 估算剩余
        try:
            remaining = len(_scan_visible(tools))
        except Exception:  # noqa: BLE001
            remaining = -1
        result["remaining"] = remaining
        result["ok"] = True
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("accept_all 异常")
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        try:
            _ensure_home(tools)
        except Exception:  # noqa: BLE001
            log.exception("accept_all 收尾回首页失败")
