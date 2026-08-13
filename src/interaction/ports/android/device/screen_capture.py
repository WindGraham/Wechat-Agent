# -*- coding: utf-8 -*-
"""screen_capture.py — ImageReader + JPEG q100 单帧采集客户端（socket 版）。

对接 CaptureServer 服务（app_process 运行，socket 端口 7000）。
adb forward 转发端口，socket 通信：发 1 字节命令 → 收 [4字节长度 + JPEG]。

注意：adb forward 的 socket 是"懒连接"（本地 accept 但设备端未监听时会
静默断开），所以 __init__ 里必须"发命令 + 收到帧"才算服务真正就绪。
"""

import socket
import struct
import subprocess
import time

import cv2
import numpy as np


class ScreenCapture:
    """按需单帧截图（ImageReader + JPEG q100，替代 screencap）。"""

    ADB = "tools/platform-tools/adb"
    SERIAL = "cf04642e"
    DEX = "/data/local/tmp/capture.dex"
    MAIN = "com.wechatagent.capture.CaptureServer"
    PORT = 7000

    def __init__(self, adb=None, serial=None):
        self.adb = adb or self.ADB
        self.serial = serial or self.SERIAL
        # 启动服务（后台）+ 端口转发
        subprocess.Popen(
            [self.adb, "-s", self.serial, "shell",
             f"CLASSPATH={self.DEX} app_process / {self.MAIN}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run([self.adb, "-s", self.serial, "forward",
                        f"tcp:{self.PORT}", f"tcp:{self.PORT}"],
                       capture_output=True)
        # 连接 + 验证（收到完整一帧才算就绪）
        self.sock = None
        for _ in range(40):
            try:
                s = socket.create_connection(("127.0.0.1", self.PORT), timeout=5)
                s.settimeout(5)
                s.sendall(b"C")
                head = self._recv_exact_sock(s, 4)
                if len(head) == 4:
                    n = struct.unpack(">I", head)[0]
                    self._recv_exact_sock(s, n)   # 读完这一帧，丢弃
                    self.sock = s                 # 服务就绪，socket 干净
                    break
                s.close()
            except OSError:
                pass
            time.sleep(0.5)     # 失败也等待，给服务启动时间
        if self.sock is None:
            raise RuntimeError("无法连接 CaptureServer 端口 7000")

    def capture(self):
        """抓一帧，返回 BGR ndarray。"""
        self.sock.sendall(b"C")
        head = self._recv_exact(4)
        if len(head) < 4:
            raise RuntimeError("CaptureServer 未返回帧")
        n = struct.unpack(">I", head)[0]
        data = self._recv_exact(n)
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("JPEG 解码失败")
        return img

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
        try:
            self.sock.close()
        except Exception:
            pass
        subprocess.run([self.adb, "-s", self.serial, "forward",
                        "--remove", f"tcp:{self.PORT}"], capture_output=True)
