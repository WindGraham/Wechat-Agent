# -*- coding: utf-8 -*-
"""profile_extractor.py — 个人资料页信息提取（可复用原语）。

从当前屏幕（须已停在个人资料页）提取：主昵称 / 群昵称 / 微信号 / 地区 /
是否好友 / 圆角头像。头像裁剪沿用现有逻辑（与 run_full_group_spider 的
find_profile_avatar_box / crop_exact_rounded_avatar 逐行等价），但改用
dev 接口（capture_bytes / _shell），不写 /tmp、不依赖 raw adb 全局。

供 Phase 3（好友通过存档）与 Phase 4（改名/换头像调和）复用。
"""

import base64
import logging
import os
import re
import time

import cv2
import numpy as np

from .ocr_engine import run_ocr

log = logging.getLogger("perception.profile_extractor")

# 状态栏/通知预览常见片段（与 spider 的 _STATUS_RE 等价）
_STATUS_RE = re.compile(
    r"^\d{2}:\d{2}(?::\d{2})?$|^\d+%$|^微信\s*[(（]|^交流一下|^YOUSAOBI")

_NICK_EXCLUDE = ["微信号", "地区", "朋友圈", "来源", "朋友资料", "设置",
                 "群昵称", "聊天信息", "备注", "更多", "添加到通讯录",
                 "音视频通话", "发消息"]


def sanitize_filename(filename):
    clean = re.sub(r'[\\/*?:"<>|]', '_', filename or "").strip()
    return clean if clean else "unnamed"


def is_profile_page(items):
    texts = " ".join(i.get("text", "") for i in items)
    return any(k in texts for k in
               ("微信号", "朋友资料", "发消息", "音视频通话", "添加到通讯录"))


def _strip_gender_and_emoji_noise(text):
    """去掉性别图标 OCR 残留下的 &/8/0 及前导 emoji 碎片。"""
    if not text:
        return text
    text = re.sub(r'[\s]*[&＆♂♀👤🔵🔴][\s]*$', '', text)
    text = re.sub(r'\s+[80oOQ]$', '', text)
    text = re.sub(
        r'^(?![A-Za-z]$)([A-Za-z0-9!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>/?])\s+(?=.{2,})',
        '', text)
    return text.strip()


def _clean_nickname_roi(roi):
    """把 ROI 里的性别图标/emoji 等彩色像素涂成背景色，避免 OCR 读成噪声。"""
    if roi.size == 0:
        return roi
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    colored = ((hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 60) & (hsv[:, :, 2] < 245))
    cleaned = roi.copy()
    bg = int(np.median(roi[:, :, 0]))
    cleaned[colored] = (bg, bg, bg)
    return cleaned


def find_profile_avatar_box(img):
    """在个人资料页定位正方形头像框，返回 (x, y, w, h)。"""
    if img is None:
        return None
    roi = img[150:600, 0:500]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    avatar_box, max_area = None, 0
    for cnt in contours:
        x, y, w_box, h_box = cv2.boundingRect(cnt)
        area = w_box * h_box
        ar = float(w_box) / h_box
        if 0.85 <= ar <= 1.15 and 150 <= w_box <= 350 and 150 <= h_box <= 350:
            if area > max_area:
                max_area = area
                avatar_box = (x, y + 150, w_box, h_box)
    return avatar_box if avatar_box else (42, 251, 175, 175)


