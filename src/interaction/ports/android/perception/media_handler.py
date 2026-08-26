# -*- coding: utf-8 -*-
"""media_handler.py — 微信多媒体消息统一处置器。

把 media_classifier 输出的非 text 类型（link/media/sticker/card/red_packet 等）
转换成可入库的结构化内容。所有中间文件落盘 workspace，测试阶段不删除。

当前真机验证状态（OnePlus 6T / 1080x2340 / 微信 8.0.76）：
- 链接：复制 → 输入框粘贴 → 底部区域 OCR → 清空，已验证。
- 图片：点击 → 查看器长按 → 保存 → adb pull → 删源文件，已验证。
- 表情包：点击 → 详情页 → 裁上半屏最大非背景块，已验证。
- 聊天记录转发卡：点击 → 顶部标题「xxx 的聊天记录」→ 滚动 OCR，已验证。
- 红包：仅记录，不自动点开。

设计原则：
1. 任何处置前后都必须能回到原聊天页原位置。
2. 任何异常保留最后屏幕截图到 workspace/collect_debug/errors/。
3. 操作超时必须有兜底：截图 → back → 标记失败。
"""

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import cv2
import numpy as np

from ..device.device_ctl import DeviceCtl
from ..device.layout import (
    CHAT_INPUT_BAR, CHAT_INPUT_BAR_FOCUSED, CHAT_BACK,
    Rect,
)
from .ocr_engine import run_ocr, ocr_region
from .media_classifier import classify_segment

log = logging.getLogger("perception.media_handler")

PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", ".."))

WORKSPACE_ROOT = os.path.join(PROJECT_ROOT, "workspace")
MEDIA_ROOT = os.path.join(WORKSPACE_ROOT, "media")
DEBUG_ROOT = os.path.join(WORKSPACE_ROOT, "collect_debug")

SCREEN_W, SCREEN_H = 1080, 2340

# 聊天记录详情页：时间戳格式（右上角，如 8月13日01:08:33）与顶部 chrome 区
_RECORD_TS_RE = re.compile(r"^\d{1,2}月\d{1,2}日\d{1,2}[:：]\d{2}[:：]\d{2}$")
_RECORD_TOP_Y = 200      # 状态栏/标题栏噪声，跳过
_RECORD_CONTENT_X_MIN = 900  # 内容区右界，过滤右侧杂项


# --------------------------------------------------------------------------- 工具

def _safe_name(s: str) -> str:
    """文件名安全化：保留中文/英文/数字/下划线，其余替换。"""
    return "".join(ch if ("\u4e00" <= ch <= "\u9fff") or ch.isalnum() or ch in "_-" else "_"
                   for ch in (s or "")).strip("_") or "unknown"


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _bbox_center(bbox: Tuple[int, int, int, int]) -> Tuple[int, int]:
    x, y, w, h = bbox
    return int(x + w / 2), int(y + h / 2)


def _bbox_rect(bbox: Tuple[int, int, int, int], pad: int = 0) -> Rect:
    x, y, w, h = bbox
    return Rect(
        max(0, x - pad), max(0, y - pad),
        min(SCREEN_W, w + pad * 2), min(SCREEN_H, h + pad * 2),
    )


def _join_url_lines(texts: List[str]) -> Optional[str]:
    """OCR 可能把一行长 URL 截成两行，按行首/行尾拼接。"""
    url = "".join(t.strip() for t in texts if t.strip())
    url = url.replace(" ", "").replace("\n", "")
    # 微信分享链接常见前缀
    if re.search(r"https?://[^\s\"'<>]+", url, re.IGNORECASE):
        m = re.search(r"(https?://[^\s\"'<>]+)", url, re.IGNORECASE)
        return m.group(1)
    return None


@dataclass
class MediaTask:
    msg_id: str
    msg_type: str                       # classifier 输出
    bbox: Tuple[int, int, int, int]     # 单条消息在屏幕上的包围框 (x,y,w,h)
    screen_path: str                    # 当前整屏截图路径
    group_name: str
    sender_hint: str = ""               # 发送者昵称/头像提示
    extra: dict = field(default_factory=dict)


@dataclass
class MediaResult:
    msg_id: str
    msg_type: str
    content: Any
    raw_files: List[str] = field(default_factory=list)
    success: bool = False
    error: Optional[str] = None
    run_dir: Optional[str] = None           # 本次处置的落盘目录（写 manifest.json）

    def to_message_entry(self):
        """转成 message_log 可接受的 dict（由接入层决定如何 append）。"""
        return {
            "sender": "",
            "is_mine": False,
            "content": self.content if isinstance(self.content, str) else json.dumps(self.content, ensure_ascii=False),
            "content_type": self.msg_type,
            "complete": 1,
            "media_path": ";".join(self.raw_files),
        }


