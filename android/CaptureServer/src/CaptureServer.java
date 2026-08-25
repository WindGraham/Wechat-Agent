package com.wechatagent.capture;

import android.content.Context;
import android.graphics.Rect;
import android.hardware.display.VirtualDisplay;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Looper;
import android.os.Process;
import android.util.Log;
import android.view.Surface;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.InputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;

/**
 * MediaCodec(H.264) + VirtualDisplay 单帧采集服务（替代 ImageReader）。
 *
 * Android 15 关键：5 参静态 DisplayManager.createVirtualDisplay 创建的虚拟显示
 * 不自镜像（无 FLAG_AUTO_MIRROR），编码器 surface 收不到帧。方案（scrcpy 同款）：
 * 反射 ActivityThread.systemMain().getSystemContext() 拿系统 Context →
 * 反射 DisplayManager(Context) 构造实例 → 调 6 参实例 createVirtualDisplay
 * (name,w,h,dpi,surface,FLAG_AUTO_MIRROR)。
 *
 * 协议：客户端发任意字节 → 服务端请求关键帧并返回 [4字节长度][annex-b SPS+PPS+IDR]。
 */
public class CaptureServer {
    private static final int WIDTH = 1080;
    private static final int HEIGHT = 2340;
    private static final int PORT = 7000;
    private static final String TAG = "CaptureServer";

    private MediaCodec encoder;
    private Surface inputSurface;
    private VirtualDisplay virtualDisplay;

    private final Object frameLock = new Object();
    private volatile byte[] codecConfig;
    private volatile byte[] latestKeyframe;
    private volatile long keyframeSeq = 0;

    public static void main(String[] args) throws Exception {
        Looper.prepareMainLooper();
        CaptureServer server = new CaptureServer();
        server.start();
        server.serve();
    }

    void start() throws Exception {
        MediaFormat fmt = MediaFormat.createVideoFormat("video/avc", WIDTH, HEIGHT);
        fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        fmt.setInteger(MediaFormat.KEY_BIT_RATE, 8_000_000);
        fmt.setInteger(MediaFormat.KEY_FRAME_RATE, 60);
        fmt.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
        // 强制编码器按 ~30fps 重复上一帧：静态屏虚拟显示被限帧(~6fps)时，
        // 也能持续出帧，REQUEST_SYNC_FRAME 立即拿到关键帧，不等 ~150ms。
        try {
            fmt.setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 33333L);
        } catch (Exception ignored) {
        }
        encoder = MediaCodec.createEncoderByType("video/avc");
        encoder.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        inputSurface = encoder.createInputSurface();
        encoder.start();

        virtualDisplay = DisplayManagerReflect.createVirtualDisplay(
                "cap", WIDTH, HEIGHT, 420, inputSurface);
        Log.i(TAG, "virtual display created");

