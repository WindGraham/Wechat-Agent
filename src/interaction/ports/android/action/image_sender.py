# -*- coding: utf-8 -*-
"""image_sender.py — 聊天页发图片 / 发文件流程（action 层）。

移植自旧仓库 src/agent/skills.py 的 _execute_send_image，适配新包路径：
- adb 调用走 DeviceCtl（_run/_shell 自带 -s <serial> 与重试），不再裸 subprocess
- OCR 用 ..perception.ocr_engine.run_ocr（内存截图，全程不落盘）
- 坐标常量用 ..device.layout；加号面板项点按走 .plus_panel（网格检测+翻页）

图片流程（docs/ANDROID_PORT.md 操控节）：
  push 图片到相册目录 → 媒体扫描广播 → 加号面板 → 相册 →
  选第一张 → OCR 验证"发送(N)" → 发送（逐步 OCR 校验，不盲点）

文件流程（2026-08-08 真机重测，旧"手机存储/Download"路径已废弃）：
  push 到 /sdcard/Download + touch 刷新 mtime（保证 SAF 最近列表排第一）→
  加号面板（第二页）"文件" → "选择文件"页点"手机文件"标签 → 点"选取" →
  系统 SAF 选择器"最近"列表第一行就是我们的文件 → 点选 → "发送"。

输入栏聚焦两态处理（2026-08-07 旧仓库修复实录）：聚焦时底部有
"ADB Keyboard {ON}" 细条，整条输入栏顶起 ~125px，⊕ 中心 2185→2060；
先 OCR 底部细条判断聚焦态再点 ⊕，两态都试一次仍不开面板才判失败。

契约限制（docs/DECISION_LAYER.md）：path 只接受本机绝对路径，禁止 url。

dev 协议（可注入假 dev 离线测试）：capture_bytes() / tap_rect(Rect) /
wait_random(lo,hi) / swipe_zone(...) / input_text(t) / _run(args) / _shell(cmd)。
真实设备验证本轮未做，代码路径按旧仓库真机验证流程 1:1 移植。
"""

import logging
import os
import shlex
import time

import cv2
import numpy as np

from ..device import layout
from ..device.random_touch import Rect
from ..perception.ocr_engine import run_ocr
from . import plus_panel

log = logging.getLogger("action.image_sender")

# ------------------------------------------------------------------ 坐标常量
# ⊕ 加号按钮：未聚焦用 layout.CHAT_PLUS（中心 1015,2185）；
# 聚焦态整条输入栏顶起 ~125px，⊕ 中心变为 (1015,2060)。
CHAT_PLUS_FOCUSED = Rect(960, 2005, 110, 110)

# 相册选择页第一张图的选择圆（网格 218+271·col, 256+272·row，旧仓库真机标定）。
# 仅作 CV 检测失败时的兜底坐标；正常路径用 _find_select_circle 动态定位。
FIRST_IMG_CIRCLE = Rect(198, 236, 40, 40)

PHONE_IMG_DIR = "/sdcard/Pictures"      # 图片 push 目录（相册可扫到）
PHONE_FILE_DIR = "/sdcard/Download"     # 文件 push 目录（文件选择器可找到）


