import java.io.BufferedReader;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.lang.reflect.Method;

/** 真机剪贴板读取（Android 15 root）。路由：app_process + IClipboard + setuid(聚焦App uid)。 */
public class ClipIO {
    static Object iclip;

    static void init() throws Exception {
        Class<?> sm = Class.forName("android.os.ServiceManager");
        Object binder = sm.getMethod("getService", String.class).invoke(null, "clipboard");
        if (binder == null) throw new IllegalStateException("clipboard service null");
        Class<?> ibinder = Class.forName("android.os.IBinder");
        iclip = Class.forName("android.content.IClipboard$Stub")
                .getMethod("asInterface", ibinder).invoke(null, binder);
    }

    static void setuid(int uid) throws Exception {
        Class<?> os = Class.forName("android.system.Os");
        Method m = os.getMethod("setuid", int.class);
        m.setAccessible(true);
        m.invoke(null, uid);
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

    /** 聚焦窗口包名，如 com.tencent.mm（优先 mCurrentFocus）。 */
    static String focusedPkg() throws Exception {
        String w = cmd("dumpsys", "window");
        for (String line : w.split("\n")) {
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
        String d = cmd("dumpsys", "package", pkg);
        for (String line : d.split("\n")) {
            String t = line.trim();
            if (t.startsWith("appId=")) return Integer.parseInt(t.substring("appId=".length()).trim());
        }
        return -1;
    }

    static String read() throws Exception {
        String pkg = focusedPkg();
        if (pkg == null) throw new IllegalStateException("no focused pkg");
        int uid = appId(pkg);
        if (uid < 0) throw new IllegalStateException("no appId for " + pkg);
        setuid(uid);   // 以聚焦 App 身份读剪贴板（过 Android 15 access gate）
        Method gp = iclip.getClass().getMethod(
                "getPrimaryClip", String.class, String.class, int.class, int.class);
        Object cd = gp.invoke(iclip, pkg, null, 0, 0);
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
        String out;
        try {
            if (args.length > 0 && args[0].equals("set")) { set(args[1]); out = "SET"; }
            else out = read();
        } catch (Throwable e) {
            StringBuilder sb = new StringBuilder("ERR[").append(e.getClass().getSimpleName()).append("] ");
            Throwable c = e;
            while (c != null) { sb.append(c.getMessage() == null ? c.getClass().getSimpleName() : c.getMessage()).append(" <- "); c = c.getCause(); }
            out = sb.toString();
        }
        write(out);
    }

    static void write(String s) throws Exception {
        try (FileOutputStream fos = new FileOutputStream("/data/local/tmp/clip_read.txt")) {
            fos.write(s.getBytes("UTF-8"));
        }
    }
}
