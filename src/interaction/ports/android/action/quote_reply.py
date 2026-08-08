# -*- coding: utf-8 -*-
"""quote_reply.py — 长按消息菜单识别 + 引用回复流程（action 层）。

移植自旧仓库 src/v2/longpress_menu.py，适配新包路径：
- normalize:   interaction.msglog.message_log
- Rect:        ..device.random_touch
- 布局常量:    ..perception.layout_consts（INPUT_BAR_Y0 / SEND_SCAN_ZONE / 绿掩膜阈值）
- 聊天气泡解析: ..perception.chat_parser.parse_chat
- 发送按钮 Rect: ..device.layout.SEND_BTN（与旧 SEND_BTN_RECT 同值 917,2033,135,86）

长按一条消息浮出深色圆角面板，9 个选项两行排列（上排 复制/转发/收藏/删除/多选，
下排 引用/提醒/翻译/搜一搜），每项"图标在上 + 短标签在下"，面板位置随被按气泡变化。
本模块只认文字标签（图标不识别），点击点 = 标签文字中心。

- detect_longpress_menu(img, ocr_items)：OCR 项里找 9 个短标签，精确匹配优先、
  归一化模糊匹配兜底；>=6 个不同标签命中且通过面板聚类校验
  （y 聚成 1~2 行、行间距 ~200px、行内 x 均匀分布）才认定菜单展开。
- quote_reply(dev, ...)：找目标消息 -> 长按气泡 -> 验菜单 -> 点"引用" ->
  验输入栏引用预览条 -> 输入 -> 验发送按钮 -> 发送。每一步失败返回带原因的
  dict，绝不硬点。

dev 协议（可注入假 dev 测试）：
  必需：capture_bytes() -> BGR ndarray；input_text(text)
  点按：dev.tap_rect(Rect)（DeviceCtl 主 API）或 dev.tap(x, y)（假 dev）
  长按：dev.long_press_rect(Rect)（DeviceCtl 主 API）或
        dev.long_press(x, y, dwell_ms)（假 dev）
  可选：wait_random(lo_ms, hi_ms)
"""

import logging
import random
import time
from difflib import SequenceMatcher

import cv2

from ....msglog.message_log import normalize
from ..device import layout
from ..device.random_touch import Rect
from ..perception import layout_consts as LC
from ..perception.chat_parser import parse_chat

log = logging.getLogger("action.quote_reply")

LONGPRESS_OPTIONS = ("复制", "转发", "收藏", "删除", "多选",
                     "引用", "提醒", "翻译", "搜一搜")

MENU_MIN_HITS = 6                 # 至少命中多少个不同标签才认定菜单展开
ROW_SAME_GAP = 110                # 同一行标签 cy 差上限（行内抖动/OCR 框高差）
PANEL_ROW_GAP = (120, 360)        # 两行面板的行间距（实测 ~200px）
COL_GAP = (40, 330)               # 同行相邻标签 cx 间距合理区间（均匀分布，实测 ~150）

# 引用预览条判定带：输入栏顶 (LC.INPUT_BAR_Y0) 附近；聚焦态输入栏被顶起 ~115px，
# 预览条贴输入栏上方，故向上多留余量。
_PREVIEW_BAND = (LC.INPUT_BAR_Y0 - 340, LC.INPUT_BAR_Y0 + 130)


# ------------------------------------------------------------------ 菜单识别
def _item_cxy(it):
    """兼容 run_ocr 格式 (cx/cy) 与样本 .ocr.json 格式 (center/bbox)。"""
    if "cx" in it and "cy" in it:
        return float(it["cx"]), float(it["cy"])
    if "center" in it:
        c = it["center"]
        return float(c["x"]), float(c["y"])
    b = it.get("bbox") or {}
    return float(b.get("x", 0) + b.get("w", 0) / 2), \
        float(b.get("y", 0) + b.get("h", 0) / 2)


def _fuzzy_label(text):
    """对已知标签集合做归一化模糊匹配（OCR 小错容错）。
    2 字标签允许错 1 字（ratio>=0.5），3 字允许错 1 字（ratio>=0.6）。"""
    nt = normalize(text)
    if not (2 <= len(nt) <= 4):
        return None
    best_label, best_r = None, 0.0
    for label in LONGPRESS_OPTIONS:
        nl = normalize(label)
        if abs(len(nt) - len(nl)) > 1:
            continue
        r = SequenceMatcher(None, nt, nl).ratio()
        if r > best_r:
            best_label, best_r = label, r
    if best_label is None:
        return None
    threshold = 0.5 if min(len(nt), len(normalize(best_label))) <= 2 else 0.6
    return best_label if best_r >= threshold else None


