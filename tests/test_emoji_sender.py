# -*- coding: utf-8 -*-
"""离线单元自测：表情包检索（emoji_index）+ bundle_sender <sticker> 发送编排。
全程不碰手机：dev/nav/sender 全是假对象；EmojiIndex 用临时 sqlite db + 空文件。"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.emoji_index import EmojiIndex
from src.interaction.sender.bundle_sender import BundleSender

PASS = []
def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


# ---------------------------------------------------------------- 临时表情库
def make_index():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "index.db")
    img_dir = os.path.join(tmp, "renamed")
    os.makedirs(img_dir)
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE emojis (
            seq INTEGER PRIMARY KEY, filename TEXT NOT NULL, ext TEXT NOT NULL,
            original_md5 TEXT NOT NULL UNIQUE, description TEXT, text_content TEXT,
            is_real INTEGER DEFAULT 0, style TEXT, mood TEXT, use_case TEXT,
            keywords TEXT, frames INTEGER DEFAULT 1, filesize INTEGER,
            processed_at TEXT);
    """)
    rows = [
        (1, "000001.gif", ".gif", "a", "黑色文字好吧", "好吧", 0, "文字", "无奈", "妥协", '["文字"]', 1, 100),
        (2, "000002.png", ".png", "b", "一只猫在笑", None, 0, "卡通", "开心", "开心场景", '["猫"]', 1, 200),
        (3, "000003.jpg", ".jpg", "c", "真人自拍照片", None, 1, "真人", "中性", "真人照片", '["真人"]', 1, 300),
        (4, "000004.gif", ".gif", "d", "文件丢失的表情", None, 0, "卡通", "无语", "无语", '["missing"]', 1, 400),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO emojis (seq,filename,ext,original_md5,description,"
            "text_content,is_real,style,mood,use_case,keywords,frames,filesize) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    conn.commit(); conn.close()
    # 真实文件（seq=4 故意不建，测文件丢失跳过）
    for fn in ("000001.gif", "000002.png", "000003.jpg"):
        open(os.path.join(img_dir, fn), "w").close()
    return EmojiIndex(db, img_dir)


idx = make_index()

# ---------------------------------------------------------------- 检索
e = idx.get(1)
check("get(1) 返回 seq=1", e and e["seq"] == 1 and e["path"].endswith("000001.gif"))
check("get(999) 返回 None", idx.get(999) is None)

hits = idx.search("猫", exclude_real=True)
check("search 猫 -> seq=2", [h["seq"] for h in hits] == [2], str([h["seq"] for h in hits]))
check("search 默认排除真人照片", idx.search("真人", exclude_real=True) == [],
      "is_real=1 应被过滤")
hits = idx.search("真人", exclude_real=False)
check("search exclude_real=False 可搜到真人", [h["seq"] for h in hits] == [3])

e = idx.resolve(query="好吧")
check("resolve 文字精确 -> seq=1", e and e["seq"] == 1, str(e))
e = idx.resolve(query="猫")
check("resolve 模糊 -> seq=2", e and e["seq"] == 2, str(e))
e = idx.resolve(seq="3")
check("resolve seq 精确指定不过滤真人", e and e["seq"] == 3 and e["is_real"])
check("resolve 文件丢失自动跳过", idx.resolve(query="文件丢失") is None)
check("resolve 无匹配 -> None", idx.resolve(query="不存在的词xyz") is None)


# ---------------------------------------------------------------- bundle_sender sticker 编排
class R:
    success = True
    error = None

class FakeNav:
    def __init__(self): self.entered = []
    def enter_session(self, s): self.entered.append(s); return R()

class FakeSender:
    def __init__(self): self.sent = []
    def send(self, session, text): self.sent.append((session, text)); return True

class FakeDev:
    pass

class FakeTools:
    def __init__(self, dev): self.dev = dev

def make_bs(index):
    sender, nav = FakeSender(), FakeNav()
    bs = BundleSender(sender, nav, FakeTools(FakeDev()), emoji_index=index)
    bs._sleep = lambda s: None
    bs._rand = lambda a, b: a
    return bs, sender, nav

import src.interaction.ports.android.action.image_sender as ims
sent_paths = []
orig_si = ims.send_image
def fake_send_image(dev, path):
    sent_paths.append(path)
    return {"ok": True, "step": "sent", "path": path}
ims.send_image = fake_send_image

# query 盲发被拒（必须先走 emoji 工具搜索）
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><sticker query="猫"/></reply>')
check("sticker query 盲发被拒", not r.ok and not r.retryable and "盲发" in (r.error or ""),
      r.error)
check("query 盲发不发图", sent_paths == [], str(sent_paths))

# text + sticker query：query 报错，text 也不发
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><text>哈哈哈</text><sticker query="猫"/></reply>')
check("text+sticker query 被拒且不发文字", not r.ok and sender.sent == [],
      f"sent={sender.sent}")

# seq 精确发送
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><sticker seq="1"/></reply>')
check("sticker seq 精确", r.ok and sent_paths[-1].endswith("000001.gif"))

# text + sticker seq 顺序（先文字后表情）
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><text>哈哈哈</text><sticker seq="1"/></reply>')
check("text+sticker seq 先文字后表情", r.ok and sender.sent == [("s", "哈哈哈")]
      and sent_paths[-1].endswith("000001.gif"), f"sent={sender.sent}")

# 混用拒绝
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><image path="/a.jpg"/><sticker seq="1"/></reply>')
check("image+sticker 混用拒绝", not r.ok and not r.retryable, r.error)

# 缺属性
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><sticker/></reply>')
check("sticker 缺 seq 报错", not r.ok and not r.retryable, r.error)

# seq 非数字
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><sticker seq="abc"/></reply>')
check("sticker seq 非数字报错", not r.ok and not r.retryable, r.error)

# seq 不存在
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><sticker seq="9999"/></reply>')
check("sticker seq 不存在报错", not r.ok and not r.retryable, r.error)

# text + file 混合：先发文字，再发文件（2026-08-13 修复 text 被忽略）
sent_files = []
orig_sf = ims.send_file
ims.send_file = lambda dev, path: (sent_files.append(path),
                                   {"ok": True, "step": "sent"})[1]
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><text>报告来啦</text><file path="/tmp/x.pdf"/></reply>')
check("text+file 先文字后文件", r.ok and sender.sent == [("s", "报告来啦")]
      and sent_files == ["/tmp/x.pdf"], f"sent={sender.sent} files={sent_files}")
ims.send_file = orig_sf

# text + image 混合：先发文字，再发图片
bs, sender, nav = make_bs(idx)
r = bs.submit_bundle("s", '<reply><text>图来了</text><image path="/tmp/x.jpg"/></reply>')
check("text+image 先文字后图片", r.ok and sender.sent == [("s", "图来了")]
      and sent_paths[-1] == "/tmp/x.jpg", f"sent={sender.sent}")

ims.send_image = orig_si

# ---------------------------------------------------------------- CV 选图检测
import cv2 as _cv2
import numpy as _np
from src.interaction.ports.android.action.image_sender import (
    _find_select_circle, _find_select_circles)

# 合成"相册选择页"：深色背景 + 第一张图白色空心圆环（模拟选择圆圈）+ 实心圆干扰
_syn = _np.full((2340, 1080, 3), 50, _np.uint8)
_cv2.circle(_syn, (218, 256), 30, (255, 255, 255), 3)    # 选择圆圈（空心）
_cv2.circle(_syn, (400, 500), 25, (180, 180, 180), -1)   # 缩略图内容实心圆（干扰）
_pos = _find_select_circle(_syn)
check("CV 检测白色空心圆环", _pos is not None
      and abs(_pos[0] - 218) < 12 and abs(_pos[1] - 256) < 12, str(_pos))
check("CV 排除实心圆干扰", _pos is not None
      and not (abs(_pos[0] - 400) < 50 and abs(_pos[1] - 500) < 50))

_blank = _np.full((2340, 1080, 3), 50, _np.uint8)
check("CV 无圆圈返回 None", _find_select_circle(_blank) is None)

# 检测多个圆圈（两张图两个圆圈），第一张 = 最左上角
_grid = _np.full((2340, 1080, 3), 50, _np.uint8)
_cv2.circle(_grid, (218, 257), 30, (255, 255, 255), 3)
_cv2.circle(_grid, (489, 257), 30, (255, 255, 255), 3)
check("CV 检测多个圆圈", len(_find_select_circles(_grid)) == 2,
      str(_find_select_circles(_grid)))
_first = _find_select_circle(_grid)
check("CV 第一张圆圈 = 最左上角", _first is not None
      and abs(_first[0] - 218) < 12 and abs(_first[1] - 257) < 12, str(_first))

# ---------------------------------------------------------------- 汇总
fails = [n for n, ok in PASS if not ok]
print(f"\n{'='*50}\n{len(PASS)-len(fails)}/{len(PASS)} passed")
if fails:
    print("FAILED:", fails); sys.exit(1)
