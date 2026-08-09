# -*- coding: utf-8 -*-
"""device_ctl.py — Layer 0 设备操控层封装（OnePlus 6T / cf04642e 专用）。

所有操作带随机化防风控；所有 adb 调用统一走 _run 封装（debug 日志 + 失败重试 1 次）。

中文输入方案（2026-08-04 v2，IME 已锁定）：
  - 系统默认且唯一启用的 IME = ADBKeyBoard（com.android.adbkeyboard/.AdbIME），
    wetype/LatinIME 已 disable（备份 config/ime_backup.json）。
  - 输入链：点输入栏（无键盘弹出，布局零变化）-> 热身广播 -> jieba 分块广播 ->
    绿色"发送"按钮固定位置。不再需要 ime set 来回切换。
  - 触控随机化走 random_touch.RandomTouch（Rect 区域抽象），tap/swipe 为兼容包装。
"""

import logging
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time

import jieba
import threading

from .random_touch import Rect, RandomTouch
from .....shared.ops_journal import log_op

PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", ".."))
ADB_PATH = os.path.join(PROJECT_ROOT, "tools", "platform-tools", "adb")
SERIAL = "cf04642e"

WECHAT_PKG = "com.tencent.mm"
WECHAT_MAIN_ACTIVITY = "com.tencent.mm/.ui.LauncherUI"

ADBKB_PKG = "com.android.adbkeyboard"
ADBKB_IME = "com.android.adbkeyboard/.AdbIME"
# apk 放项目内（/tmp 会丢）；缺失时从旧位置拷贝兜底，都没有则报错提示
ADBKB_APK = os.path.join(PROJECT_ROOT, "tools", "ADBKeyboard.apk")
ADBKB_APK_LEGACY = "/tmp/ADBKeyboard.apk"
ADBKB_APK_URL = "https://github.com/senzhk/ADBKeyBoard/raw/master/ADBKeyboard.apk"

# 截图落盘目录（capture_bytes 内存截图是主路径；落盘版仅调试用，用完即删）
CAPTURE_TMP_DIR = os.path.join(PROJECT_ROOT, "workspace", "runtime", "tmp")

SCREEN_W, SCREEN_H = 1080, 2340