def _collect_hits(ocr_items):
    """OCR 项 -> 标签命中点列表 [{label, cx, cy, exact}]。精确优先：
    某标签有精确命中时丢弃它的模糊命中。"""
    hits = []
    for it in ocr_items:
        text = (it.get("text") or "").strip()
        if not text or len(text) > 4:
            continue
        cx, cy = _item_cxy(it)
        if text in LONGPRESS_OPTIONS:
            hits.append({"label": text, "cx": cx, "cy": cy, "exact": True})
            continue
        label = _fuzzy_label(text)
        if label:
            hits.append({"label": label, "cx": cx, "cy": cy, "exact": False})
    exact_labels = {h["label"] for h in hits if h["exact"]}
    return [h for h in hits if h["exact"] or h["label"] not in exact_labels]


def _row_group(hits):
    """按 cy 聚行：相邻点 cy 差 <= ROW_SAME_GAP 归同一行。返回 [ [hit...], ... ]。"""
    rows = []
    for h in sorted(hits, key=lambda h: h["cy"]):
        if rows and h["cy"] - rows[-1][-1]["cy"] <= ROW_SAME_GAP:
            rows[-1].append(h)
        else:
            rows.append([h])
    return rows


def _trim_row_x(row):
    """行内按 cx 排序，在间距异常处断开，保留最长均匀段（剔除非面板的同名杂点）。"""
    pts = sorted(row, key=lambda h: h["cx"])
    best, cur = [], []
    for h in pts:
        if cur and not (COL_GAP[0] <= h["cx"] - cur[-1]["cx"] <= COL_GAP[1]):
            if len(cur) > len(best):
                best = cur
            cur = []
        cur.append(h)
    if len(cur) > len(best):
        best = cur
    return best


def _dedupe_labels(pts):
    """同名歧义：保留精确命中；都是精确/都是模糊时保留先出现的。"""
    by_label = {}
    for h in pts:
        cur = by_label.get(h["label"])
        if cur is None or (h["exact"] and not cur["exact"]):
            by_label[h["label"]] = h
    return by_label


