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
import socket
import struct
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

# ---- 剪贴板读取（Android 15 root，见 tools/clipboard/ClipIO.java）----
# 原理：Android 15 剪贴板读门控 = 读取者必须是「默认 IME」或「聚焦 App」。
# 以 root 跑 app_process → setuid(聚焦 App 的 uid) → IClipboard.getPrimaryClip。
# dex 是预编译产物（无需真机 SDK）；源码/构建见 tools/clipboard/。
CLIP_DEX_SRC = os.path.join(PROJECT_ROOT, "tools", "clipboard", "dex", "clip.dex")
CLIP_DEX_REMOTE = "/data/local/tmp/clip.dex"
CLIP_OUT_REMOTE = "/data/local/tmp/clip_read.txt"

log = logging.getLogger("device_ctl")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# ImageReader 快速截图重连冷却（秒）：初始化失败后这段时间内不重复尝试，
# 避免服务真起不来时每帧都白等 40×0.5s 的连接重试。
FAST_CAP_RETRY_INTERVAL = 60.0
# 快速截图总开关：MediaCodec/ffmpeg 链路不稳（超时/解码失败）时置 False，
# 直接走 screencap 兜底，绕过坏掉的 ImageReader。
USE_FAST_CAPTURE = False


class DeviceCtl:
    def __init__(self):
        self.touch = RandomTouch(self._shell)
        self._cap_lock = threading.Lock()  # 截图串行锁：防止推流线程与 agent 抢 adb
        self._fast_cap = None  # ImageReader 快速截图客户端（懒加载）
        self._fast_cap_failed_ts = 0.0  # 上次初始化失败时间戳（重连冷却用）
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
        """内存版截图，返回 BGR ndarray。

        优先走 ImageReader 快速截图（~50ms，JPEG q100 无损）；不可用或失败
        时回退 screencap（~500ms）。坏图重试一次。"""
        # 1) 优先 ImageReader 快速截图
        cap = self._get_fast_capture()
        if cap is not None:
            try:
                return cap.capture()
            except Exception as e:
                log.warning("ImageReader 截图失败，回退 screencap: %s", e)
                # 服务中途死掉：置回 None 触发下次重连（而非永久持有死连接）
                self._fast_cap = None
                try:
                    cap.close()
                except Exception:
                    pass
        # 2) 回退 screencap
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

    def _get_fast_capture(self):
        """懒加载 ImageReader 快速截图客户端（首次启动服务约 2~3 秒）。

        成功返回 ScreenCapture；失败返回 False 并缓存，冷却期内不重试。
        服务中途死掉时 capture_bytes 会把 _fast_cap 置回 None 触发重连
        （不再永久持有死连接，每帧空等超时）。
        """
        if not USE_FAST_CAPTURE:
            return None
        if self._fast_cap is None:
            self._try_fast_capture()
        elif self._fast_cap is False:
            if time.time() - self._fast_cap_failed_ts >= FAST_CAP_RETRY_INTERVAL:
                self._try_fast_capture()
        return self._fast_cap if self._fast_cap is not False else None

    def _try_fast_capture(self):
        """尝试初始化 ImageReader 客户端；失败降级为 False 并记录时间戳。"""
        try:
            from .screen_capture import ScreenCapture
            self._fast_cap = ScreenCapture()
            log.info("ImageReader 快速截图已就绪（~50ms/帧）")
        except Exception as e:
            log.warning("ImageReader 快速截图不可用，回退 screencap: %s", e)
            self._fast_cap = False
            self._fast_cap_failed_ts = time.time()

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

    def _ensure_clip_dex(self):
        """把剪贴板读取器 dex 推到设备（与本地大小一致则跳过）。"""
        if not os.path.exists(CLIP_DEX_SRC):
            raise RuntimeError(f"剪贴板 dex 缺失：{CLIP_DEX_SRC}。"
                               f"请运行 tools/clipboard/build.sh")
        local_size = os.path.getsize(CLIP_DEX_SRC)
        try:
            remote_size = int(self._shell(
                f"wc -c < {CLIP_DEX_REMOTE} 2>/dev/null").decode(errors="replace").strip() or 0)
        except ValueError:
            remote_size = -1
        if remote_size != local_size:
            self._run(["push", CLIP_DEX_SRC, CLIP_DEX_REMOTE], timeout=30)

    # ---- 剪贴板常驻服务（daemon，读 ~0ms）----
    CLIP_PORT = 7001

    @staticmethod
    def _recv_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _clip_ping(self, timeout=0.6):
        """常驻服务是否真的健在（发 'P' 心跳做完整往返，避免 adb forward 的假连接）。"""
        try:
            with socket.create_connection(("127.0.0.1", self.CLIP_PORT), timeout=timeout) as s:
                s.sendall(b"P")
                hdr = self._recv_exact(s, 4)
                if len(hdr) == 4 and struct.unpack(">I", hdr)[0] == 0:
                    return True
        except OSError:
            return False
        return False

    def _clip_daemon(self):
        """确保常驻服务已起并已 forward；成功返回 True。"""
        if self._clip_ping():
            return True
        self._ensure_clip_dex()
        try:
            self._run(["forward", f"tcp:{self.CLIP_PORT}", f"tcp:{self.CLIP_PORT}"])
            # su 用双引号、sh -c 用单引号 —— 这样 setsid & 不会让 adb shell 阻塞
            self._shell(f'su -c "setsid sh -c \'CLASSPATH={CLIP_DEX_REMOTE} '
                        f'app_process / ClipIOServer >/dev/null 2>&1 &\'"')
        except Exception as e:  # noqa: BLE001
            log.warning("clipsrv start failed: %s", e)
        for _ in range(25):          # 最多等 ~5s
            if self._clip_ping():
                return True
            time.sleep(0.2)
        return self._clip_ping()

    def _clip_request(self, cmd_byte):
        """向常驻服务发单个命令并读回复；失败抛异常。"""
        with socket.create_connection(("127.0.0.1", self.CLIP_PORT), timeout=5) as s:
            s.sendall(cmd_byte.encode())
            hdr = self._recv_exact(s, 4)
            if len(hdr) < 4:
                raise RuntimeError("clipsrv 无响应")
            n = struct.unpack(">I", hdr)[0]
            data = self._recv_exact(s, n)
        return data.decode("utf-8", errors="replace").strip()

    def read_clipboard(self) -> str:
        """读取剪贴板文本（Android 15 root：常驻服务 setuid 到聚焦 App 后读）。

        return：剪贴板文本；空串 = 无文本/无法读取。优先走常驻服务（~0ms），
        服务不可用时回退一次性 app_process（以聚焦 App 身份 getPrimaryClip，
        ClipData 里所有 item 的 text 拼接）。
        """
        try:
            if self._clip_daemon():
                return self._clip_request("R")
        except Exception as e:  # noqa: BLE001
            log.warning("read_clipboard daemon 失败，回退 one-shot: %s", e)
        # fallback：一次性 app_process
        self._ensure_clip_dex()
        self._shell(f"su -c 'touch {CLIP_OUT_REMOTE} && chmod 666 {CLIP_OUT_REMOTE}'")
        self._shell(f"su -c 'CLASSPATH={CLIP_DEX_REMOTE} app_process / ClipIO read'")
        out = self._shell(f"cat {CLIP_OUT_REMOTE}").decode(errors="replace").strip()
        if out.startswith("ERR["):
            log.warning("read_clipboard one-shot: %s", out)
            return ""
        return out

    def set_clipboard(self, text: str) -> None:
        """写剪贴板（root，测试/预置用）。优先常驻服务，回退一次性。"""
        try:
            if self._clip_daemon():
                b = text.encode("utf-8")
                with socket.create_connection(("127.0.0.1", self.CLIP_PORT), timeout=5) as s:
                    s.sendall(b"S" + struct.pack(">I", len(b)) + b)
                    self._recv_exact(s, 4)
                return
        except Exception as e:  # noqa: BLE001
            log.warning("set_clipboard daemon 失败，回退 one-shot: %s", e)
        self._ensure_clip_dex()
        self._shell(f"su -c 'CLASSPATH={CLIP_DEX_REMOTE} "
                    f"app_process / ClipIO set {shlex.quote(text)}'")

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
