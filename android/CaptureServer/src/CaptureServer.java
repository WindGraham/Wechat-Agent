package com.wechatagent.capture;

import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.os.IBinder;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.view.Surface;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.InputStream;
import java.lang.reflect.Method;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;

/**
 * ImageReader + JPEG q100 单帧采集服务（替代 adb screencap）。
 *
 * 屏幕捕获用 scrcpy 同款机制：DisplayManager.createVirtualDisplay（hidden API）
 * 镜像主屏到 ImageReader surface。
 *
 * 后台 refreshLoop 持续 acquire 最新帧并缓存（保持虚拟显示持续渲染），
 * 客户端连上后发任意字节 → 服务返回缓存的帧 [4字节长度][JPEG]。
 * 通信走 socket（adb forward），不用 stdin/stdout（adb exec-out 不转发 stdin）。
 */
public class CaptureServer {
    private static final int WIDTH = 1080;
    private static final int HEIGHT = 2340;
    private static final int DISPLAY_ID = 0;
    private static final int PORT = 7000;

    private ImageReader imageReader;
    private VirtualDisplay virtualDisplay;
    private IBinder displayToken;
    private volatile Bitmap latestBitmap;   // 缓存最新帧

    public static void main(String[] args) throws Exception {
        Looper.prepareMainLooper();
        CaptureServer server = new CaptureServer();
        server.start();
        server.serve();     // 主线程阻塞在 accept，保持进程存活
    }

    void start() throws Exception {
        imageReader = ImageReader.newInstance(WIDTH, HEIGHT, PixelFormat.RGBA_8888, 3);
        Surface surface = imageReader.getSurface();

        try {
            virtualDisplay = DisplayManagerReflect.createVirtualDisplay(
                    "cap", WIDTH, HEIGHT, DISPLAY_ID, surface);
        } catch (Exception e1) {
            try {
                displayToken = SurfaceControlReflect.createDisplay("cap", false);
                Rect r = new Rect(0, 0, WIDTH, HEIGHT);
                SurfaceControlReflect.setDisplaySurface(displayToken, surface, r, r);
            } catch (Exception e2) {
                throw new AssertionError("could not create display");
            }
        }

    }

    void serve() {
        try {
            ServerSocket serverSocket = new ServerSocket(PORT);
            // 先等首帧就绪（latestBitmap 非空），再接受客户端连接
            while (latestBitmap == null) {
                Image img = imageReader.acquireLatestImage();
                if (img != null) {
                    Bitmap bmp = toBitmap(img);
                    img.close();
                    if (bmp != null) {
                        latestBitmap = bmp;
                    }
                } else {
                    Thread.sleep(100);
                }
            }
            while (true) {
                // 主线程刷新帧（acquireLatestImage 必须在主线程才能拿到帧）
                Image img = imageReader.acquireLatestImage();
                if (img != null) {
                    Bitmap bmp = toBitmap(img);
                    img.close();
                    if (bmp != null) {
                        latestBitmap = bmp;
                    }
                } else {
                    Thread.sleep(100);
                }
                // 非阻塞 accept，连接交给独立线程处理
                serverSocket.setSoTimeout(1);
                try {
                    final Socket socket = serverSocket.accept();
                    Thread th = new Thread(() -> {
                        try { handle(socket); } catch (Exception ignored) {}
                    });
                    th.setDaemon(true);
                    th.start();
                } catch (java.net.SocketTimeoutException e) {
                    // 无连接，继续刷新
                }
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
                Bitmap bmp = latestBitmap;
                if (bmp == null) {
                    continue;
                }
                ByteArrayOutputStream baos = new ByteArrayOutputStream();
                bmp.compress(Bitmap.CompressFormat.JPEG, 100, baos);
                byte[] jpeg = baos.toByteArray();
                out.writeInt(jpeg.length);
                out.write(jpeg);
                out.flush();
            }
        }
    }

    Bitmap toBitmap(Image img) {
        Image.Plane p = img.getPlanes()[0];
        ByteBuffer buf = p.getBuffer();
        int pixelStride = p.getPixelStride();
        int rowStride = p.getRowStride();
        int rowPad = rowStride - pixelStride * WIDTH;
        Bitmap bmp = Bitmap.createBitmap(
                WIDTH + rowPad / pixelStride, HEIGHT, Bitmap.Config.ARGB_8888);
        bmp.copyPixelsFromBuffer(buf);
        if (rowPad != 0) {
            bmp = Bitmap.createBitmap(bmp, 0, 0, WIDTH, HEIGHT);
        }
        return bmp;
    }
}

/** 反射调用 DisplayManager.createVirtualDisplay（hidden API）。 */
final class DisplayManagerReflect {
    private static final Method CVD;
    static {
        try {
            CVD = android.hardware.display.DisplayManager.class.getMethod(
                    "createVirtualDisplay", String.class, int.class, int.class,
                    int.class, Surface.class);
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }
    static VirtualDisplay createVirtualDisplay(String name, int w, int h, int id, Surface s)
            throws Exception {
        return (VirtualDisplay) CVD.invoke(null, name, w, h, id, s);
    }
}

/** 反射调用 SurfaceControl（hidden API）作为 fallback。 */
final class SurfaceControlReflect {
    private static final Class<?> SC;
    static {
        try {
            SC = Class.forName("android.view.SurfaceControl");
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }
    static IBinder createDisplay(String name, boolean secure) throws Exception {
        return (IBinder) SC.getMethod("createDisplay", String.class, boolean.class)
                .invoke(null, name, secure);
    }
    static void setDisplaySurface(IBinder token, Surface s, Rect deviceRect, Rect displayRect)
            throws Exception {
        SC.getMethod("openTransaction").invoke(null);
        try {
            SC.getMethod("setDisplaySurface", IBinder.class, Surface.class)
                    .invoke(null, token, s);
            SC.getMethod("setDisplayProjection", IBinder.class, int.class, Rect.class, Rect.class)
                    .invoke(null, token, 0, deviceRect, displayRect);
            SC.getMethod("setDisplayLayerStack", IBinder.class, int.class)
                    .invoke(null, token, 0);
        } finally {
            SC.getMethod("closeTransaction").invoke(null);
        }
    }
}