def crop_exact_rounded_avatar(img, avatar_dir, save_filename, avatar_box=None):
    """圆角头像裁剪（18px 弧度，4 通道 BGRA，逻辑与 spider 完全一致）。

    返回相对路径 "avatars/<file>.png"。
    """
    if img is None:
        return None
    if avatar_box is None:
        avatar_box = find_profile_avatar_box(img)
    x, y, w, h = avatar_box
    avatar_crop = img[y:y + h, x:x + w].copy()

    radius = 18
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.rectangle(mask, (radius, 0), (w - radius, h), 255, -1)
    cv2.rectangle(mask, (0, radius), (w, h - radius), 255, -1)
    cv2.circle(mask, (radius, radius), radius, 255, -1)
    cv2.circle(mask, (w - radius, radius), radius, 255, -1)
    cv2.circle(mask, (radius, h - radius), radius, 255, -1)
    cv2.circle(mask, (w - radius, h - radius), radius, 255, -1)

    bgra = cv2.cvtColor(avatar_crop, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = mask

    base_name = save_filename[:-4]
    ext = ".png"
    counter = 1
    final_filename = save_filename
    while os.path.exists(os.path.join(avatar_dir, final_filename)):
        final_filename = f"{base_name}_{counter}{ext}"
        counter += 1

    avatar_rel_path = os.path.join("avatars", final_filename)
    cv2.imwrite(os.path.join(avatar_dir, final_filename), bgra)
    return avatar_rel_path


def get_clipboard_exact(dev):
    """输入法后门提权读剪贴板（微信资料页微信号复制用）。"""
    try:
        dev._shell("ime set io.appium.settings/.AppiumIME")
        time.sleep(0.5)
        out = dev._shell("am broadcast -a io.appium.settings.clipboard.get")
        dev._shell("ime set com.android.adbkeyboard/.AdbIME")
    except Exception as e:  # noqa: BLE001
        log.warning("clipboard 提权失败: %s", e)
        return None
    for line in (out or "").split("\n"):
        if "data=" in line:
            d = line.split('data="')[1].split('"')[0]
            if d:
                try:
                    return base64.b64decode(d).decode("utf-8")
                except Exception:
                    return d
    return None


def _truncated(s):
    """OCR 文本是否被省略号截断（长群昵称/昵称会显示为「很长的…」）。"""
    s = (s or "").rstrip()
    return bool(s) and s.endswith(("...", "…", "···", "…"))


def _longpress_copy(dev, cy):
    """长按资料页某一行 → 点「复制」→ 读剪贴板。返回文本或 None。

    与 spider 的微信号提权逻辑等价（input swipe 同点=长按 + 点复制 + 读剪贴板）。
    """
    try:
        dev._shell(f"input swipe 400 {int(cy)} 400 {int(cy)} 800")  # 长按该行
        time.sleep(1)
        dev.tap(450, int(cy) - 65)   # 「复制」按钮（实测在长按点上方）
        time.sleep(1)
        return get_clipboard_exact(dev)
    except Exception as e:  # noqa: BLE001
        log.warning("长按复制提权失败: %s", e)
        return None


def extract_profile(dev, avatar_dir, profile_shots_dir=None, session_name=None):
    """从当前资料页提取个人档案。返回 record dict 或 None（不在资料页/失败）。

    dev 需提供：capture_bytes() / _shell()（tap_rect/swipe 仅剪贴板兜底用）。
    """
    try:
        img = dev.capture_bytes()
    except Exception as e:  # noqa: BLE001
        log.exception("profile extract 截图失败")
        return None
    return extract_profile_from_img(img, avatar_dir,
                                    profile_shots_dir=profile_shots_dir,
                                    session_name=session_name, dev=dev)


def extract_profile_from_img(img, avatar_dir, profile_shots_dir=None,
                             session_name=None, dev=None):
    """从已截好的资料页图片提取个人档案（纯图片处理，dev 仅剪贴板兜底用）。

    拆出来供真机截图 / 历史 profile_shots 离线验证复用。
    """
    if img is None:
        return None

    full_items = run_ocr(img)
    if not is_profile_page(full_items):
        log.warning("当前页不是个人资料页，跳过提取")
        return None

    # 资料页截图存档（可选）
    profile_shot_rel = ""
    if profile_shots_dir and session_name:
        os.makedirs(profile_shots_dir, exist_ok=True)
        idx = len([f for f in os.listdir(profile_shots_dir)
                   if f.endswith(".png")])
        fn = f"{sanitize_filename(session_name)}_{idx:04d}.png"
        cv2.imwrite(os.path.join(profile_shots_dir, fn), img)
        profile_shot_rel = os.path.join("profile_shots", fn)

    main_nickname, group_nickname, wechat_id, region, is_friend = "", "", "", "", 0
    wxid_cy = None
    group_nick_cy = None
    for i in full_items:
        t = i["text"]
        if "微信号" in t:
            wxid_cy = i["cy"]
            clean_t = t.replace("微信号:", "").replace("微信号：", "").replace("微信号", "").strip()
            if clean_t:
                wechat_id = clean_t
        elif "地区" in t:
            clean_r = t.replace("地区:", "").replace("地区：", "").replace("地区", "").strip()
            if clean_r:
                region = clean_r
        elif "群昵称" in t:
            group_nick_cy = i["cy"]
            clean_g = t.replace("群昵称:", "").replace("群昵称：", "").replace("群昵称", "").strip()
            if clean_g:
                group_nickname = clean_g
        elif "发消息" in t or "音视频通话" in t:
            is_friend = 1

    # 微信号/群昵称 长按复制提权兜底（长文本会被省略号截断，剪贴板拿全量）
    if dev is not None:
        if wxid_cy and not wechat_id:
            exact = _longpress_copy(dev, wxid_cy)
            if exact:
                wechat_id = exact
        if group_nick_cy and (not group_nickname or _truncated(group_nickname)):
            exact = _longpress_copy(dev, group_nick_cy)
            if exact:
                group_nickname = exact

    # 精确定位头像，昵称 OCR 限制在「头像右侧」区域
    avatar_box = find_profile_avatar_box(img)
    ax, ay, aw, ah = avatar_box
    ih, iw = img.shape[:2]
    roi_x = min(ax + aw + 10, iw - 50)
    roi_y = max(0, ay - 10)
    roi_w = min(700, iw - roi_x - 50)
    roi_h = min(ih, ay + ah + 90) - roi_y
    if roi_w <= 20 or roi_h <= 20:
        main_nickname = "未知昵称"
    else:
        roi = img[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]
        cleaned_roi = _clean_nickname_roi(roi)
        nick_items = run_ocr(cleaned_roi)
        for it in nick_items:
            it["cy"] += roi_y
            it["box"] = tuple(v + (roi_x if i % 2 == 0 else roi_y)
                              for i, v in enumerate(it["box"]))

        cutoff = roi_y + roi_h
        for it in nick_items:
            if "微信号" in it["text"] or "地区" in it["text"] or "群昵称" in it["text"]:
                cutoff = min(cutoff, it["cy"] - 10)

        name_parts = []
        for it in nick_items:
            t = it["text"].strip()
            if not t or it["cy"] >= cutoff:
                continue
            if any(k in t for k in _NICK_EXCLUDE) or _STATUS_RE.search(t):
                continue
            name_parts.append(t)
        raw_nick = " ".join(name_parts) if name_parts else ""
        main_nickname = _strip_gender_and_emoji_noise(raw_nick)
        if not main_nickname:
            main_nickname = "未知昵称"

    if any(k in main_nickname for k in ("微信(", "交流一下", "YOUSAOBI",
                                        "国家网络", "Clash", "Termux")) \
            or len(main_nickname) > 30:
        log.warning("提取到的昵称疑似干扰文本: %r，跳过", main_nickname)
        return None

    name_for_file = main_nickname if main_nickname != "未知昵称" else group_nickname
    os.makedirs(avatar_dir, exist_ok=True)
    clean_file_name = sanitize_filename(name_for_file) + ".png"
    avatar_path = crop_exact_rounded_avatar(img, avatar_dir, clean_file_name,
                                            avatar_box=avatar_box)

    return {
        "main_nickname": main_nickname,
        "group_nickname": group_nickname,
        "wechat_id": wechat_id,
        "region": region,
        "is_friend": bool(is_friend),
        "avatar_image_path": avatar_path or "",
        "profile_screenshot": profile_shot_rel,
        "update_ts": time.time(),
    }