        Thread drain = new Thread(this::drainLoop, "capture-drain");
        drain.setDaemon(true);
        drain.start();
        Log.i(TAG, "encoder started, drain thread running");
    }

    void drainLoop() {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        long frames = 0, lastLog = System.currentTimeMillis();
        try {
            while (true) {
                int idx = encoder.dequeueOutputBuffer(info, 100_000);
                if (idx >= 0) {
                    ByteBuffer out = encoder.getOutputBuffer(idx);
                    if (out != null && info.size > 0) {
                        if ((info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) {
                            byte[] raw = copyBytes(out, info.size);
                            Log.i(TAG, "codec-config raw(" + info.size + "): " + hex(raw, 40));
                            codecConfig = csdToAnnexB(raw);
                        } else if ((info.flags & MediaCodec.BUFFER_FLAG_KEY_FRAME) != 0) {
                            synchronized (frameLock) {
                                latestKeyframe = toAnnexB(copyBytes(out, info.size));
                                keyframeSeq++;
                                frameLock.notifyAll();
                            }
                        }
                    }
                    encoder.releaseOutputBuffer(idx, false);
                    frames++;
                    if (System.currentTimeMillis() - lastLog > 2000) {
                        Log.i(TAG, "encoder fps=" + (frames * 1000.0 / (System.currentTimeMillis() - lastLog)));
                        frames = 0; lastLog = System.currentTimeMillis();
                    }
                } else if (idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    MediaFormat f = encoder.getOutputFormat();
                    ByteBuffer csd = f.getByteBuffer("csd-0");
                    if (csd != null) {
                        codecConfig = csdToAnnexB(copyBytes(csd, csd.remaining()));
                    }
                }
            }
        } catch (Exception e) {
            Log.e(TAG, "drain loop died", e);
            Process.killProcess(Process.myPid());
        }
    }

    byte[] captureFrame() throws Exception {
        Bundle b = new Bundle();
        b.putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0);
        encoder.setParameters(b);

        long startSeq = keyframeSeq;
        long timeoutMs = (latestKeyframe == null) ? 3000 : 60;
        synchronized (frameLock) {
            long deadline = System.currentTimeMillis() + timeoutMs;
            while (keyframeSeq == startSeq && System.currentTimeMillis() < deadline) {
                frameLock.wait(100);
            }
        }
        if (codecConfig == null || latestKeyframe == null) {
            return null;
        }
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        bos.write(codecConfig);
        bos.write(latestKeyframe);
        return bos.toByteArray();
    }

    void serve() {
        try (ServerSocket serverSocket = new ServerSocket(PORT)) {
            while (true) {
                final Socket socket = serverSocket.accept();
                Thread th = new Thread(() -> {
                    try { handle(socket); } catch (Exception e) {
                        Log.e(TAG, "handle failed", e);
                    }
                });
                th.setDaemon(true);
                th.start();
            }
        } catch (Exception e) {
            Process.killProcess(Process.myPid());
        }
    }

    void handle(Socket socket) throws Exception {
        try (DataOutputStream out = new DataOutputStream(socket.getOutputStream());
             InputStream in = socket.getInputStream()) {
            byte[] cmd = new byte[64];
            while (in.read(cmd) != -1) {
                byte[] frame = captureFrame();
                if (frame == null) {
                    out.writeInt(0);
                    out.flush();
                    continue;
                }
                out.writeInt(frame.length);
                out.write(frame);
                out.flush();
            }
        }
    }

    static String hex(byte[] b, int max) {
        StringBuilder sb = new StringBuilder();
        int n = Math.min(b.length, max);
        for (int i = 0; i < n; i++) sb.append(String.format("%02x ", b[i]));
        return sb.toString();
    }

    static byte[] copyBytes(ByteBuffer buf, int size) {
        byte[] data = new byte[size];
        buf.get(data);
        return data;
    }

    /** codec-config(csd-0) → annex-b SPS+PPS。先判 annex-b，再 avcC，全程 bounds 保护。 */
    static byte[] csdToAnnexB(byte[] data) throws Exception {
        if (data == null || data.length < 7) return data;
        Log.w(TAG, "csd raw(" + data.length + "): " + hex(data, 48));
        // annex-b 起始码（00 00 01 或 00 00 00 01）→ 原样返回
        if (data[0] == 0 && data[1] == 0 && (data[2] == 1 || (data[2] == 0 && data[3] == 1))) {
            Log.w(TAG, "csd is annex-b, return as-is");
            return data;
        }
        // avcC：头 5 字节 + numSPS(1) + [2字节长度+SPS]*n + numPPS(1) + [2字节长度+PPS]*n
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            int pos = 5;
            int numSPS = data[pos] & 0x1F;
            pos++;
            for (int i = 0; i < numSPS && pos + 2 <= data.length; i++) {
                int len = ((data[pos] & 0xff) << 8) | (data[pos + 1] & 0xff);
                pos += 2;
                if (len <= 0 || pos + len > data.length) break;
                out.write(new byte[]{0, 0, 0, 1});
                out.write(data, pos, len);
                pos += len;
            }
            if (pos < data.length) {
                int numPPS = data[pos] & 0xff;
                pos++;
                for (int i = 0; i < numPPS && pos + 2 <= data.length; i++) {
                    int len = ((data[pos] & 0xff) << 8) | (data[pos + 1] & 0xff);
                    pos += 2;
                    if (len <= 0 || pos + len > data.length) break;
                    out.write(new byte[]{0, 0, 0, 1});
                    out.write(data, pos, len);
                    pos += len;
                }
            }
            byte[] r = out.toByteArray();
            Log.w(TAG, "csd parsed annex-b(" + r.length + ")");
            return r.length > 0 ? r : data;
        } catch (Exception e) {
            Log.w(TAG, "csd avcC parse failed, return raw", e);
            return data;
        }
    }

    static byte[] toAnnexB(byte[] src) throws Exception {
        if (src == null || src.length < 4) return src;
        if ((src[0] == 0 && src[1] == 0 && (src[2] == 1 || (src[2] == 0 && src[3] == 1)))) {
            return src;
        }
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int pos = 0;
        while (pos + 4 <= src.length) {
            int nalLen = ((src[pos] & 0xff) << 24) | ((src[pos + 1] & 0xff) << 16)
                    | ((src[pos + 2] & 0xff) << 8) | (src[pos + 3] & 0xff);
            pos += 4;
            if (nalLen <= 0 || pos + nalLen > src.length) break;
            out.write(new byte[]{0, 0, 0, 1});
            out.write(src, pos, nalLen);
            pos += nalLen;
        }
        return out.toByteArray();
    }
}

/** DisplayManager 反射：DisplayManager(Context) 实例 + 6 参 AUTO_MIRROR（scrcpy 同款）。 */
final class DisplayManagerReflect {
    private static final int FLAG_AUTO_MIRROR = 0x8;
    private static final Method CVD6;
    private static final Method CVD5;
    static {
        try {
            CVD6 = android.hardware.display.DisplayManager.class.getMethod(
                    "createVirtualDisplay", String.class, int.class, int.class,
                    int.class, Surface.class, int.class);
            CVD5 = android.hardware.display.DisplayManager.class.getMethod(
                    "createVirtualDisplay", String.class, int.class, int.class,
                    int.class, Surface.class);
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }
    static VirtualDisplay createVirtualDisplay(String name, int w, int h, int dpi, Surface s)
            throws Exception {
        // 优先 5 参静态（scrcpy 同款，连续渲染主屏，静态屏也出帧）；
        // 失败回退 6 参 AUTO_MIRROR 实例方法。
        try {
            return (VirtualDisplay) CVD5.invoke(null, name, w, h, dpi, s);
        } catch (Exception e5) {
            try {
                android.hardware.display.DisplayManager dm = getDisplayManager();
                return (VirtualDisplay) CVD6.invoke(dm, name, w, h, dpi, s, FLAG_AUTO_MIRROR);
            } catch (Exception e6) {
                throw new AssertionError("createVirtualDisplay both paths failed: " + e5 + " / " + e6);
            }
        }
    }
    private static android.hardware.display.DisplayManager getDisplayManager() throws Exception {
        Class<?> at = Class.forName("android.app.ActivityThread");
        Object thread = at.getMethod("systemMain").invoke(null);
        Object ctx = at.getMethod("getSystemContext").invoke(thread);
        Constructor<?> ctor = android.hardware.display.DisplayManager.class
                .getDeclaredConstructor(Context.class);
        ctor.setAccessible(true);
        return (android.hardware.display.DisplayManager) ctor.newInstance(ctx);
    }
}
