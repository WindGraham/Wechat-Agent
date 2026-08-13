# -*- coding: utf-8 -*-
"""generate_crop_gallery.py — 只呈现原截图 + 裁切框标注（hover 看详情）"""

import json
import html as html_mod

JSON_PATH = "workspace/test_cropped_messages/crop_report.json"
HTML_PATH = "src/gateway/pages/static/crop_gallery.html"
SCREEN_H = 2340   # OnePlus 6T 竖屏高度（1080x2340）

with open(JSON_PATH, "r", encoding="utf-8") as f:
    reports = json.load(f)

CSS = """
  body { font-family: -apple-system, sans-serif; background: #f4f6f9; color: #1f2937; margin: 0; padding: 20px; }
  h1 { font-size: 20px; margin-bottom: 8px; }
  .subtitle { font-size: 13px; color: #6b7280; margin-bottom: 24px; }
  .screen-card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  .screen-title { font-size: 16px; font-weight: 600; margin-bottom: 16px; border-bottom: 1px solid #e5e7eb; padding-bottom: 8px; }
  .shot-row { display: flex; gap: 28px; align-items: flex-start; }
  .source-wrap { position: relative; width: 360px; flex-shrink: 0; }
  .source-img { width: 100%; display: block; border: 1px solid #d1d5db; border-radius: 6px; }
  .crop-box { position: absolute; left: 0; width: 100%; box-sizing: border-box; border: 2px solid; cursor: pointer; opacity: 0.5; transition: opacity .15s; }
  .crop-box:hover { opacity: 1; }
  .crop-box .tag { position: absolute; left: 0; top: 0; font-size: 9px; font-weight: 700; color: #fff; padding: 1px 4px; border-radius: 2px; white-space: nowrap; line-height: 1.3; }
  .crop-box .tooltip { display: none; position: absolute; left: calc(100% + 8px); top: -2px; width: 280px; background: #1f2937; color: #f9fafb; padding: 10px 12px; border-radius: 8px; z-index: 50; font-size: 12px; line-height: 1.5; box-shadow: 0 4px 12px rgba(0,0,0,0.3); pointer-events: none; }
  .crop-box:hover .tooltip { display: block; }
  .tooltip .tt-title { font-weight: 700; margin-bottom: 4px; }
  .tooltip .tt-range { color: #93c5fd; font-size: 11px; margin-bottom: 4px; }
  .tooltip .tt-content { color: #d1d5db; white-space: pre-wrap; max-height: 120px; overflow: hidden; }
  .box-strong { border-color: #10b981; } .box-strong .tag { background: #10b981; }
  .box-ocr { border-color: #f59e0b; } .box-ocr .tag { background: #f59e0b; }
  .box-sys { border-color: #9ca3af; } .box-sys .tag { background: #9ca3af; }
  .legend { font-size: 12px; color: #6b7280; line-height: 2; position: sticky; top: 20px; }
  .legend span { display: inline-block; width: 12px; height: 12px; border-radius: 2px; vertical-align: -2px; margin-right: 4px; }
"""

html = ["<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<title>消息裁切标注可视化</title>",
        "<style>", CSS, "</style></head><body>",
        "<h1>📷 原截图 + 裁切框标注</h1>",
        "<div class='subtitle'>每个色框 = 一条被裁切的消息（框内编号 msgN，颜色代表识别状态），鼠标悬停查看该条消息的详情。</div>"]

for scr in reports:
    scr_name = scr["screenshot"]
    items = scr["items"]
    html.append(f'<div class="screen-card"><div class="screen-title">{scr_name} · 切出 {len(items)} 条消息</div>')
    html.append('<div class="shot-row"><div class="source-wrap">')
    html.append(f'<img src="/workspace/test_screenshots/{scr_name}" class="source-img" alt="{scr_name}">')
    for it in items:
        yt = it.get("y_top", 0)
        yb = it.get("y_bottom", yt + 100)
        top_pct = yt / SCREEN_H * 100
        h_pct = max((yb - yt) / SCREEN_H * 100, 0.8)
        st = it.get("status_tag", "")
        cls = "box-strong" if "强匹配" in st else ("box-ocr" if "仅OCR" in st or "确认" in st else "box-sys")
        sender = html_mod.escape(it.get("sender", ""))
        content = html_mod.escape(it.get("content") or "（无文本/多媒体消息）")
        html.append(
            f'<div class="crop-box {cls}" style="top:{top_pct:.2f}%;height:{h_pct:.2f}%">'
            f'<span class="tag">msg{it["idx"]}</span>'
            f'<div class="tooltip">'
            f'<div class="tt-title">msg{it["idx"]} · {sender}</div>'
            f'<div class="tt-range">y={yt} ~ {yb}</div>'
            f'<div class="tt-content">{content}</div>'
            f'</div></div>')
    html.append('</div>')  # source-wrap
    html.append('<div class="legend">'
                '<div><span style="background:#10b981"></span>双因子强匹配</div>'
                '<div><span style="background:#f59e0b"></span>待资料页确认</div>'
                '<div><span style="background:#9ca3af"></span>时间戳 / 系统</div>'
                '</div></div></div>')  # legend + shot-row + screen-card

html.append("</body></html>")

with open(HTML_PATH, "w", encoding="utf-8") as f:
    f.write("".join(html))

print(f"✅ 可视化 HTML 页面已生成: {HTML_PATH}")