def detect_longpress_menu(img, ocr_items):
    """识别长按菜单是否展开。返回 {label: {"center": (cx, cy)}} 或 None。

    img 当前不参与判定（标签全靠 OCR），保留以便后续加面板底色校验。"""
    del img
    hits = _collect_hits(ocr_items)
    if len({h["label"] for h in hits}) < MENU_MIN_HITS:
        return None
    rows = _row_group(hits)

    # 候选面板：单行，或行间距 ~200px 的相邻两行
    candidates = [[r] for r in rows]
    for r1, r2 in zip(rows, rows[1:]):
        med1 = sorted(h["cy"] for h in r1)[len(r1) // 2]
        med2 = sorted(h["cy"] for h in r2)[len(r2) // 2]
        if PANEL_ROW_GAP[0] <= med2 - med1 <= PANEL_ROW_GAP[1]:
            candidates.append([r1, r2])

    best = None
    for cand in candidates:
        pts = []
        for row in cand:
            pts.extend(_trim_row_x(row))
        by_label = _dedupe_labels(pts)
        n = len(by_label)
        if n < MENU_MIN_HITS:
            continue
        if len(cand) == 2:      # 两行面板每行至少 2 项（实测 5+4）
            row_labels = [{h["label"] for h in _trim_row_x(row)} for row in cand]
            if min(len(s) for s in row_labels) < 2:
                continue
        if best is None or n > len(best):
            best = by_label
    if best is None:
        return None
    return {label: {"center": (h["cx"], h["cy"])} for label, h in best.items()}


# ------------------------------------------------------------------ dev 适配
def _dev_tap(dev, x, y):
    if hasattr(dev, "tap_rect"):
        return dev.tap_rect(Rect(int(x) - 22, int(y) - 22, 44, 44))
    return dev.tap(int(x), int(y))


def _dev_long_press(dev, rect):
    """长按气泡。DeviceCtl.long_press_rect 内置 500~800ms 随机按压；
    假 dev 用 long_press(x, y, dwell_ms) 600~900ms 随机。"""
    if hasattr(dev, "long_press_rect"):
        return dev.long_press_rect(rect)
    cx, cy = rect.center
    return dev.long_press(int(cx), int(cy), random.randint(600, 900))


def _dev_wait(dev, sleep_fn, lo_ms, hi_ms):
    if hasattr(dev, "wait_random"):
        dev.wait_random(lo_ms, hi_ms)
    else:
        sleep_fn(random.uniform(lo_ms, hi_ms) / 1000.0)


def _default_ocr(img):
    from ..perception.ocr_engine import run_ocr
    return run_ocr(img)


# ------------------------------------------------------------------ 校验辅助
def _title_from_items(ocr_items):
    """标题栏文本（跳过左上角未读数胶囊），供 parse_chat 当 raw_title 用。"""
    parts = [it for it in ocr_items
             if 90 <= _item_cxy(it)[1] <= 190 and 300 <= _item_cxy(it)[0] <= 780]
    parts.sort(key=lambda it: _item_cxy(it)[0])
    return "".join((it.get("text") or "").strip() for it in parts)


def _find_target(elements, match_text, match_sender):
    """按 content 包含 match_text、sender 匹配 match_sender 过滤，取最后一条命中。"""
    nt = normalize(match_text) if match_text else ""
    ns = normalize(match_sender) if match_sender else ""
    target = None
    for e in elements:
        if e.get("type") != "message_bubble":
            continue
        if match_text:
            nc = normalize(e.get("content") or "")
            hit = (nt in nc) if nt else (match_text in (e.get("content") or ""))
            if not hit:
                continue
        if match_sender:
            fields = [e.get("sender") or "", e.get("sender_nickname") or ""]
            if not any(ns and (ns in normalize(f) or normalize(f) in ns and normalize(f))
                       for f in fields):
                continue
        target = e            # elements 按 seq 升序，覆盖即取最后一条
    return target


def _quote_preview_visible(ocr_items, target_content):
    """输入栏上方是否出现引用预览条：判定带内 OCR 文本与被引用内容有重合。"""
    tn = normalize((target_content or "").split("\n")[0])
    if len(tn) < 2:
        return False
    head = tn[:8]
    for it in ocr_items:
        cx, cy = _item_cxy(it)
        if not (_PREVIEW_BAND[0] <= cy <= _PREVIEW_BAND[1]):
            continue
        itn = normalize(it.get("text") or "")
        if not itn or itn == normalize("发送消息"):
            continue
        if head in itn or (len(itn) >= 2 and itn in tn):
            log.info("quote preview matched: %r", it.get("text"))
            return True
    return False


def _send_btn_visible(img, ocr_items):
    """发送按钮存在性校验（宽区域版）。"""
    return _find_send_btn(img, ocr_items) is not None


def _find_send_btn(img, ocr_items):
    """动态定位发送按钮中心 (cx, cy)。

    引用预览条/输入栏聚焦会把按钮顶起不同高度，固定坐标必偏
    （2026-08-08 实测：引用态下 SEND_BTN 标定点按空）。
    OCR "发送" 文字优先（区域：右下 x>850, y 1850~2280），绿色掩膜兜底。"""
    # 1) OCR "发送" 文字
    for it in ocr_items:
        cx, cy = _item_cxy(it)
        if "发送" in (it.get("text") or "") and cx > 850 and 1800 < cy < 2280:
            return int(cx), int(cy)
    # 2) 绿色掩膜最大连通域中心
    if img is not None:
        import numpy as np
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        green = ((h >= LC.GREEN_H_LO) & (h <= LC.GREEN_H_HI)
                 & (s > LC.GREEN_S_MIN) & (v > LC.GREEN_V_MIN)).astype(np.uint8)
        region = green[1800:2280, 850:1080]
        n, _labels, stats, cents = cv2.connectedComponentsWithStats(region)
        best, best_area = None, 0
        for k in range(1, n):
            area = stats[k, cv2.CC_STAT_AREA]
            if area > best_area:
                best, best_area = k, area
        if best is not None and best_area > 400:    # 按钮级面积（防噪点）
            cx = int(cents[best][0]) + 850
            cy = int(cents[best][1]) + 1800
            log.info("send btn green mask center: (%d, %d) area=%d",
                     cx, cy, best_area)
            return cx, cy
    return None


# ------------------------------------------------------------------ 预览条清理
# 预览 pill 的 × 是小图标，OCR 读不出来（2026-08-08 真机实测）。
# 多行引用时 pill 变高、× 垂直居中漂移，固定坐标/固定文字带都不稳
# （用户实测多行引用卡输入框）——× 圆圈图标本身大小形态不变，
# 用模板匹配定位（assets/icon_templates/quote_close_x.png，TM_CCOEFF_NORMED
# 实测：有预览 1.00，无预览 <=0.51，阈值 0.75 宽裕）。
_CLOSE_TPL = None
_CLOSE_TPL_TRIED = False
_CLOSE_TPL_THRESH = 0.75
_CLOSE_ROI = (620, 1850, 860, 2300)      # x0, y0, x1, y1（× 只在这一带出没：
                                         # 聚焦/未聚焦/语音模式 pill 位置不同，
                                         # 多行引用还会再漂移，纵向放全）


def _close_template():
    global _CLOSE_TPL, _CLOSE_TPL_TRIED
    if not _CLOSE_TPL_TRIED:
        _CLOSE_TPL_TRIED = True
        from ..perception.icon_templates import load_templates
        all_tpls = load_templates()
        # 文件名按最后一段拆 variant：quote_close_x.png 会归到 "quote_close"
        tpls = all_tpls.get("quote_close_x") or all_tpls.get("quote_close") or []
        if tpls:
            _CLOSE_TPL = cv2.cvtColor(tpls[0], cv2.COLOR_BGR2GRAY) \
                if tpls[0].ndim == 3 else tpls[0]
    return _CLOSE_TPL


def find_preview_close_by_tpl(img):
    """模板匹配找引用预览条的 × 中心 (cx, cy)；找不到/模板缺失返回 None。"""
    tpl = _close_template()
    if tpl is None or img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    x0, y0, x1, y1 = _CLOSE_ROI
    roi = gray[y0:y1, x0:x1]
    if roi.shape[0] < tpl.shape[0] or roi.shape[1] < tpl.shape[1]:
        return None
    res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(res)
    if mx < _CLOSE_TPL_THRESH:
        return None
    return (ml[0] + x0 + tpl.shape[1] // 2,
            ml[1] + y0 + tpl.shape[0] // 2)


# pill 文字带（兜底路径用）：预览条无论聚焦/未聚焦都贴输入栏地带，
# 正常聊天消息不会出现在这个高度
_PREVIEW_PILL_BAND = (1950, 2160)
_PREVIEW_CLOSE_X = 728
_PREVIEW_IGNORE = ("ADB Keyboard", "按住说话", "发送")


def find_quote_preview_close(ocr_items):
    """引用预览条关闭按钮中心 (cx, cy, pill_text)；无预览返回 None。

    兜底路径（模板缺失时用）：检测 pill 文字。pill_text 用于点完后复验
    （同文消失才算点掉，防止对无关 OCR 噪声补点误触输入框）。"""
    for it in ocr_items:
        cx, cy = _item_cxy(it)
        if not (_PREVIEW_PILL_BAND[0] <= cy <= _PREVIEW_PILL_BAND[1]):
            continue
        raw = (it.get("text") or "").strip()
        if not raw or any(k in raw for k in _PREVIEW_IGNORE):
            continue
        return _PREVIEW_CLOSE_X, int(cy), raw
    return None


def dismiss_quote_preview(dev, ocr_fn=None):
    """存在引用预览条则点 × 关掉（发送失败后的状态清理）。
    模板匹配主路径（多行 pill 也稳）；模板缺失退 pill 文字法。
    点完复验，最多补点一次。返回是否清除。"""
    ocr_fn = ocr_fn or _default_ocr
    use_tpl = _close_template() is not None
    pill = None
    for attempt in range(2):
        try:
            img = dev.capture_bytes()
        except Exception:
            log.exception("dismiss_quote_preview capture failed")
            return False
        if use_tpl:
            pos = find_preview_close_by_tpl(img)
            if pos is None:
                return attempt > 0 or pill is None
            _dev_tap(dev, pos[0], pos[1])
            _dev_wait(dev, time.sleep, 300, 700)
            log.info("quote preview × tapped (tpl) @%s attempt %d", pos, attempt)
            continue
        # 兜底：pill 文字法
        try:
            items = ocr_fn(img)
        except Exception:
            log.exception("dismiss_quote_preview ocr failed")
            return False
        found = find_quote_preview_close(items)
        if found is None:
            return pill is not None or attempt > 0
        if pill is not None and normalize(found[2]) != normalize(pill):
            return True                 # 原 pill 已消失，残留的是别的噪声
        pill = found[2]
        _dev_tap(dev, found[0] + attempt * 30, found[1])
        _dev_wait(dev, time.sleep, 300, 700)
        log.info("quote preview close tapped @(%d,%d) (attempt %d)",
                 found[0], found[1], attempt)
    # 最后确认一次
    try:
        img = dev.capture_bytes()
        if use_tpl:
            return find_preview_close_by_tpl(img) is None
        return find_quote_preview_close(ocr_fn(img)) is None
    except Exception:
        return False


def _sent_visible(ocr_items, reply_text):
    """轻验证：内容区出现回复文本（不判失败，仅标记——防重复发送，同
    wechat_tools._send_once 先例）。"""
    rn = normalize(reply_text)
    if not rn:
        return False
    head = rn[:8]
    for it in ocr_items:
        if _item_cxy(it)[1] >= LC.INPUT_BAR_Y0:
            continue
        itn = normalize(it.get("text") or "")
        if head in itn or (itn and itn in rn):
            return True
    return False


# ------------------------------------------------------------------ 引用回复流程
def quote_reply(dev, match_text=None, match_sender=None, reply_text=None,
                ocr_fn=None, sleep_fn=None):
    """引用某条消息回复：当前聊天屏找目标 -> 长按 -> 点"引用" -> 输入 -> 发送。

    返回 dict：成功 {"ok": True, "step": "sent", "verified": bool, "target": ...,
    "quote_tap": (x, y)}；失败 {"ok": False, "step": <失败步骤>, "error": <原因>}，
    失败步骤之前已发生的动作不回滚（菜单未点开则无任何触屏动作）。
    """
    def fail(step, error):
        log.warning("quote_reply fail @%s: %s", step, error)
        return {"ok": False, "step": step, "error": error}

    if not reply_text or not reply_text.strip():
        return fail("args", "reply_text 不能为空")
    if not (match_text and match_text.strip()) \
            and not (match_sender and match_sender.strip()):
        return fail("args", "match_text / match_sender 至少给一个")
    ocr_fn = ocr_fn or _default_ocr
    sleep_fn = sleep_fn or time.sleep

    # 1. 找目标消息
    img = dev.capture_bytes()
    items = ocr_fn(img)
    _title, elements, _input_area, _actions, _extra = \
        parse_chat(img, items, _title_from_items(items))
    target = _find_target(elements, match_text, match_sender)
    if target is None:
        return fail("find_target",
                    f"当前屏未找到目标消息 match_text={match_text!r} "
                    f"match_sender={match_sender!r}")
    pos = target["position"]
    rect = Rect(pos["x"], pos["y"], pos["w"], pos["h"])
    log.info("quote_reply target: sender=%r content=%r rect=%r",
             target["sender"], target["content"][:40], rect)

    # 2. 长按气泡中心（500~900ms 随机，超过系统长按阈值 ~500ms）
    _dev_long_press(dev, rect)
    _dev_wait(dev, sleep_fn, 700, 1200)

    # 3. 验证菜单展开
    img2 = dev.capture_bytes()
    menu = detect_longpress_menu(img2, ocr_fn(img2))
    if menu is None:
        return fail("detect_menu", "长按后未检测到菜单展开（长按未生效？）")
    if "引用" not in menu:
        return fail("detect_menu", "菜单中无“引用”选项（该消息类型不支持引用）")
    qcx, qcy = menu["引用"]["center"]

    # 4. 点“引用”（点文字中心）
    _dev_tap(dev, qcx, qcy)
    _dev_wait(dev, sleep_fn, 600, 1000)

    # 5. 验证输入栏出现引用预览条
    img3 = dev.capture_bytes()
    if not _quote_preview_visible(ocr_fn(img3), target["content"]):
        return fail("quote_preview", "输入栏未出现引用预览条（点引用未生效）")

    # 6. 输入回复文本（现有输入链：ADBKeyBoard 分块广播，封装在 dev.input_text）
    dev.input_text(reply_text)
    _dev_wait(dev, sleep_fn, 400, 800)

    # 7. 验证发送按钮出现后点发送（动态定位：引用预览条会把按钮顶高）
    img4 = dev.capture_bytes()
    btn = _find_send_btn(img4, ocr_fn(img4))
    if btn is None:
        # 清理残留预览条：不点掉会盖住输入框点按区、下一条普通发送变引用
        dismiss_quote_preview(dev, ocr_fn)
        return fail("send_button", "输入后发送按钮未出现（文本未上屏？）")
    _dev_tap(dev, *btn)
    _dev_wait(dev, sleep_fn, 600, 1200)

    # 8. 轻验证（不判失败：发送按钮已点下，判失败会导致上层重发刷屏）
    verified = _sent_visible(ocr_fn(dev.capture_bytes()), reply_text)
    return {"ok": True, "step": "sent", "verified": verified,
            "target": {"sender": target["sender"],
                       "content": target["content"]},
            "quote_tap": (qcx, qcy)}
