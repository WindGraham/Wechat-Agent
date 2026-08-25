# -*- coding: utf-8 -*-
"""screen_capture.py — MediaCodec(H.264) 单帧采集客户端（socket 版）。

对接 CaptureServer（app_process，socket 7000）：发 1 字节命令 → 收
[4字节长度 + H.264(annex-b SPS+PPS+IDR)]，常驻 ffmpeg 进程解码成 BGR。
常驻 ffmpeg 避免每帧 fork 进程 ~100ms；关键帧几百 KB 超 64KB 管道缓冲，
故写入走后台线程、主线程同步读 stdout，避免管道写满互相阻塞死锁。
"""

import socket
import struct
import subprocess
import threading
import time

import numpy as np


class _H264Decoder:
    """常驻 ffmpeg 解码器：H.264(annex-b) → BGR 帧。"""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.frame_size = width * height * 3
        self.proc = None

    def _ensure(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-f", "h264", "-i", "pipe:0",
                 "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)

    def decode(self, h264_data):
        self._ensure()
        def _write():
            try:
                self.proc.stdin.write(h264_data)
                self.proc.stdin.flush()
            except Exception:
                pass
        t = threading.Thread(target=_write, daemon=True)
        t.start()
        raw = self._read_exact(self.proc.stdout, self.frame_size)
        t.join(timeout=2)
        if raw is None:
            self._restart()
            raise RuntimeError("ffmpeg 解码无输出")
        return np.frombuffer(raw, np.uint8).reshape(self.height, self.width, 3)

    def _restart(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None

    @staticmethod
    def _read_exact(stream, n):
        buf = b""
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None


class ScreenCapture:
    """按需单帧截图（MediaCodec H.264 + 常驻 ffmpeg 解码）。"""

    ADB = "tools/platform-tools/adb"
    SERIAL = "cf04642e"
    DEX = "/data/local/tmp/capture.dex"
    MAIN = "com.wechatagent.capture.CaptureServer"
    PORT = 7000
    WIDTH = 1080
    HEIGHT = 2340

    def __init__(self, adb=None, serial=None):
        self.adb = adb or self.ADB
        self.serial = serial or self.SERIAL
        self.sock = None
        self._decoder = _H264Decoder(self.WIDTH, self.HEIGHT)
        self._start_server()
        self._connect()

    def _start_server(self):
        cmd = (f"setsid sh -c 'CLASSPATH={self.DEX} app_process / {self.MAIN} "
               f">/dev/null 2>&1 &'")
        subprocess.run([self.adb, "-s", self.serial, "shell", cmd],
                       capture_output=True, timeout=15)
        subprocess.run([self.adb, "-s", self.serial, "forward",
                        f"tcp:{self.PORT}", f"tcp:{self.PORT}"],
                       capture_output=True)

    def _connect(self):
        for _ in range(40):
            try:
                s = socket.create_connection(("127.0.0.1", self.PORT), timeout=5)
                s.settimeout(5)
                s.sendall(b"C")
                head = self._recv_exact_sock(s, 4)
                if len(head) == 4:
                    n = struct.unpack(">I", head)[0]
                    self._recv_exact_sock(s, n)
                    self.sock = s
                    return
                s.close()
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError("无法连接 CaptureServer 端口 7000")

    def alive(self):
        if self.sock is None:
            return False
        try:
            self.sock.sendall(b"C")
            head = self._recv_exact(4)
            if len(head) < 4:
                return False
            n = struct.unpack(">I", head)[0]
            return len(self._recv_exact(n)) == n
        except Exception:
            return False

    def capture(self):
        self.sock.sendall(b"C")
        head = self._recv_exact(4)
        if len(head) < 4:
            raise RuntimeError("CaptureServer 未返回帧")
        n = struct.unpack(">I", head)[0]
        if n == 0:
            raise RuntimeError("空帧（服务端无关键帧）")
        data = self._recv_exact(n)
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "h264", "-i", "pipe:0",
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
            input=data, capture_output=True, timeout=6)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError("ffmpeg 解码失败")
        img = np.frombuffer(proc.stdout, dtype=np.uint8)
        expect = self.WIDTH * self.HEIGHT * 3
        if img.size != expect:
            raise RuntimeError(f"解码尺寸异常: {img.size} != {expect}")
        return img.reshape(self.HEIGHT, self.WIDTH, 3)

    def _recv_exact(self, n):
        return self._recv_exact_sock(self.sock, n)

    @staticmethod
    def _recv_exact_sock(s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def close(self):
        self._decoder.close()
        try:
            self.sock.close()
        except Exception:
            pass
        subprocess.run([self.adb, "-s", self.serial, "forward",
                        "--remove", f"tcp:{self.PORT}"], capture_output=True)
