# -*- coding: utf-8 -*-
"""扫描 + 把每条完整消息的裁切图/字段发布到网关网页，供人工核对质量"""
import sys, os, random, time, json, html
sys.path.insert(0, ".")

from src.interaction.ports.android.device.device_ctl import DeviceCtl
from src.interaction.ports.android.perception.ocr_engine import run_ocr
from src.interaction.ports.android.perception.chat_slicer import slice_chat, classify_message
from src.interaction.ports.android.perception.roster_matcher import RosterMatcher
from src.shared.fling_physics import plan_swipe

GROUP = "交流一下？"
N = 8
OUT_DIR = "workspace/scan_gallery"
HTML_PATH = "src/gateway/pages/static/scan_gallery.html"

dev = DeviceCtl()
rm = RosterMatcher(GROUP)
os.makedirs(OUT_DIR, exist_ok=True)

records = {}
for direction in ("earlier", "later"):
    for i in range(N):
        img = dev.capture_bytes()
        ocr = run_ocr(img)
        res = slice_chat(img, ocr, is_group=True, title=GROUP, roster_matcher=rm)
        for m in res["messages"]:
            c = classify_message(m)
            if c["state"] != "complete":
                continue
            sender = m.get("matched_user_name") or m.get("nickname") or "我"
            key = f"{sender}|{m.get('content_type')}|{m.get('content_norm','')[:30]}"
            if key in records:
                continue
            # 保存头像裁切 + 完整消息段 + 引用块
            import cv2
            av = m.get("avatar") or {}
            files = {}
            if av:
                ax, ay, aw, ah = av["x"], av["y"], av["w"], av["h"]
                crop = img[max(0,ay):ay+ah, max(0,ax):ax+aw]
                fn = f"{len(records):03d}_avatar.png"
                cv2.imwrite(os.path.join(OUT_DIR, fn), crop)
                files["avatar"] = fn
            y0 = max(0, c["y_top"]); y1 = min(img.shape[0], c["y_bottom"])
            full = img[y0:y1, :]
            fn = f"{len(records):03d}_full.png"
            cv2.imwrite(os.path.join(OUT_DIR, fn), full)
            files["full"] = fn
            if m.get("quote_rect"):
                qx, qy, qw, qh = [int(v) for v in m["quote_rect"]]
                qcrop = img[qy:qy+qh, qx:qx+qw]
                fn = f"{len(records):03d}_quote.png"
                cv2.imwrite(os.path.join(OUT_DIR, fn), qcrop)
                files["quote"] = fn
            records[key] = {
                "sender": sender,
                "nickname": m.get("nickname"),
                "matched": m.get("matched_user_name"),
                "content": m.get("content", ""),
                "content_type": m.get("content_type"),
                "files": files,
            }
        if i < N - 1:
            t = random.uniform(1000, 1400); s = random.uniform(600, 900)
            plan = plan_swipe(t, swipe=s)
            x = int(random.uniform(400, 680)); y = int(random.uniform(1100, 1300))
            if direction == "earlier":
                dev.swipe(x, y, x, y + int(plan.swipe_px), int(plan.duration_ms))
            else:
                dev.swipe(x, y, x, y - int(plan.swipe_px), int(plan.duration_ms))
            time.sleep(0.6)

# ===== 生成 HTML =====
items = list(records.values())
css = """
body{font-family:-apple-system,sans-serif;background:#f4f6f9;color:#1f2937;margin:0;padding:20px}
h1{font-size:20px}.sub{font-size:13px;color:#6b7280;margin-bottom:20px}
.card{background:#fff;border-radius:10px;padding:14px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.08);display:flex;gap:16px}
.avatar{width:56px;height:56px;border-radius:8px;border:1px solid #d1d5db;object-fit:cover;flex-shrink:0}
.meta{flex-shrink:0;width:340px}
.meta b{font-size:14px}.tag{display:inline-block;padding:1px 7px;font-size:11px;border-radius:10px;margin-left:6px}
.tag-match{background:#def7ec;color:#03543f}.tag-nick{background:#fef3c7;color:#92400e}
.content{font-size:13px;color:#374151;background:#f9fafb;border:1px solid #e5e7eb;padding:8px;border-radius:6px;white-space:pre-wrap;margin-top:8px}
.imgs{flex:1;display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start}
.imgs img{max-height:220px;border:1px solid #d1d5db;border-radius:6px}
.lbl{font-size:10px;color:#9ca3af;margin-bottom:2px}
"""
h = [f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>扫描质量核对</title><style>{css}</style></head><body>",
     f"<h1>扫描质量核对（{len(items)} 条完整消息）</h1>",
     "<div class='sub'>每条消息：头像（独立裁切）+ 身份 + 正文；完整消息段图 = 头像+昵称+正文+引用一起的物理裁切。</div>"]
for it in items:
    f = it["files"]
    av_html = f"<img class='avatar' src='/workspace/scan_gallery/{f['avatar']}'>" if "avatar" in f else "<div class='avatar' style='background:#e5e7eb'></div>"
    tag = '<span class="tag tag-match">双因子匹配</span>' if it["matched"] else '<span class="tag tag-nick">仅OCR昵称</span>'
    quote_html = ""
    if "quote" in f:
        quote_html = f"<div><div class='lbl'>引用块（独立裁切）</div><img src='/workspace/scan_gallery/{f['quote']}'></div>"
    content = html.escape(it["content"] or "（无文本/多媒体）")
    h.append(f"""<div class='card'>
      <div>{av_html}</div>
      <div class='meta'>
        <div><b>{html.escape(it['sender'])}</b>{tag}</div>
        <div style='font-size:11px;color:#6b7280'>昵称OCR: {html.escape(it['nickname'] or '—')} | 匹配: {html.escape(it['matched'] or '—')}</div>
        <div class='content'>{content}</div>
      </div>
      <div class='imgs'>
        <div><div class='lbl'>完整消息段（头像+昵称+正文+引用）</div><img src='/workspace/scan_gallery/{f['full']}'></div>
        {quote_html}
      </div>
    </div>""")
h.append("</body></html>")
with open(HTML_PATH, "w", encoding="utf-8") as fp:
    fp.write("".join(h))
print(f"✅ 已发布 {len(items)} 条消息 → {HTML_PATH}")
print(f"网页: http://127.0.0.1:13014/static/scan_gallery.html")
