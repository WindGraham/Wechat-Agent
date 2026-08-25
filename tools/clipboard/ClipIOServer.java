import java.io.BufferedReader;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.lang.reflect.Method;
import java.net.ServerSocket;
import java.net.Socket;

/**
 * ClipIOServer — 常驻剪贴板读取服务（Android 15 root）。
 * 与 CaptureServer 同模式：app_process 以 root 启动，绑定 tcp 7001；
 * 再 setuid 到当前聚焦 App（流程=微信 10195），以微信身份读剪贴板（过门控）。
 * 协议：'R'=[len][text]；'S'+[len][text]=写；'Q'=退出。日志走 logcat（标签 ClipIOServer）。
 */
public class ClipIOServer {
    static Object iclip;
    static final int PORT = 7001;

    static void init() throws Exception {
        Class<?> sm = Class.forName("android.os.ServiceManager");
        Object binder = sm.getMethod("getService", String.class).invoke(null, "clipboard");
        if (binder == null) throw new IllegalStateException("clipboard service null");
        Class<?> ibinder = Class.forName("android.os.IBinder");
        Class<?> stub = Class.forName("android.content.IClipboard$Stub");
        iclip = stub.getMethod("asInterface", ibinder).invoke(null, binder);
    }

    static void setuid(int uid) throws Exception {
        Class<?> os = Class.forName("android.system.Os");
        Method m = os.getMethod("setuid", int.class);
        m.setAccessible(true);
        m.invoke(null, uid);
    }

    static void logcat(String s) {
        try {
            Class<?> log = Class.forName("android.util.Log");
            log.getMethod("i", String.class, String.class).invoke(null, "ClipIOServer", s);
            try (FileOutputStream fos = new FileOutputStream("/data/local/tmp/clipio.log", true)) {
                fos.write((s + "\n").getBytes("UTF-8"));
            }
        } catch (Throwable ignored) {}
    }

    static String cmd(String... argv) throws Exception {
        Process p = Runtime.getRuntime().exec(argv);
        StringBuilder sb = new StringBuilder();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()))) {
            String line; while ((line = r.readLine()) != null) sb.append(line).append('\n');
        }
        p.waitFor();
        return sb.toString();
    }

    static String focusedPkg() throws Exception {
        for (String line : cmd("dumpsys", "window").split("\n")) {
            if (line.contains("mCurrentFocus=") && line.contains("Window{")) {
                int i = line.indexOf("mCurrentFocus=Window{");
                String rest = line.substring(i + "mCurrentFocus=Window{".length());
                int close = rest.indexOf('}');
                if (close > 0) rest = rest.substring(0, close);
                String[] toks = rest.trim().split("\\s+");
                if (toks.length > 0) return toks[toks.length - 1].split("/")[0].trim();
            }
        }
        return null;
    }

    static int appId(String pkg) throws Exception {
        for (String line : cmd("dumpsys", "package", pkg).split("\n")) {
            String t = line.trim();
            if (t.startsWith("appId=")) return Integer.parseInt(t.substring("appId=".length()).trim());
        }
        return -1;
    }

    static String read() throws Exception {
        Method gp = iclip.getClass().getMethod(
                "getPrimaryClip", String.class, String.class, int.class, int.class);
        Object cd = gp.invoke(iclip, "com.tencent.mm", null, 0, 0);
        if (cd == null) return "";
        Class<?> clipData = cd.getClass();
        Method countM = clipData.getMethod("getItemCount");
        Method itemAtM = clipData.getMethod("getItemAt", int.class);
        Method getTextM = Class.forName("android.content.ClipData$Item").getMethod("getText");
        int n = (Integer) countM.invoke(cd);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < n; i++) {
            Object item = itemAtM.invoke(cd, i);
            if (item == null) continue;
            Object t = getTextM.invoke(item);
            if (t != null) sb.append(t.toString());
        }
        return sb.toString();
    }

    static void set(String text) throws Exception {
        Class<?> clipData = Class.forName("android.content.ClipData");
        Method newPlain = clipData.getMethod("newPlainText", CharSequence.class, CharSequence.class);
        Object cd = newPlain.invoke(null, "clip", text);
        iclip.getClass().getMethod("setPrimaryClip",
                clipData, String.class, String.class, int.class, int.class)
             .invoke(iclip, cd, "com.tencent.mm", null, 0, 0);
    }

    public static void main(String[] args) throws Exception {
        init();
        String pkg = focusedPkg();
        int uid = pkg == null ? -1 : appId(pkg);
        // 先绑定（root），再 setuid —— 避免 setuid 后 bind 权限问题
        ServerSocket ss = new ServerSocket(PORT);
        logcat("bound " + PORT + " focused=" + pkg + " uid=" + uid);
        if (uid > 0) { setuid(uid); logcat("setuid -> " + uid); }
        while (true) {
            try (Socket s = ss.accept()) {
                DataInputStream in = new DataInputStream(s.getInputStream());
                DataOutputStream out = new DataOutputStream(s.getOutputStream());
                int cmd = in.read();
                if (cmd == 'R') {
                    byte[] b = read().getBytes("UTF-8");
                    out.writeInt(b.length); out.write(b); out.flush();
                } else if (cmd == 'S') {
                    int n = in.readInt();
                    byte[] b = new byte[n]; in.readFully(b);
                    set(new String(b, "UTF-8"));
                    out.writeInt(0); out.flush();
                } else if (cmd == 'P') {
                    out.writeInt(0); out.flush();   // 心跳：客户端以此判断服务健在
                } else if (cmd == 'Q') {
                    out.writeInt(0); out.flush();
                    break;
                }
            } catch (Throwable ex) {
                logcat("handler err: " + ex);
            }
        }
        ss.close();
        logcat("exited");
    }
}