class MediaHandler:
    """多媒体消息统一处置器。"""

    def __init__(self, dev: DeviceCtl):
        self.dev = dev

    # ------------------------------------------------------------------ 入口

    def handle(self, task: MediaTask) -> MediaResult:
        log.info("[media] handle %s id=%s", task.msg_type, task.msg_id)
        try:
            if task.msg_type == "text":
                result = self._handle_text(task)
            elif task.msg_type in ("link", "card", "media", "sticker"):
                # 这些类型要点击屏幕：先确认当前就在目标聊天页。
                # 教训（2026-08-26）：首页残留状态 + 旧 bbox = 乱点进别人会话。
                if not self._verify_in_chat(task):
                    result = MediaResult(
                        msg_id=task.msg_id, msg_type=task.msg_type,
                        content=None,
                        error=f"当前不在目标聊天页（期望 {task.group_name}），拒绝点击")
                elif task.msg_type in ("link", "card"):
                    # 先尝试链接，若页面不是 webview 再按聊天记录卡/文件卡处理
                    result = self._handle_link_or_card(task)
                elif task.msg_type == "media":
                    result = self._handle_media(task)
                else:
                    result = self._handle_sticker(task)
            elif task.msg_type == "red_packet":
                result = self._handle_red_packet(task)
            else:
                result = MediaResult(
                    msg_id=task.msg_id, msg_type=task.msg_type,
                    content=None, error=f"未实现类型: {task.msg_type}")
        except Exception as e:
            log.exception("[media] handle failed")
            self._save_error_shot(task, f"{task.msg_type}_exception")
            self._return_to_chat()
            result = MediaResult(
                msg_id=task.msg_id, msg_type=task.msg_type,
                content=None, error=str(e))
        self._write_manifest(task, result)
        return result

    def _verify_in_chat(self, task: MediaTask) -> bool:
        """点击前校验：当前页标题与目标会话名一致（OCR 顶部标题条）。

        标题可能带成员数后缀（如「陈曦猫猫群(9)」），用规范化后双向包含判定。
        OCR 异常时放行（不阻塞主流程，后续页面签名仍会兜底）。
        """
        try:
            img = self.dev.capture_bytes()
            h = img.shape[0]
            top = img[0:int(h * 0.12), :]
            title = "".join(it["text"] for it in run_ocr(top))
        except Exception as e:  # noqa: BLE001
            log.warning("[media] verify_in_chat OCR failed: %s, 放行", e)
            return True

        def _norm(s):
            return re.sub(r"[\s　()（）\d]+", "", s or "")

        nt, ng = _norm(title), _norm(task.group_name)
        ok = bool(nt) and bool(ng) and (ng in nt or nt in ng)
        if not ok:
            log.warning("[media] verify_in_chat: title=%r 不含 %r，拒绝点击",
                        title, task.group_name)
            self._save_frame(img, task, "not_in_chat")
        return ok

    def _write_manifest(self, task: MediaTask, result: MediaResult):
        """每个消息处置落盘 manifest.json（§5.3）。run_dir 为空则跳过。"""
        run_dir = result.run_dir
        if not run_dir:
            return
        try:
            content = result.content
            if not isinstance(content, (str, dict, list, type(None))):
                content = str(content)
            manifest = {
                "msg_id": task.msg_id,
                "type": result.msg_type,
                "group": task.group_name,
                "bbox": list(task.bbox),
                "files": result.raw_files,
                "success": result.success,
                "result": content,
                "error": result.error,
            }
            with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            log.warning("[media] manifest write failed: %s", e)

    # ------------------------------------------------------------------ 文本

    def _handle_text(self, task: MediaTask) -> MediaResult:
        """文本气泡直接 OCR（兼容入口统一调用）。"""
        img = cv2.imread(task.screen_path)
        if img is None:
            return MediaResult(msg_id=task.msg_id, msg_type="text",
                               content="", error="无法读取截图")
        x, y, w, h = task.bbox
        crop = img[y:y + h, x:x + w]
        text = ocr_region(img, task.bbox)
        crop_path = self._save_crop(crop, task, "text_crop")
        return MediaResult(msg_id=task.msg_id, msg_type="text",
                           content=text, raw_files=[crop_path], success=True)

    # ------------------------------------------------------------------ 链接 / 卡

    def _handle_link_or_card(self, task: MediaTask) -> MediaResult:
        """点击后按页面签名分流：webview=链接，聊天记录=转发卡，其余=未处理卡。"""
        cx, cy = _bbox_center(task.bbox)
        self.dev.tap(cx, cy)
        self.dev.wait_random(1200, 1800)

        img = self.dev.capture_bytes()
        sig, img = self._detect_page_signature(img)
        log.info("[media] after tap signature=%s", sig)

        if sig == "chat_record":
            return self._read_chat_record(task, img)
        if sig == "file_card":
            return self._handle_file(task, img)
        # webview 首屏不显示「复制链接」（它在 ⋯ 菜单里），固定签名常检测不到 →
        # 一律尝试链接流：_read_link_from_webview 内找不到「复制链接」会优雅退出。
        return self._read_link_from_webview(task, img)

    def _read_link_from_webview(self, task: MediaTask, first_frame: np.ndarray) -> MediaResult:
        """尝试从当前页取链接：点 ⋯ → 找「复制链接」→ 直读剪贴板 → 回会话。

        webview 首屏不显示「复制链接」（在 ⋯ 菜单），故不做前置签名判断；这里
        点 ⋯ 后 OCR 找「复制链接」，找不到即认定非 webview，优雅退出（不读脏剪贴板）。
        直读剪贴板失败（非 root / 空 / 异常）回退「粘贴 → 底部 OCR → 清空」。
        """
        run_dir = _ensure_dir(os.path.join(DEBUG_ROOT, "links", f"{task.msg_id}_{_ts()}"))
        files: List[str] = []

        # 1) 点右上角更多（⋯）
        self.dev.tap_rect(Rect(950, 95, 110, 110))
        self.dev.wait_random(800, 1200)

        # 2) OCR 找「复制链接」；找不到 = 非 webview，优雅退出
        img = self.dev.capture_bytes()
        items = run_ocr(img)
        copy_pos = self._find_text_opt(items, ("复制链接",))
        if copy_pos is None:
            log.info("[media] 非 webview（无「复制链接」），优雅退出")
            self.dev.back(); self.dev.wait_random(600, 1000)   # 关 ⋯ 菜单
            self.dev.back(); self.dev.wait_random(800, 1200)   # 回聊天页
            return MediaResult(
                msg_id=task.msg_id, msg_type="card",
                content=None,
                raw_files=[self._save_frame(img, task, "not_webview")],
                error="非 webview 页面（无「复制链接」）")

        self.dev.tap(*copy_pos)
        self.dev.wait_random(800, 1200)

        # 3) 直读剪贴板（此时前台仍是微信，聚焦 App 身份可读）
        url = None
        try:
            url = self.dev.read_clipboard()
            if url:
                log.info("[media] link read_clipboard: %s", url)
        except Exception as e:  # noqa: BLE001
            log.warning("[media] read_clipboard 失败: %s，回退 OCR", e)

        # 4) 回会话（webview → 聊天页）
        self.dev.wait_random(400, 700)
        self.dev.back()
        self.dev.wait_random(800, 1200)
        self.dev.back()
        self.dev.wait_random(800, 1200)

        if url:
            with open(os.path.join(run_dir, "url.txt"), "w", encoding="utf-8") as f:
                f.write(url)
            return MediaResult(
                msg_id=task.msg_id, msg_type="link",
                content=url, raw_files=files, success=True, run_dir=run_dir)

        # 5) 回退：粘贴 → 底部 OCR → 清空
        return self._paste_ocr_link_fallback(task, run_dir, files)

    def _paste_ocr_link_fallback(self, task: MediaTask, run_dir: str, files: list) -> MediaResult:
        """read_clipboard 不可用时的回退：粘贴到输入框 + 底部 OCR + 清空。"""
        # 1) 粘贴到输入框
        self.dev.tap_rect(CHAT_INPUT_BAR)
        self.dev.wait_random(400, 700)
        self.dev.long_press_rect(CHAT_INPUT_BAR_FOCUSED)
        self.dev.wait_random(600, 1000)
        paste_pos = self._find_text_on_screen("粘贴", fallback=(110, 1970))
        self.dev.tap(*paste_pos)
        self.dev.wait_random(1000, 1500)

        # 2) 截图并 OCR 底部输入框
        screen = self.dev.capture_bytes()
        screen_path = os.path.join(run_dir, "screen_after_paste.png")
        cv2.imwrite(screen_path, screen)
        files.append(screen_path)

        url = self._ocr_input_box_url(screen)
        log.info("[media] link OCR 结果: %s", url)

        # 3) 清空输入框（ADBKeyBoard 广播优先，失败用 Ctrl+A+DEL）
        self._clear_input_box()

        if url:
            with open(os.path.join(run_dir, "url.txt"), "w", encoding="utf-8") as f:
                f.write(url)
            return MediaResult(
                msg_id=task.msg_id, msg_type="link",
                content=url, raw_files=files, success=True, run_dir=run_dir)

        return MediaResult(
            msg_id=task.msg_id, msg_type="link",
            content=None, raw_files=files,
            error="read_clipboard 与输入框 OCR 均未识别到 URL")

    def _ocr_input_box_url(self, img: np.ndarray) -> Optional[str]:
        h, w = img.shape[:2]
        # 未聚焦输入框 y≈2140~2230；聚焦后 ADB 细条顶起，输入框 y≈2015~2120。
        # 取底部 14% 区域可覆盖两态。
        y1 = int(h * 0.83)
        crop = img[y1:h, :]
        items = run_ocr(crop)
        texts = [it["text"] for it in items]
        return _join_url_lines(texts)

    def _clear_input_box(self):
        """清空输入框，绝不触发发送。"""
        try:
            self.dev.clear_text()
        except Exception:
            pass
        self.dev.wait_random(200, 400)
        # 二次确认：聚焦 → Ctrl+A → DEL
        self.dev.tap_rect(CHAT_INPUT_BAR)
        self.dev.wait_random(200, 400)
        self.dev._shell("input keycombination 113 29")  # Ctrl+A
        self.dev.wait_random(200, 400)
        self.dev._shell("input keyevent 67")            # DEL
        self.dev.wait_random(400, 600)

    # ------------------------------------------------------------------ 图片 / 视频

    def _handle_media(self, task: MediaTask) -> MediaResult:
        """media 类型：先点开后按页面签名分图片/视频。"""
        cx, cy = _bbox_center(task.bbox)
        self.dev.tap(cx, cy)
        self.dev.wait_random(1500, 2200)

        img = self.dev.capture_bytes()
        sig, img = self._detect_page_signature(img)
        log.info("[media] media after tap signature=%s", sig)

        if sig in ("photo_viewer", "video_viewer", "media_viewer"):
            # media_viewer（黑底全屏查看器，无文字标签）也要走保存流：
            # 图片/视频的区分放到长按后的 action sheet（保存图片/保存视频）再定。
            return self._save_photo_or_video(task, is_video=(sig == "video_viewer"))
        if sig == "sticker_detail":
            return self._handle_sticker_detail(task, img)
        if sig == "webview":
            # 链接卡被分成了 media：点开是 webview，走链接读取
            return self._read_link_from_webview(task, img)

        err_path = self._save_frame(img, task, "unknown_media")
        self.dev.back()
        self.dev.wait_random(600, 1000)
        return MediaResult(
            msg_id=task.msg_id, msg_type="media",
            content=None, raw_files=[err_path],
            error=f"未知媒体页面 signature={sig}")

    def _save_photo_or_video(self, task: MediaTask, is_video: Optional[bool] = None) -> MediaResult:
        """全屏查看器（黑底）保存流。

        is_video=None 时不预设类型：长按出 action sheet 后按「保存图片/保存视频」
        关键词判定（查看器初始界面是无文字标签的 4 图标，OCR 无法区分）。
        """
        run_dir = _ensure_dir(os.path.join(
            MEDIA_ROOT, "media_saves",
            f"{task.msg_id}_{_ts()}"))
        files: List[str] = []

        # 0) 保存前快照目标目录（差集法认新文件，防拉到无关旧文件）；
        #    此时还不知道是图片还是视频，两个目录都快照。
        before_img = set(self._list_dir("/sdcard/Pictures/WeiXin"))
        before_vid = set(self._list_dir("/sdcard/Movies/WeiXin"))

        # 1) 长按图片/视频中心 → action sheet
        self.dev.long_press_rect(Rect(SCREEN_W // 2 - 50, SCREEN_H // 2 - 50, 100, 100))
        self.dev.wait_random(1000, 1500)

        # 2) 截图 + OCR action sheet，判定类型并定位「保存图片/保存视频」
        sheet = self.dev.capture_bytes()
        sheet_path = os.path.join(run_dir, "action_sheet.png")
        cv2.imwrite(sheet_path, sheet)
        files.append(sheet_path)
        items = run_ocr(sheet)
        save_pos = None
        if is_video is None or is_video:
            save_pos = self._find_text_opt(items, "保存视频")
            if save_pos is not None:
                is_video = True
        if save_pos is None:
            save_pos = self._find_text_opt(items, "保存图片")
            if save_pos is not None:
                is_video = False
        if save_pos is None:
            self.dev.back()
            self.dev.wait_random(600, 1000)
            return MediaResult(
                msg_id=task.msg_id, msg_type="media",
                content=None, raw_files=files,
                error="长按后 action sheet 未出现「保存图片/保存视频」",
                run_dir=run_dir)
        self.dev.tap(*save_pos)
        self.dev.wait_random(800, 1200)

        # 2b) 重复保存确认框：微信按消息记忆保存历史，同一消息第二次保存会弹
        # 「已保存过图片到系统相册 / 再次保存 / 取消」。截图识别，命中则点「再次保存」。
        after = self.dev.capture_bytes()
        confirm_path = os.path.join(run_dir, "after_save_tap.png")
        cv2.imwrite(confirm_path, after)
        files.append(confirm_path)
        rep_pos = self._find_text_opt(run_ocr(after), "再次保存")
        if rep_pos is not None:
            log.info("[media] repeat-save confirm dialog, tap 再次保存")
            self.dev.tap(*rep_pos)
            self.dev.wait_random(800, 1200)
        else:
            # 未弹确认框：after_save_tap.png 与 action_sheet 重复，不留垃圾
            files.remove(confirm_path)
            try:
                os.remove(confirm_path)
            except OSError:
                pass

        # 3) 轮询手机目录等待文件出现（差集法，只认快照之外的新名字）
        src_dir = "/sdcard/Movies/WeiXin" if is_video else "/sdcard/Pictures/WeiXin"
        src_path = self._wait_for_new_file(
            src_dir, timeout=30,
            exclude=before_vid if is_video else before_img)
        if not src_path:
            self.dev.back()
            self.dev.wait_random(600, 1000)
            return MediaResult(
                msg_id=task.msg_id, msg_type="video" if is_video else "image",
                content=None, raw_files=files,
                error=f"未在 {src_dir} 找到保存的文件", run_dir=run_dir)

        # 4) pull 到电脑
        ext = os.path.splitext(src_path)[1] or (".mp4" if is_video else ".jpg")
        local_name = f"{task.msg_id}{ext}"
        local_path = os.path.join(run_dir, local_name)
        self.dev._run(["pull", src_path, local_path])
        files.append(local_path)

        # 5) 删除手机源文件
        self.dev._shell(f"rm -f {src_path}")

        # 6) 回会话（只按一次 back，按两次会退到首页）
        self.dev.back()
        self.dev.wait_random(800, 1200)

        return MediaResult(
            msg_id=task.msg_id,
            msg_type="video" if is_video else "image",
            content=local_path,
            raw_files=files,
            success=True,
            run_dir=run_dir)

    # ------------------------------------------------------------------ 表情包

    def _handle_sticker(self, task: MediaTask) -> MediaResult:
        """表情包：点击 → 详情页 → 裁上半屏最大非背景块。"""
        cx, cy = _bbox_center(task.bbox)
        self.dev.tap(cx, cy)
        self.dev.wait_random(1200, 1800)
        img = self.dev.capture_bytes()
        return self._handle_sticker_detail(task, img)

    def _handle_sticker_detail(self, task: MediaTask, img: np.ndarray) -> MediaResult:
        run_dir = _ensure_dir(os.path.join(MEDIA_ROOT, "stickers", f"{task.msg_id}_{_ts()}"))

        # 保存详情页整图
        detail_path = os.path.join(run_dir, "detail.png")
        cv2.imwrite(detail_path, img)

        # 上半屏
        h, w = img.shape[:2]
        upper = img[0:int(h * 0.65), :]
        sticker_path = os.path.join(run_dir, "sticker.png")
        extracted = self._extract_largest_non_bg(upper, bg_bgr=(92, 92, 92), tol=20)
        if extracted is not None and extracted.size > 0:
            cv2.imwrite(sticker_path, extracted)
            files = [detail_path, sticker_path]
        else:
            files = [detail_path]
            sticker_path = None

        # 回会话
        self.dev.back()
        self.dev.wait_random(600, 1000)

        return MediaResult(
            msg_id=task.msg_id, msg_type="sticker",
            content=sticker_path,
            raw_files=files,
            success=sticker_path is not None,
            run_dir=run_dir)

    def _extract_largest_non_bg(self, img: np.ndarray, bg_bgr: Tuple[int, int, int],
                                tol: int = 20) -> Optional[np.ndarray]:
        """提取最大非背景连通块（表情包详情页用）。"""
        bg = np.array(bg_bgr, dtype=np.uint8)
        mask = cv2.inRange(img, bg - tol, bg + tol)
        fg = cv2.bitwise_not(mask)
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        cnt = max(contours, key=cv2.contourArea)
        if cv2.contourArea(cnt) < 1000:
            return None
        x, y, w, h = cv2.boundingRect(cnt)
        return img[y:y + h, x:x + w]

    # ------------------------------------------------------------------ 聊天记录转发

    def _read_chat_record(self, task: MediaTask, first_frame: np.ndarray) -> MediaResult:
        run_dir = _ensure_dir(os.path.join(
            MEDIA_ROOT, "chat_records", f"{task.msg_id}_{_ts()}"))
        screens_dir = _ensure_dir(os.path.join(run_dir, "screens"))
        files: List[str] = []

        # 保存首屏
        first_path = os.path.join(screens_dir, "screen_000.png")
        cv2.imwrite(first_path, first_frame)
        files.append(first_path)

        records = []
        # 先解析首屏
        records.extend(self._parse_chat_record_screen(first_frame))

        # 向上滚动继续抓（聊天记录详情页可滚动）
        for i in range(1, 20):
            before = self.dev.capture_bytes()
            self.dev.swipe_zone(Rect(0, 300, 1080, 1650), direction="down",
                                length_ratio=(0.6, 0.8))
            self.dev.wait_random(800, 1500)
            after = self.dev.capture_bytes()
            path = os.path.join(screens_dir, f"screen_{i:03d}.png")
            cv2.imwrite(path, after)
            files.append(path)

            # 用直方图或特征点判断是否到底
            if self._frames_similar(before, after):
                log.info("[media] chat record scroll reached bottom")
                break
            records.extend(self._parse_chat_record_screen(after))

        # 去重并按时间排序
        seen = set()
        unique = []
        for r in records:
            key = (r.get("time"), r.get("sender"), r.get("content"))
            if key not in seen:
                seen.add(key)
                unique.append(r)

        result = {
            "title": task.extra.get("title", ""),
            "records": unique,
        }
        with open(os.path.join(run_dir, "record.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        # 返回聊天页
        self.dev.back()
        self.dev.wait_random(800, 1200)

        return MediaResult(
            msg_id=task.msg_id, msg_type="chat_record",
            content=result,
            raw_files=files,
            success=True,
            run_dir=run_dir)

    def _parse_chat_record_screen(self, img: np.ndarray) -> List[dict]:
        """聊天记录详情页单屏解析（两遍法）。

        版式：每条 = 右侧时间戳(`8月13日01:08:33`, cx>600) 为「头」；
        同 cy 左侧( cx<320 ) 短行为发送者名；头与下一头之间 (cy 递增) 为内容。
        过滤状态栏/标题栏 (y<_RECORD_TOP_Y)；图片类无文本消息自然跳过。
        """
        items = run_ocr(img)
        items = [it for it in items if it["box"][1] > _RECORD_TOP_Y
                 and (it.get("text") or "").strip()]
        for it in items:
            it["_t"] = it["text"].strip()
        headers = [it for it in items
                   if _RECORD_TS_RE.match(it["_t"].replace(" ", "")) and it["cx"] > 600]
        headers.sort(key=lambda it: it["cy"])

        records: List[dict] = []
        for i, h in enumerate(headers):
            cy, t = h["cy"], h["_t"].replace(" ", "")
            # 发送者：同 cy 带内左侧短行
            sender = ""
            for it in items:
                if (it is not h and it["cx"] < 320 and abs(it["cy"] - cy) <= 35
                        and not _RECORD_TS_RE.match(it["_t"].replace(" ", ""))):
                    sender = it["_t"]
                    break
            next_cy = headers[i + 1]["cy"] if i + 1 < len(headers) else 99999
            content = "\n".join(
                it["_t"] for it in items
                if cy + 45 < it["cy"] < next_cy - 30 and it["cx"] < _RECORD_CONTENT_X_MIN
            ).strip()
            records.append({"time": t, "sender": sender, "content": content})
        return records

    def _frames_similar(self, a: np.ndarray, b: np.ndarray, thr: float = 0.98) -> bool:
        if a.shape != b.shape:
            return False
        gray_a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        gray_b = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray_a, gray_b)
        sim = 1.0 - (np.mean(diff) / 255.0)
        return sim > thr

    # ------------------------------------------------------------------ 红包

    def _handle_red_packet(self, task: MediaTask) -> MediaResult:
        """红包不自动点开，仅记录事件。"""
        return MediaResult(
            msg_id=task.msg_id, msg_type="red_packet",
            content="[微信红包]",
            success=True)

    # ------------------------------------------------------------------ 文件卡

    def _handle_file(self, task: MediaTask, first_frame: np.ndarray) -> MediaResult:
        """文件卡：预览页 → 右上角 ⋯ → 菜单点「保存」→ /sdcard/Download/WeiXin/ 取回。

        真机标定（2026-08-26，.md 样本）：
        - 不支持预览的类型页面只有「用其他应用打开」（死路，进系统选择器），
          真正的保存入口在 ⋯ 菜单里（保存/收藏/浮窗/更多打开方式）。
        - ⋯ → 保存 后文件落 /sdcard/Download/WeiXin/<原文件名>。
        - 取文件必须用「先快照文件名集合 → 等新名字出现」差集法，不能取
          目录最新文件（会拉到无关旧文件）。
        """
        run_dir = _ensure_dir(os.path.join(MEDIA_ROOT, "files", f"{task.msg_id}_{_ts()}"))
        files: List[str] = []

        # 记录首屏（文件卡预览页）
        first_path = os.path.join(run_dir, "file_card.png")
        cv2.imwrite(first_path, first_frame)
        files.append(first_path)

        save_dir = "/sdcard/Download/WeiXin"

        # 1) 右上角 ⋯ 菜单
        self.dev.tap_rect(Rect(950, 95, 110, 110))
        self.dev.wait_random(800, 1200)

        # 2) OCR 菜单找「保存」（精确匹配，避开「保存图片/保存到手机」类）
        menu = self.dev.capture_bytes()
        menu_path = os.path.join(run_dir, "menu.png")
        cv2.imwrite(menu_path, menu)
        files.append(menu_path)
        items = run_ocr(menu)
        save_pos = None
        for it in items:
            if it["text"].strip() == "保存":
                save_pos = (int(it["cx"]), int(it["cy"]))
                break
        if save_pos is None:
            save_pos = self._find_text_opt(items, ("保存到手机", "保存"))

        if save_pos is None:
            log.warning("[media] file menu has no 保存 entry")
            self.dev.back(); self.dev.wait_random(600, 1000)   # 关菜单
            self.dev.back(); self.dev.wait_random(800, 1200)   # 回聊天页
            return MediaResult(
                msg_id=task.msg_id, msg_type="file",
                content="[文件]", raw_files=files,
                error="⋯ 菜单未找到「保存」入口", run_dir=run_dir)

        # 3) 先快照目录，再点保存，等新文件名出现
        before = set(self._list_dir(save_dir))
        self.dev.tap(*save_pos)
        self.dev.wait_random(1500, 2500)
        src_path = self._wait_for_new_file(save_dir, exclude=before, timeout=30)

        local_path = None
        if src_path:
            local_path = os.path.join(run_dir, os.path.basename(src_path))
            try:
                self.dev._run(["pull", src_path, local_path])
                files.append(local_path)
                self.dev._shell(f"rm -f {src_path}")
            except Exception as e:  # noqa: BLE001
                log.warning("[media] file pull failed: %s", e)

        # 4) 返回：保存后菜单自动关，当前在预览页；back 一次回聊天页，
        #    若仍在预览页（OCR 见「文件大小」）再 back 一次。
        self.dev.back()
        self.dev.wait_random(800, 1200)
        cur = self.dev.capture_bytes()
        if self._find_text_opt(run_ocr(cur), "文件大小"):
            self.dev.back()
            self.dev.wait_random(800, 1200)

        return MediaResult(
            msg_id=task.msg_id, msg_type="file",
            content=local_path or "[文件]",
            raw_files=files,
            success=bool(local_path),
            error=None if local_path else f"未在 {save_dir} 找到新保存的文件",
            run_dir=run_dir)

    # ------------------------------------------------------------------ 页面签名

    def _detect_page_signature(self, img: np.ndarray) -> Tuple[str, np.ndarray]:
        """判断当前页面类型，返回 (signature, 用于判定的稳定帧)。

        全帧 OCR（文件卡的「文件大小/文件名」在屏幕中部，顶部+底部双条会漏）；
        页面切换的过渡帧是纯黑，会被黑底查看器规则误吞——判到 media_viewer
        时等 ~1s 重截重判一次，稳定后再下结论，并把稳定帧一并返回
        （调用方后续裁切/OCR 必须用返回的帧，不能再用过渡帧）。
        """
        sig = self._classify_frame(img)
        if sig == "media_viewer":
            self.dev.wait_random(800, 1200)
            img2 = self.dev.capture_bytes()
            sig2 = self._classify_frame(img2)
            if sig2 != "media_viewer":
                return sig2, img2
        return sig, img

    def _classify_frame(self, img: np.ndarray) -> str:
        items = run_ocr(img)
        h = img.shape[0]
        full = " ".join(it["text"] for it in items)
        bottom = " ".join(it["text"] for it in items if it["cy"] > h * 0.82)

        if "的聊天记录" in full:
            return "chat_record"
        if "复制链接" in bottom or "在浏览器打开" in bottom:
            return "webview"
        if "保存图片" in bottom or "编辑" in bottom:
            return "photo_viewer"
        if "保存视频" in bottom:
            return "video_viewer"
        if "更多表情" in bottom or "添加" in bottom:
            return "sticker_detail"
        if ("用其他应用打开" in full or "文件大小" in full
                or re.search(r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|txt|md)\b", full, re.I)
                or re.search(r"\d+(?:\.\d+)?\s?(?:KB|MB|GB)\b", full, re.I)):
            return "file_card"
        # 全屏图片/视频查看器：黑底、底部只有无文字标签的 4 个圆形图标，
        # OCR 关键词全部落空。黑像素占比区分：查看器 ~0.37，聊天页 ~0.09。
        if self._is_fullscreen_viewer(img):
            return "media_viewer"
        return "unknown"

    @staticmethod
    def _is_fullscreen_viewer(img: np.ndarray) -> bool:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return float(np.mean(gray < 20)) > 0.25

    # ------------------------------------------------------------------ OCR 定位辅助

    def _find_text_on_screen(self, keyword: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
        """全屏 OCR 找关键词，命中则返回中心点，否则用 fallback。"""
        img = self.dev.capture_bytes()
        items = run_ocr(img)
        for it in items:
            if keyword in it["text"]:
                return int(it["cx"]), int(it["cy"])
        log.warning("[media] keyword %r not found, use fallback %s", keyword, fallback)
        return fallback

    @staticmethod
    def _find_text_opt(items, keywords) -> Optional[Tuple[int, int]]:
        """在 OCR items 里找任意一个关键词，返回中心点；没找到返回 None。
        items 由调用方传入（同一次 OCR 复用），避免每找一次就重新截图识别。"""
        kw = keywords if isinstance(keywords, (list, tuple)) else [keywords]
        for it in items:
            t = it.get("text", "")
            if any(k in t for k in kw):
                return int(it["cx"]), int(it["cy"])
        return None

    # ------------------------------------------------------------------ 返回会话兜底

    def _return_to_chat(self):
        """尽最大努力回到微信聊天页。"""
        for _ in range(5):
            act = self.dev.get_current_activity()
            if act == "com.tencent.mm/.ui.LauncherUI":
                # 可能在首页，需要重新进入会话（由接入层负责）
                break
            if act.startswith("com.tencent.mm"):
                # 还在微信某页面，持续 back
                self.dev.back()
                self.dev.wait_random(600, 1000)
            else:
                break

    # ------------------------------------------------------------------ 落盘辅助

    def _save_crop(self, crop: np.ndarray, task: MediaTask, name: str) -> str:
        d = _ensure_dir(os.path.join(DEBUG_ROOT, "messages", _safe_name(task.group_name), task.msg_id))
        path = os.path.join(d, f"{name}.png")
        cv2.imwrite(path, crop)
        return path

    def _save_frame(self, img: np.ndarray, task: MediaTask, name: str) -> str:
        d = _ensure_dir(os.path.join(DEBUG_ROOT, "errors"))
        path = os.path.join(d, f"{task.msg_id}_{_safe_name(task.group_name)}_{name}_{_ts()}.png")
        cv2.imwrite(path, img)
        return path

    def _save_error_shot(self, task: MediaTask, name: str):
        try:
            img = self.dev.capture_bytes()
            self._save_frame(img, task, name)
        except Exception:
            pass

    def _list_dir(self, remote_dir: str) -> List[str]:
        """列目录文件名（ls -1，一行一名，中文/空格安全）。"""
        out = self.dev._shell(f"ls -1 {remote_dir} 2>/dev/null").decode(errors="replace")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def _wait_for_new_file(self, remote_dir: str, timeout: int = 30,
                           exclude=()) -> Optional[str]:
        """轮询远程目录，返回新出现的文件路径。

        exclude：操作前的文件名快照集合；只认不在快照里的名字（差集法），
        避免拉到目录里无关的旧文件。文件名可能含中文/空格，用 ls -1t 整行解析，
        不能用 ls -l 按空格 split。
        """
        excl = set(exclude)
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self.dev._shell(f"ls -1t {remote_dir} 2>/dev/null").decode(errors="replace")
            for ln in out.splitlines():
                n = ln.strip()
                if n and n not in excl and n not in (".", ".."):
                    return f"{remote_dir}/{n}"
            time.sleep(0.5)
        return None


# -----------------------------------------------------------------------------

def handle_media(dev: DeviceCtl, task: MediaTask) -> MediaResult:
    """便捷入口。"""
    return MediaHandler(dev).handle(task)


# -----------------------------------------------------------------------------
# slice_chat 消息 -> MediaTask 桥接（接入交互层用）

def _handle_label(label: str, detail: Optional[dict]) -> Optional[str]:
    """media_classifier.classify_segment 的 label -> MediaHandler.handle() 的 msg_type。

    text/quote 由现有文本 OCR 路径处理（非多媒体），返回 None 表示跳过；
    unknown/system 跳过。
    """
    if label == "media":
        return "media"          # 图片/视频/表情包：点开后按页面签名细分
    if label == "card":
        return "card"           # 聊天记录/文件/链接卡：_handle_link_or_card 按签名分流
    if label == "red_packet":
        return "red_packet"
    if label in ("text", "quote"):
        return None             # 走现有文本路径
    return None


def classify_slice_to_task(img, msg, group_name):
    """把 slice_chat 的一条消息转成 MediaTask（用 media_classifier 定类型）。

    img: 整屏 BGR；msg: slice_chat 的 message dict（含 bubble_rect/content）。
    返回 (MediaTask|None, label)：非多媒体（text/quote/system）或找不到 bubble_rect
    时返回 (None, label)，由调用方跳过。
    """
    bb = msg.get("bubble_rect")
    if not bb or len(bb) < 4:
        return None, msg.get("content_type")
    x, y, w, h = (int(v) for v in bb[:4])
    if w <= 0 or h <= 0:
        return None, msg.get("content_type")
    crop = img[y:y + h, x:x + w]
    if crop.size == 0:
        return None, msg.get("content_type")
    label, detail = classify_segment(crop, msg.get("content") or "")
    msg_type = _handle_label(label, detail)
    if msg_type is None:
        return None, label
    task = MediaTask(
        msg_id=f"{group_name}_{y}_{x}_{w}_{h}",
        msg_type=msg_type,
        bbox=(x, y, w, h),
        screen_path="",
        group_name=group_name,
    )
    return task, label
