# -*- coding: utf-8 -*-
"""离线单元自测：bundle_sender 修复 + quote_reply/image_sender 假对象验证。
全程不碰手机：dev/nav/sender/tools 全是假对象，OCR 全部注入。"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from src.interaction.sender.bundle_sender import BundleSender, _extract_blocks
from src.interaction.ports.android.action import quote_reply as qr
from src.interaction.ports.android.action import image_sender as ims
from src.interaction.ports.android.action import plus_panel as pp

PASS = []
def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


# ---------------------------------------------------------------- 假对象
class R:  # nav ToolResult 替身
    success = True
    error = None

class FakeNav:
    def __init__(self): self.entered = []
    def enter_session(self, s): self.entered.append(s); return R()

class FakeSender:
    def __init__(self): self.sent = []
    def send(self, session, text): self.sent.append((session, text)); return True

class FakeDevBase:
    def __init__(self):
        self.taps = []; self.long_presses = []; self.inputs = []
        self.runs = []; self.shells = []
    def capture_bytes(self): return np.zeros((2340, 1080, 3), np.uint8)
    def tap_rect(self, rect, **kw): self.taps.append(rect); return rect.center
    def long_press_rect(self, rect, **kw): self.long_presses.append(rect)
    def input_text(self, t): self.inputs.append(t)
    def wait_random(self, a, b): pass
    def swipe_zone(self, *a, **kw): pass
    def _run(self, args, timeout=20, retries=1): self.runs.append(args); return b""
    def _shell(self, cmd, timeout=20): self.shells.append(cmd); return b""

class FakeTools:
    def __init__(self, dev): self.dev = dev

def make_bs():
    sender, nav = FakeSender(), FakeNav()
    bs = BundleSender(sender, nav, FakeTools(FakeDevBase()))
    bs._sleep = lambda s: None          # 不真的睡
    bs._rand = lambda a, b: a           # 延迟确定化
    return bs, sender, nav


# ---------------------------------------------------------------- S3: 块提取
blocks = _extract_blocks('<reply session="a"><text>hi</text></reply>'
                         '<reply session="b"><text>yo</text></reply><silent/>')
check("S3 正常多块提取", [t for t, _ in blocks] == ["reply", "reply", "silent"])

xml = '<reply session="a"><text>坏块没闭合' \
      '<reply session="b"><text>好块</text></reply>'
blocks = _extract_blocks(xml)
check("S3 坏块不污染好块", len(blocks) == 1 and "好块" in blocks[0][1],
      str([(t, b[:30]) for t, b in blocks]))

blocks = _extract_blocks('<reply><text>尾部没有闭合')
check("S3 无闭合且无后续块 -> 全丢", blocks == [])

blocks = _extract_blocks('<task session="x" desc="d">简报</task>'
                         '<reply><text>ok</text></reply>')
check("S3 task 块也能提取", [t for t, _ in blocks] == ["task", "reply"])

# 嵌套文本：text 内含转义实体，块边界不受影响
blocks = _extract_blocks('<reply><text>a &lt;text&gt; b &amp; c</text></reply>')
check("S3 转义文本不干扰块边界", len(blocks) == 1)


# ---------------------------------------------------------------- S1: 拆句不合并
bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<reply session="s"><text>第一句</text>'
                          '<text>第二句</text><text>第三句</text></reply>')
check("S1 多 <text> 逐条发送", r.ok and [t for _, t in sender.sent] ==
      ["第一句", "第二句", "第三句"], str(sender.sent))
check("S1 只进一次会话", nav.entered == ["s"])

long_text = "这是一段很长的话。" * 30
bs, sender, nav = make_bs()
r = bs.submit_bundle("s", f'<reply><text>{long_text}</text></reply>')
check("S1 单条长文不拆", r.ok and len(sender.sent) == 1
      and sender.sent[0][1] == long_text)

# 条间延迟 1~3s 被调用
bs, sender, nav = make_bs()
sleeps = []
bs._sleep = sleeps.append
bs.submit_bundle("s", '<reply><text>a</text><text>b</text></reply>')
check("S1 条间有随机延迟", len(sleeps) == 1 and 1.0 <= sleeps[0] <= 3.0,
      str(sleeps))


# ---------------------------------------------------------------- S2: 不双重反转义
bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<reply><text>a &lt; b &amp; c</text></reply>')
check("S2 ET 反转义一次", r.ok and sender.sent[0][1] == "a < b & c",
      repr(sender.sent[0][1]) if sender.sent else "nothing sent")

bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<reply><text>显示 &amp;lt; 字面</text></reply>')
check("S2 不二次反转义（&amp;lt; -> &lt; 而非 <）",
      r.ok and sender.sent[0][1] == "显示 &lt; 字面",
      repr(sender.sent[0][1]) if sender.sent else "nothing sent")


# ---------------------------------------------------------------- 包级行为
bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<silent/>')
check("silent 无动作", r.ok and sender.sent == [])

bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<reply><text>坏块没闭合'
                          '<reply><text>好块</text></reply>')
check("坏块丢弃后好块照发", r.ok and sender.sent == [("s", "好块")],
      str(sender.sent))

bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<task session="s" desc="d">简报</task>')
check("task 块跳过且报错（无 reply）", not r.ok and sender.sent == [])

bs, sender, nav = make_bs()
bs._mutex = True
r = bs.submit_bundle("s", '<reply><text>x</text></reply>')
check("屏幕互斥锁", not r.ok and r.retryable and sender.sent == [])

# 最多 3 个 reply 块
bs, sender, nav = make_bs()
xml = "".join(f'<reply><text>m{i}</text></reply>' for i in range(5))
r = bs.submit_bundle("s", xml)
check("最多 3 个 reply 块", r.ok and len(sender.sent) == 3)


# ---------------------------------------------------------------- S4: quote
# bundle_sender 接线：quote 失败降级普通发送（每条 @ 必回优先于带引用，
# 2026-08-09 用户要求），不再整体判失败
import src.interaction.ports.android.action.quote_reply as qr_mod
orig_qr = qr_mod.quote_reply
qr_mod.quote_reply = lambda *a, **kw: {"ok": False, "step": "detect_menu",
                                       "error": "长按后未检测到菜单展开"}
bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<reply><quote match="周六去爬山吗"/>'
                          '<text>来啦</text></reply>')
check("S4 quote 失败 -> 降级普通发送", r.ok and sender.sent == [("s", "来啦")],
      f"ok={r.ok} sent={sender.sent} err={r.error}")

qr_mod.quote_reply = lambda *a, **kw: {"ok": True, "step": "sent",
                                       "verified": True}
bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<reply><quote match="爬山"/>'
                          '<text>来啦</text><text>等我十分钟</text></reply>')
check("S4 quote 成功 + 剩余 text 照发", r.ok
      and sender.sent == [("s", "等我十分钟")], str(sender.sent))

qr_mod.quote_reply = orig_qr

# quote_reply 八步流程（假 dev + 注入 OCR + 打桩 parse_chat）
class QuoteDev(FakeDevBase):
    pass

MENU = [("复制", 130, 900), ("转发", 280, 900), ("收藏", 430, 900),
        ("删除", 580, 900), ("多选", 730, 900),
        ("引用", 130, 1100), ("提醒", 280, 1100), ("翻译", 430, 1100),
        ("搜一搜", 580, 1100)]
def items_of(pairs):
    return [{"text": t, "cx": x, "cy": y} for t, x, y in pairs]

dev = QuoteDev()
ocr_seq = [
    items_of([("特高课(6)", 400, 150)]),                       # 1 找目标（parse_chat 打桩）
    items_of(MENU),                                           # 2 菜单
    items_of([("周六去爬山吗", 400, 1900)]),                  # 3 引用预览条
    items_of([("发送", 980, 2070)]),                          # 4 发送按钮
    items_of([("来啦", 400, 800)]),                           # 5 轻验证
]
def fake_ocr(img):
    return ocr_seq.pop(0) if ocr_seq else []

orig_parse = qr.parse_chat
qr.parse_chat = lambda img, items, title: (
    "特高课", [{"type": "message_bubble", "sender": "Leisure",
                "content": "周六去爬山吗", "is_mine": False,
                "position": {"x": 100, "y": 500, "w": 400, "h": 80}}],
    None, None, None)
res = qr.quote_reply(dev, match_text="爬山", reply_text="来啦",
                     ocr_fn=fake_ocr, sleep_fn=lambda s: None)
check("S4 quote_reply 八步成功", res["ok"] and res["verified"], str(res))
check("S4 quote_reply 长按+输入+三次tap（聚焦+引用+发送）",
      len(dev.long_presses) == 1 and dev.inputs == ["来啦"]
      and len(dev.taps) == 3,
      f"lp={len(dev.long_presses)} in={dev.inputs} taps={len(dev.taps)}")
qr.parse_chat = orig_parse

# 菜单未展开 -> 失败带步骤
dev2 = QuoteDev()
res = qr.quote_reply(dev2, match_text="爬山", reply_text="来啦",
                     ocr_fn=lambda img: [], sleep_fn=lambda s: None)
check("S4 quote_reply 无目标失败", not res["ok"] and res["step"] == "find_target",
      str(res))

# ---------------------------------------------------------------- S4: image/file
r = ims.send_image(FakeDevBase(), "relative/pic.jpg")
check("S4 image 拒绝相对路径", not r["ok"] and r["step"] == "args", r["error"])
r = ims.send_image(FakeDevBase(), "/tmp/not_exist_xxx.jpg")
check("S4 image 文件不存在", not r["ok"] and r["step"] == "args", r["error"])
r = ims.send_file(FakeDevBase(), "a/b.pdf")
check("S4 file 拒绝相对路径", not r["ok"] and r["step"] == "args", r["error"])

# image 全流程（假 dev + 打桩 OCR/面板检测）
import cv2, tempfile, os
img_path = os.path.join(tempfile.mkdtemp(), "pic.jpg")
cv2.imwrite(img_path, np.zeros((50, 50, 3), np.uint8))

dev3 = FakeDevBase()
panel_state = {"open": False}
orig_tap_rect = dev3.tap_rect
def tap_and_open(rect, **kw):          # 点 ⊕ 后面板才"打开"
    from src.interaction.ports.android.device import layout as _lay
    if rect in (_lay.CHAT_PLUS, ims.CHAT_PLUS_FOCUSED):
        panel_state["open"] = True
    return orig_tap_rect(rect, **kw)
dev3.tap_rect = tap_and_open
orig_items, orig_tap = pp.plus_panel_items, pp.plus_panel_tap
orig_ocr = ims.run_ocr
def fake_items(d):
    return {"success": panel_state["open"], "page": 1, "items": []}
def fake_panel_tap(d, label):
    panel_state["open"] = True
    return {"success": True, "page": 1, "label": label, "tap": (166, 1767)}
def fake_ocr_img(img):
    # 相册页标题 + 发送按钮都在；聚焦细条不在（未聚焦态）
    return items_of([("图片和视频", 400, 150), ("发送(1)", 980, 2070)])
pp.plus_panel_items = fake_items
pp.plus_panel_tap = fake_panel_tap
ims.run_ocr = fake_ocr_img
r = ims.send_image(dev3, img_path)
check("S4 image 全流程成功", r["ok"], str(r))
check("S4 image push+媒体扫描+清理",
      any(a[0] == "push" for a in dev3.runs)
      and any("MEDIA_SCANNER_SCAN_FILE" in c for c in dev3.shells)
      and any(c.startswith("rm -f") for c in dev3.shells),
      f"runs={dev3.runs} shells={dev3.shells}")
check("S4 image 点开面板+选图+点发送", len(dev3.taps) >= 3,
      f"{len(dev3.taps)} taps")
pp.plus_panel_items, pp.plus_panel_tap = orig_items, orig_tap
ims.run_ocr = orig_ocr

# bundle_sender 接线 image：失败 ok=False
orig_si = ims.send_image
ims.send_image = lambda dev, path: {"ok": False, "step": "album_open",
                                    "error": "相册选择页未打开"}
bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<reply><image path="/tmp/x.jpg"/></reply>')
check("S4 bundle image 失败 -> ok=False", not r.ok and "相册选择页未打开" in r.error,
      r.error)
ims.send_image = lambda dev, path: {"ok": True, "step": "sent"}
bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<reply><image path="/tmp/x.jpg"/></reply>')
check("S4 bundle image 成功", r.ok and nav.entered == ["s"])
ims.send_image = orig_si

# image+file 混用拒绝
bs, sender, nav = make_bs()
r = bs.submit_bundle("s", '<reply><image path="/a.jpg"/><file path="/b.pdf"/></reply>')
check("image+file 混用拒绝", not r.ok and not r.retryable, r.error)

# ---------------------------------------------------------------- sender.py 回归
from src.interaction.ports.android.action.sender import Sender
class FakeWT:
    class RT: success = True; error = None
    def __init__(self): self.texts = []
    def send_text(self, t): self.texts.append(t); return self.RT()
wt = FakeWT()
s = Sender(wt, sleep_fn=lambda x: None, rand_fn=lambda a, b: a)
s.send("s", "[AGENTS_UPDATE] 旧指令按普通文本发出去")
check("sender 无 AGENTS_UPDATE 特判", wt.texts != ["已更新工作指南"],
      str(wt.texts))
check("sender 无 PROJECT_ROOT 残留", not hasattr(
    sys.modules["src.interaction.ports.android.action.sender"], "PROJECT_ROOT"))

# ---------------------------------------------------------------- wechat_tools __main__
import subprocess
p = subprocess.run([sys.executable,
                    "/media/data_old/wechat-agent/src/interaction/ports/android/action/wechat_tools.py"],
                   capture_output=True, text=True, timeout=60)
check("wechat_tools __main__ 只警告退出", p.returncode == 1
      and "已禁用" in p.stdout, f"rc={p.returncode} out={p.stdout.strip()[:50]}")

# ---------------------------------------------------------------- 汇总
fails = [n for n, ok in PASS if not ok]
print(f"\n{'='*50}\n{len(PASS)-len(fails)}/{len(PASS)} passed")
if fails:
    print("FAILED:", fails); sys.exit(1)