# ------------------------------------------------------------------ CV 选图
def _find_select_circles(img):
    """检测相册选择页里**所有**"选择圆圈"，返回 [(cx, cy), ...]（按位置排序）。

    微信相册每张缩略图右上角有一个白色空心圆环（勾选圆圈）。
    特征：圆环描边亮（白色）、圆心暗（空心）——以此与缩略图内容里
    的实心圆/暗圆/文字区分。霍夫圆检测候选后按该特征过滤。

    2026-08-13 真机标定：选择圆圈中心约 (218,256) r≈30，环亮~160、
    圆心亮~13、差~148；缩略图内容里的圆均被该特征正确排除。
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1, minDist=50,
                               param1=80, param2=25,
                               minRadius=15, maxRadius=45)
    if circles is None:
        return []
    cands = []
    for x, y, r in circles[0]:
        x, y, r = int(x), int(y), int(r)
        # 圆环亮度（半径 r 的描边）
        ring_mask = np.zeros(gray.shape, np.uint8)
        cv2.circle(ring_mask, (x, y), r, 255, 2)
        ring = gray[ring_mask > 0]
        # 圆心亮度（半径 0.3r 内）
        rr = max(1, int(r * 0.3))
        y0, y1 = max(0, y - rr), min(gray.shape[0], y + rr)
        x0, x1 = max(0, x - rr), min(gray.shape[1], x + rr)
        center = gray[y0:y1, x0:x1]
        if len(ring) == 0 or center.size == 0:
            continue
        ring_b = float(ring.mean())
        center_b = float(center.mean())
        # 空心圆环：圆环足够亮，且明显亮于圆心
        if ring_b > 150.0 and (ring_b - center_b) > 30.0:
            cands.append((x, y))
    cands.sort(key=lambda p: (p[1], p[0]))   # 按 (y, x) 排序 = 网格顺序
    return cands


def _find_select_circle(img):
    """返回第一张图（最左上角）的选择圆圈位置 (cx, cy)，找不到 None。"""
    circles = _find_select_circles(img)
    return circles[0] if circles else None


def _clear_album(dev):
    """清空手机相册：删除公共图片目录的文件 + MediaStore 记录。

    目的（2026-08-13 用户定方案）：默认保持手机相册全空，每次只发一个。
    发送前清空相册 → push 的图就是唯一第一张 → CV 检测第一张圆圈即可
    正确选中，不依赖"push 图按时间排第一"这个不可靠假设（此前曾因相册
    残留截图/旧图导致选错图）。
    """
    dev._shell("find /sdcard/Pictures /sdcard/DCIM -type f -delete")
    dev._shell("content delete --uri content://media/external/images/media")


# ------------------------------------------------------------------ 通用辅助
def _fail(step, error, dev=None, phone_path=None, tmp_path=None):
    log.warning("media send fail @%s: %s", step, error)
    if dev is not None and phone_path:
        _cleanup(dev, phone_path, tmp_path)
    return {"ok": False, "step": step, "error": error}


def _cleanup(dev, phone_path, tmp_path=None):
    """删除手机上的临时文件（本地临时文件由调用方持有，不在这里删）。"""
    try:
        dev._shell(f"rm -f {shlex.quote(phone_path)}", timeout=5)
    except Exception as e:
        log.warning("cleanup phone file failed: %s", e)


def _push_to_phone(dev, local_path, phone_path):
    """adb push + MediaStore 刷新广播（不刷新相册/文件管理器看不到）。"""
    dev._run(["push", local_path, phone_path], timeout=30)
    dev._shell("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
               f"-d file://{shlex.quote(phone_path)}", timeout=10)
    time.sleep(1.5)


def _ocr_find(dev, keyword, ymin=0, ymax=9999):
    """当前截图全图 OCR 找关键词，返回 (cx, cy)；ymin/ymax 限定纵向区域，
    避免聊天内容里的同文误命中。"""
    try:
        for it in run_ocr(dev.capture_bytes()):
            if keyword in it["text"] and ymin <= it["cy"] <= ymax:
                return (int(it["cx"]), int(it["cy"]))
    except Exception:
        log.exception("ocr_find(%r) failed", keyword)
    return None


def _input_bar_focused(dev):
    """输入栏是否聚焦态：OCR 底部 "ADB Keyboard {ON}" 细条（y>=2100）。"""
    return _ocr_find(dev, "ADB Keyboard", ymin=2100) is not None


def _open_plus_panel(dev):
    """打开加号面板：先看不否已开；没开按聚焦态点 ⊕，坐标态判错兜底换另一态再试。
    返回 True/False（每步都有 OCR/网格验证，不盲点）。"""
    if plus_panel.plus_panel_items(dev)["success"]:
        return True
    focused = _input_bar_focused(dev)
    dev.tap_rect(CHAT_PLUS_FOCUSED if focused else layout.CHAT_PLUS)
    dev.wait_random(1000, 1500)
    if plus_panel.plus_panel_items(dev)["success"]:
        return True
    # 坐标态判错兜底：换另一个态再试一次（仍有面板检测验证）
    dev.tap_rect(layout.CHAT_PLUS if focused else CHAT_PLUS_FOCUSED)
    dev.wait_random(1000, 1500)
    return plus_panel.plus_panel_items(dev)["success"]


# ------------------------------------------------------------------ 发图片
def send_image(dev, path):
    """把本地图片发到当前聊天页。返回 {"ok": bool, "step": ..., ...}。

    调用方须保证已在目标聊天页（bundle_sender 负责 enter_session）。
    """
    # 1. 校验本地图片（契约：只接受本机绝对路径，禁止 url）
    if not path or not os.path.isabs(path):
        return _fail("args", f"path 必须是本机绝对路径: {path!r}")
    if not os.path.isfile(path):
        return _fail("args", f"本地图片不存在: {path}")
    if cv2.imread(path) is None:
        return _fail("args", f"文件不是有效图片: {path}")

    # 2. 清空相册：保证 push 的图是唯一第一张（不依赖"按时间排第一"的假设）
    try:
        _clear_album(dev)
    except Exception as e:
        log.warning("清空相册失败（继续发送）: %s", e)

    # 3. push + 媒体扫描
    ext = os.path.splitext(path)[1].lower() or ".jpg"
    phone_path = f"{PHONE_IMG_DIR}/agent_send{ext}"
    try:
        _push_to_phone(dev, path, phone_path)
    except Exception as e:
        return _fail("push", f"push 图片到手机失败: {e}", dev, phone_path)

    # 4. 开加号面板（两态 ⊕ 坐标）
    if not _open_plus_panel(dev):
        return _fail("plus_panel", "加号面板未打开", dev, phone_path)

    # 5. 点"相册"卡片（plus_panel 网格检测，第一页必有）
    r = plus_panel.plus_panel_tap(dev, "相册")
    if not r["success"]:
        return _fail("album_tap", r["error"], dev, phone_path)
    dev.wait_random(1000, 1500)

    # 6. 验证相册选择页已开，再用 CV 定位第一张图的选择圆圈。
    #    相册已在第 2 步清空，push 的图就是唯一第一张，检测第一张圆圈即正确。
    if not _ocr_find(dev, "图片和视频"):
        return _fail("album_open", "相册选择页未打开", dev, phone_path)
    pos = _find_select_circle(dev.capture_bytes())
    if pos is None:
        # CV 检测失败（布局异常）回退到真机标定的硬编码坐标
        log.warning("select circle 未检测到，回退硬编码坐标")
        pos = FIRST_IMG_CIRCLE.center
    else:
        log.info("select circle 定位: (%d,%d)", int(pos[0]), int(pos[1]))
    dev.tap_rect(Rect(int(pos[0]) - 20, int(pos[1]) - 20, 40, 40))
    dev.wait_random(600, 1000)

    # 7. OCR 验证选中（出现"发送(N)"按钮），才点发送
    send_pos = _ocr_find(dev, "发送", ymin=1900)
    if not send_pos:
        return _fail("select", "未选中图片（发送按钮未出现）", dev, phone_path)
    dev.tap_rect(Rect(send_pos[0] - 40, send_pos[1] - 30, 80, 60))
    dev.wait_random(1200, 1800)

    # 8. 清理手机临时图片
    _cleanup(dev, phone_path)
    log.info("image sent: %s", path)
    return {"ok": True, "step": "sent", "path": path}


# ------------------------------------------------------------------ 发文件
# SAF"最近"列表第一行（浏览应用区之下、列表区首行，OnePlus 6T 实测）
SAF_FIRST_ROW = Rect(150, 880, 700, 130)


def send_file(dev, path):
    """把本地文件发到当前聊天页（加号面板"文件" → 手机文件 → SAF 最近列表）。

    返回 {"ok": bool, "step": ..., ...}。调用方须已在目标聊天页。
    文件选择策略：push 后 touch 刷新 mtime，SAF"最近"列表第一行必是它，
    直接点第一行（2026-08-08 与用户确认的交互路径）。"""
    # 1. 校验本地文件
    if not path or not os.path.isabs(path):
        return _fail("args", f"path 必须是本机绝对路径: {path!r}")
    if not os.path.isfile(path):
        return _fail("args", f"本地文件不存在: {path}")

    # 2. push + 媒体扫描 + touch（mtime 刷成现在，SAF 最近列表排第一）。
    # 保留原文件名：文件卡片显示的就是手机上的文件名（2026-08-09 实测
    # 固定名 agent_file.txt 发到群里 recipients 看不懂）
    phone_path = f"{PHONE_FILE_DIR}/{os.path.basename(path)}"
    try:
        _push_to_phone(dev, path, phone_path)
        dev._shell(f"touch {shlex.quote(phone_path)}", timeout=5)
    except Exception as e:
        return _fail("push", f"push 文件到手机失败: {e}", dev, phone_path)

    # 3. 开加号面板（两态 ⊕ 坐标）
    if not _open_plus_panel(dev):
        return _fail("plus_panel", "加号面板未打开", dev, phone_path)

    # 4. 点"文件"卡片（第二页，plus_panel_tap 自动翻页）
    r = plus_panel.plus_panel_tap(dev, "文件")
    if not r["success"]:
        return _fail("file_tap", r["error"], dev, phone_path)
    dev.wait_random(1200, 1800)

    # 5. "选择文件"页 → "手机文件"标签 → "选取"按钮 → SAF 选择器
    if not _ocr_find(dev, "选择文件", ymax=300):
        return _fail("picker_open", "文件选择页未打开", dev, phone_path)
    tab = _ocr_find(dev, "手机文件", ymax=400)
    if not tab:
        return _fail("tab", "找不到'手机文件'标签", dev, phone_path)
    dev.tap_rect(Rect(tab[0] - 70, tab[1] - 40, 140, 80))
    dev.wait_random(800, 1200)
    pick = _ocr_find(dev, "选取")
    if not pick:
        return _fail("pick_btn", "找不到'选取'按钮", dev, phone_path)
    dev.tap_rect(Rect(pick[0] - 120, pick[1] - 50, 240, 100))
    dev.wait_random(1500, 2200)

    # 6. SAF 选择器：点"最近"列表第一行（刚 push+touch 的文件）
    if not (_ocr_find(dev, "最近", ymax=300)
            or _ocr_find(dev, "近期的文件")):
        return _fail("saf_open", "系统文件选择器未打开", dev, phone_path)
    dev.tap_rect(SAF_FIRST_ROW)
    dev.wait_random(1200, 1800)

    # 7. 选中确认面板（"已选中N个文件"是底部弹板，"发送"按钮在屏幕
    #    中上部 y~670 而不是底部，2026-08-08 真机实测）→ 点"发送"
    if _ocr_find(dev, "已选中"):
        send_pos = _ocr_find(dev, "发送")
    else:
        send_pos = _ocr_find(dev, "发送", ymin=1900)
    if not send_pos:
        return _fail("select", "未选中文件（发送按钮未出现）", dev, phone_path)
    dev.tap_rect(Rect(send_pos[0] - 40, send_pos[1] - 30, 80, 60))
    dev.wait_random(1200, 1800)

    # 8. 清理
    _cleanup(dev, phone_path)
    log.info("file sent: %s", path)
    return {"ok": True, "step": "sent", "path": path}