log = logging.getLogger("device_ctl")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class DeviceCtl:
    def __init__(self):
        self.touch = RandomTouch(self._shell)
        self._cap_lock = threading.Lock()  # 截图串行锁：防止推流线程与 agent 抢 adb
        self.ensure_device()

    # ------------------------------------------------------------------ adb
    def _run(self, args, timeout=20, retries=1):
        """统一 adb 调用：失败重试 retries 次，返回 stdout(bytes)。"""
        cmd = [ADB_PATH, "-s", SERIAL] + [str(a) for a in args]
        last_err = ""
        for attempt in range(retries + 1):
            log.debug("adb run (attempt %d): %s", attempt, " ".join(cmd))
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                last_err = "timeout"
                log.debug("adb timeout: %s", " ".join(cmd))
                continue
            if r.returncode == 0:
                return r.stdout
            last_err = r.stderr.decode(errors="replace").strip()
            log.debug("adb failed rc=%d err=%s", r.returncode, last_err)
            time.sleep(0.3)
        raise RuntimeError(f"adb command failed: {' '.join(cmd)}\n{last_err}")

    def _shell(self, cmd_str, timeout=20):
        return self._run(["shell", cmd_str], timeout=timeout)

    # ------------------------------------------------------------- 设备状态
    def ensure_device(self):
        """检查设备在线，断线则 reconnect + wait-for-device，仍失败抛异常。"""
        try:
            out = subprocess.run([ADB_PATH, "devices"], capture_output=True,
                                 timeout=10).stdout.decode(errors="replace")
        except subprocess.TimeoutExpired:
            out = ""
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == SERIAL and parts[1] == "device":
                log.debug("device %s online", SERIAL)
                return
        log.warning("device %s not online, reconnecting...", SERIAL)
        subprocess.run([ADB_PATH, "reconnect"], capture_output=True, timeout=15)
        subprocess.run([ADB_PATH, "-s", SERIAL, "wait-for-device"],
                       capture_output=True, timeout=30)
        out = subprocess.run([ADB_PATH, "devices"], capture_output=True,
                             timeout=10).stdout.decode(errors="replace")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == SERIAL and parts[1] == "device":
                return
        raise RuntimeError(f"device {SERIAL} offline after reconnect")

    def get_current_activity(self):
        """解析 dumpsys window 的 mCurrentFocus，返回如 com.tencent.mm/.ui.LauncherUI。"""
        out = self._shell("dumpsys window | grep mCurrentFocus").decode(errors="replace")
        m = re.search(r"mCurrentFocus=Window\{[^}]*\s+\S+\s+(\S+)\}", out)
        if not m:
            # 兼容无 Window{...} 前缀的格式
            m = re.search(r"mCurrentFocus=\S*\s*(\S+/\S+)\}?", out)
        activity = m.group(1).rstrip("}") if m else ""
        log.debug("current activity: %s", activity)
        return activity

    # ----------------------------------------------------------------- 截图
    def capture(self):
        """落盘版截图（仅供调试；主路径是 capture_bytes 内存截图）。
        落 workspace/runtime/tmp/wx_cap_<timestamp>.png，调用方用完必须自行删除。"""
        os.makedirs(CAPTURE_TMP_DIR, exist_ok=True)
        for attempt in range(2):
            with self._cap_lock:
                data = self._run(["exec-out", "screencap", "-p"], timeout=30, retries=0)
            if data.startswith(PNG_MAGIC) and len(data) > 1024:
                path = os.path.join(
                    CAPTURE_TMP_DIR, f"wx_cap_{int(time.time() * 1000)}.png")
                with open(path, "wb") as f:
                    f.write(data)
                log.debug("captured %s (%d bytes)", path, len(data))
                return path
            log.warning("bad screenshot (attempt %d), %d bytes", attempt, len(data))
            time.sleep(0.5)
        raise RuntimeError("screencap returned invalid PNG twice")

    def capture_bytes(self):
        """内存版截图：PNG bytes 直接 imdecode 成 BGR ndarray，全程不落盘。

        比 capture() 更彻底地满足"截图即读即删"（字节只驻内存，引用释放即消失）。
        坏图重试一次。返回 numpy.ndarray（cv2 BGR）。"""
        import cv2
        import numpy as np
        for attempt in range(2):
            with self._cap_lock:
                data = self._run(["exec-out", "screencap", "-p"], timeout=30, retries=0)
            if data.startswith(PNG_MAGIC) and len(data) > 1024:
                img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8),
                                   cv2.IMREAD_COLOR)
                if img is not None:
                    log.debug("captured in-memory frame (%d bytes)", len(data))
                    return img
            log.warning("bad screenshot bytes (attempt %d), %d bytes",
                        attempt, len(data))
            time.sleep(0.5)
        raise RuntimeError("screencap returned invalid PNG twice")

    # ----------------------------------------------------------------- 触控
    def tap_rect(self, rect, sigma_ratio=0.25):
        """Rect 区域内高斯偏中心随机点按（随机化框架 v2 主 API）。"""
        pt = self.touch.tap_rect(rect, sigma_ratio=sigma_ratio)
        log_op("tap", x=int(pt[0]), y=int(pt[1]), rect=str(rect))
        self.wait_random(80, 200)
        return pt

    def double_tap_rect(self, rect, interval_ms=(100, 220), sigma_ratio=0.25):
        """Rect 区域内双击（两次独立随机落点 + 随机间隔），如首页"微信"Tab。"""
        pts = self.touch.double_tap_rect(rect, interval_ms=interval_ms,
                                         sigma_ratio=sigma_ratio)
        log_op("double_tap", rect=str(rect))
        self.wait_random(80, 200)
        return pts

    def long_press_rect(self, rect, sigma_ratio=0.25):
        """Rect 区域内长按（随机落点 + 500~800ms 按压，触发系统长按菜单）。"""
        pt = self.touch.long_press_rect(rect, sigma_ratio=sigma_ratio)
        log_op("long_press", rect=str(rect))
        self.wait_random(80, 200)
        return pt

    def tap(self, x, y):
        """旧签名兼容包装：以 (x,y) 为中心的小区域 tap_rect。"""
        return self.tap_rect(Rect(int(x) - 20, int(y) - 20, 40, 40))

    def swipe_zone(self, zone, direction="up", length_ratio=(0.35, 0.65),
                   diag_ratio=0.30, duration_ms=(250, 550)):
        """区域内随机化滑动（贝塞尔轨迹、支持斜滑，随机化框架 v2 主 API）。"""
        pts = self.touch.swipe_zone(zone, direction=direction,
                                    length_ratio=length_ratio,
                                    diag_ratio=diag_ratio,
                                    duration_ms=duration_ms)
        log_op("swipe", direction=direction)
        self.wait_random(100, 250)
        return pts

    def swipe(self, x1, y1, x2, y2, duration_ms=None):
        """旧签名兼容包装：由起终点推出 zone + direction + 长度，走 swipe_zone。"""
        dx, dy = x2 - x1, y2 - y1
        if abs(dy) >= abs(dx):
            direction = "up" if dy < 0 else "down"
        else:
            direction = "left" if dx < 0 else "right"
        zone = Rect.from_xyxy(max(0, min(x1, x2) - 40), max(0, min(y1, y2) - 40),
                              min(SCREEN_W, max(x1, x2) + 40),
                              min(SCREEN_H, max(y1, y2) + 40))
        inner = zone.inset(0.08)
        extent = inner.h if direction in ("up", "down") else inner.w
        ratio = max(0.05, min(0.98, (abs(dy) if direction in ("up", "down")
                                     else abs(dx)) / max(1, extent)))
        dur = (duration_ms, duration_ms) if duration_ms else (250, 550)
        return self.swipe_zone(zone, direction=direction,
                               length_ratio=(ratio, ratio), diag_ratio=0.0,
                               duration_ms=dur)

    def key_event(self, code):
        self._shell(f"input keyevent {code}")
        self.wait_random(60, 150)

    def back(self):
        log_op("back")
        self.key_event("KEYCODE_BACK")

    def home_key(self):
        self.key_event("KEYCODE_HOME")

    def wait_random(self, min_ms, max_ms):
        t = random.randint(int(min_ms), int(max_ms)) / 1000.0
        time.sleep(t)

    # ----------------------------------------------------------------- 输入
    def input_text(self, text):
        """输入文本（中英文统一走 ADBKeyBoard 广播方案，IME 已锁定无需切换）。"""
        log_op("input_text", text=text)
        self._input_text_unicode(text)
        self.wait_random(150, 350)

    def _ensure_adb_keyboard(self):
        out = self._shell(f"pm list packages {ADBKB_PKG}").decode(errors="replace")
        if ADBKB_PKG in out:
            return
        log.info("ADBKeyBoard not installed, installing...")
        if not os.path.exists(ADBKB_APK):
            # 项目内 apk 缺失：从旧位置（/tmp）拷贝兜底
            if os.path.exists(ADBKB_APK_LEGACY):
                os.makedirs(os.path.dirname(ADBKB_APK), exist_ok=True)
                shutil.copy(ADBKB_APK_LEGACY, ADBKB_APK)
                log.info("ADBKeyboard.apk copied from %s", ADBKB_APK_LEGACY)
            else:
                raise RuntimeError(
                    f"ADBKeyboard.apk 缺失：{ADBKB_APK} 不存在，"
                    f"旧位置 {ADBKB_APK_LEGACY} 也没有。"
                    f"请手动下载放到项目内：{ADBKB_APK_URL}")
        self._run(["install", "-r", ADBKB_APK], timeout=120)

    def _split_chunks(self, text):
        """jieba 分词后随机聚合成 1~3 词的词块，模拟真人打字的输入节奏。

        风控背景（2026-08-03）：整条文本一次性广播上屏的输入特征过于机械，
        疑似导致微信账号被风控踢下线。改为词块依次输入。"""
        words = [w for w in jieba.cut(text) if w.strip()]
        if not words:
            return [text]
        chunks = []
        i = 0
        while i < len(words):
            n = random.randint(1, 3)
            chunks.append("".join(words[i:i + n]))
            i += n
        return chunks

    def _input_text_unicode(self, text):
        """ADBKeyBoard 广播输入（IME 已锁定为 ADBKeyBoard，无需 ime set 切换）。

        保留：安装兜底 + 默认 IME 断言式自检、热身广播（输入连接建立仍有竞态，
        实测首块可能被吞）、jieba 分块 + 块间 0.3~0.8s 随机停顿（风控教训，勿动）。"""
        self._ensure_adb_keyboard()
        self._assert_adb_ime()
        chunks = self._split_chunks(text)
        # 热身广播：输入连接建立延迟会吞第一条广播，先发一条空广播兜底
        # （2026-08-03 实测：先发空广播可 100% 保住后续首块）
        self._shell("am broadcast -a ADB_INPUT_TEXT --es msg ''")
        time.sleep(random.uniform(0.3, 0.6))
        for i, chunk in enumerate(chunks):
            self._shell(
                f"am broadcast -a ADB_INPUT_TEXT --es msg {shlex.quote(chunk)}")
            if i < len(chunks) - 1:
                time.sleep(random.uniform(0.3, 0.8))
        log.debug("input_text chunked: %d chunks, %d chars", len(chunks), len(text))
        # 等最后一块上屏，时长随块长增加
        time.sleep(min(2.0, 0.5 + len(chunks[-1]) * 0.08))

    def _assert_adb_ime(self):
        """断言式自检：默认 IME 必须是 ADBKeyBoard（IME 锁定方案 §2.2）。
        被系统更新/误操作改掉时自动重设并告警。"""
        cur = self._shell(
            "settings get secure default_input_method").decode(errors="replace").strip()
        if cur != ADBKB_IME:
            log.warning("default IME is %r, expected %r, re-setting", cur, ADBKB_IME)
            self._shell(f"ime enable {ADBKB_IME}")
            self._shell(f"ime set {ADBKB_IME}")

    def clear_text(self, times=40):
        log_op("clear_text")
        """ADBKeyBoard 自带 ADB_CLEAR_TEXT 广播一次清空输入框（2026-08-04 真机验证）。

        根除 v1 连发 30 次 KEYCODE_DEL 丢事件残留旧文本的问题（AGENTS.md 第 9 条）。
        广播无效时（ADBKeyBoard 版本不支持）退回 KEYCODE_DEL 方案。times 仅为
        退回方案保留兼容参数。"""
        self._shell("am broadcast -a ADB_CLEAR_TEXT")
        self.wait_random(150, 300)

    # ----------------------------------------------------------------- 应用
    def open_wechat(self, timeout_s=10):
        """monkey 拉起微信并等待 activity 验证。"""
        log_op("open_wechat")
        self._shell(f"monkey -p {WECHAT_PKG} -c android.intent.category.LAUNCHER 1")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            act = self.get_current_activity()
            if act.startswith(WECHAT_PKG):
                log.debug("wechat foreground: %s", act)
                return act
            time.sleep(0.5)
        raise RuntimeError(f"wechat not in foreground after {timeout_s}s, "
                           f"current={self.get_current_activity()}")

    # ----------------------------------------------------------------- 屏幕
    def wake_and_dim(self):
        """唤醒 + 解 keyguard + 保持常亮 + 最低亮度 + 锁定竖屏（工作态）。"""
        self._shell("input keyevent KEYCODE_WAKEUP")
        self._shell("wm dismiss-keyguard")
        self._shell("svc power stayon true")
        self._shell("settings put system screen_brightness 10")
        # 自动旋转会被传感器/系统改回，横屏会让全部坐标标定失效，每次工作前强制竖屏
        self._shell("settings put system accelerometer_rotation 0")
        self._shell("settings put system user_rotation 0")

    def restore_screen(self):
        """恢复：取消常亮 + 亮度 46（原值）。"""
        self._shell("svc power stayon false")
        self._shell("settings put system screen_brightness 46")


# --------------------------------------------------------------------- 自测
if __name__ == "__main__":
    print("device_ctl.py 不提供自测入口：直接运行会真实操作手机，已禁用。"
              "请用离线单测（假对象）。")
    raise SystemExit(1)
