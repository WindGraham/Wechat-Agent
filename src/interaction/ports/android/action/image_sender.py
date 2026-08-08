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

from ..device import layout
from ..device.random_touch import Rect
from ..perception.ocr_engine import run_ocr
from . import plus_panel

log = logging.getLogger("action.image_sender")

# ------------------------------------------------------------------ 坐标常量
# ⊕ 加号按钮：未聚焦用 layout.CHAT_PLUS（中心 1015,2185）；
# 聚焦态整条输入栏顶起 ~125px，⊕ 中心变为 (1015,2060)。
CHAT_PLUS_FOCUSED = Rect(960, 2005, 110, 110)

# 相册选择页第一张图的选择圆（网格 218+271·col, 256+272·row，旧仓库真机标定）
FIRST_IMG_CIRCLE = Rect(198, 236, 40, 40)

PHONE_IMG_DIR = "/sdcard/Pictures"      # 图片 push 目录（相册可扫到）
PHONE_FILE_DIR = "/sdcard/Download"     # 文件 push 目录（文件选择器可找到）


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

    # 2. push + 媒体扫描
    ext = os.path.splitext(path)[1].lower() or ".jpg"
    phone_path = f"{PHONE_IMG_DIR}/agent_send{ext}"
    try:
        _push_to_phone(dev, path, phone_path)
    except Exception as e:
        return _fail("push", f"push 图片到手机失败: {e}", dev, phone_path)

    # 3. 开加号面板（两态 ⊕ 坐标）
    if not _open_plus_panel(dev):
        return _fail("plus_panel", "加号面板未打开", dev, phone_path)

    # 4. 点"相册"卡片（plus_panel 网格检测，第一页必有）
    r = plus_panel.plus_panel_tap(dev, "相册")
    if not r["success"]:
        return _fail("album_tap", r["error"], dev, phone_path)
    dev.wait_random(1000, 1500)

    # 5. 验证相册选择页已开（标题"图片和视频"），再选第一张图
    if not _ocr_find(dev, "图片和视频"):
        return _fail("album_open", "相册选择页未打开", dev, phone_path)
    dev.tap_rect(FIRST_IMG_CIRCLE)
    dev.wait_random(600, 1000)

    # 6. OCR 验证选中（出现"发送(N)"按钮），才点发送
    send_pos = _ocr_find(dev, "发送", ymin=1900)
    if not send_pos:
        return _fail("select", "未选中图片（发送按钮未出现）", dev, phone_path)
    dev.tap_rect(Rect(send_pos[0] - 40, send_pos[1] - 30, 80, 60))
    dev.wait_random(1200, 1800)

    # 7. 清理手机临时图片
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
