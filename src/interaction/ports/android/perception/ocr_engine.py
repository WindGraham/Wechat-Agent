#!/usr/bin/env python3
"""ocr_engine.py - v2 OCR 引擎：rapidocr 3.9.2（PP-OCRv6 small，官方 ONNX）GPU 单例
+ 区域重识别 + 小字增强。

2026-08-04 引擎升级：rapidocr_onnxruntime（PP-OCRv3）-> rapidocr 3.9.2（PP-OCRv6）。
实测全图 GPU：v6 稳态 ~245ms vs v3 ~690ms（y0_bottom），中文精度更好。
- CUDA：params {"EngineConfig.onnxruntime.use_cuda": True}
- 关闭方向分类（"Global.use_cls": False，省 ~40ms）：微信文字都是正向的
- 两库 API 差异：v6 是 RapidOCR(params=dict) + 返回 RapidOCROutput
  (.boxes/.txts/.scores)；只走识别器是 text_rec(TextRecInput(img=bgr))，
  **不接受单通道灰度图**（assert shape[2]），调用前必须转 BGR。

对外接口与 v1/v2 旧版一致：get_ocr/run_ocr/recognize_line/ocr_region/
fill_missing_lines/enhance_small_text/ocr_badge_digit。
"""

import cv2
import numpy as np

import onnxruntime as _ort
_ort.set_default_logger_severity(3)  # suppress "No registered plugin EP device" noise on CUDA 13

from . import bubble_model

# ---------------------------------------------------------------- OCR 单例（模块级懒加载）
_OCR = None

_OCR_PARAMS = {
    "EngineConfig.onnxruntime.use_cuda": True,
    "Global.use_cls": False,          # 微信文字都是正向的，关方向分类省 ~40ms
    "Global.log_level": "error",
}


def get_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr import RapidOCR
        _OCR = RapidOCR(params=_OCR_PARAMS)
    return _OCR


def run_ocr(img_or_path):
    """全图（或整区域）OCR：检测+识别。
    返回 [{'box':(x0,y0,x1,y1), 'cx','cy','h','text','conf'}, ...]"""
    res = get_ocr()(img_or_path)
    items = []
    if res is None or res.boxes is None:
        return items
    for pts, text, conf in zip(res.boxes, res.txts, res.scores):
        pts = np.asarray(pts, dtype=np.float32)
        x0, y0 = pts[:, 0].min(), pts[:, 1].min()
        x1, y1 = pts[:, 0].max(), pts[:, 1].max()
        items.append({
            "box": (float(x0), float(y0), float(x1), float(y1)),
            "cx": float((x0 + x1) / 2), "cy": float((y0 + y1) / 2),
            "h": float(y1 - y0),
            "text": text.strip(), "conf": float(conf),
        })
    return items


# ---------------------------------------------------------------- 分块并发 OCR
# 全图 det 对 ~26px 灰色小字（预览行/昵称）漏检多：1080x2340 被压到检测器
# 输入尺寸后小字糊掉。按规则裁成横向条带（内容竖排，天然对齐），条带间留
# overlap 防止切行，线程池并发识别，再按序号拼回并去重。
import threading
from concurrent.futures import ThreadPoolExecutor

_tls = threading.local()
# cuDNN 后端对并发推理不稳定（偶发 CUDNN_BACKEND_API_FAILED）：
# 推理调用串行化（全局锁），切图/坐标映射/去重仍并发。
_INFER_LOCK = threading.Lock()

# 持久线程池（2026-08-07 修复）：原先每次 run_ocr_tiled 新建 ThreadPoolExecutor，
# 新线程触发 _tls.ocr 重新初始化——每次分块 OCR 加载 3 份模型，导致 RSS 涨到
# 11GB+、每次解析多耗 ~1s。持久池让线程常驻，thread-local OCR 实例复用。
_POOL = None
_POOL_LOCK = threading.Lock()


def _get_pool(max_workers):
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=max_workers,
                                       thread_name_prefix="ocr_tile")
        return _POOL


def _thread_ocr():
    """每线程一个 RapidOCR 实例（规避 session 线程安全问题）。"""
    if getattr(_tls, "ocr", None) is None:
        from rapidocr import RapidOCR
        _tls.ocr = RapidOCR(params=_OCR_PARAMS)
    return _tls.ocr


def _ocr_tile(args):
    """识别一个条带，返回全局坐标 items。args=(img, x0, y0, x1, y1, idx)"""
    img, x0, y0, x1, y1, _idx = args
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return []
    with _INFER_LOCK:
        res = _thread_ocr()(crop)
    items = []
    if res is None or res.boxes is None:
        return items
    for pts, text, conf in zip(res.boxes, res.txts, res.scores):
        pts = np.asarray(pts, dtype=np.float32)
        gx0, gy0 = pts[:, 0].min() + x0, pts[:, 1].min() + y0
        gx1, gy1 = pts[:, 0].max() + x0, pts[:, 1].max() + y0
        items.append({
            "box": (float(gx0), float(gy0), float(gx1), float(gy1)),
            "cx": float((gx0 + gx1) / 2), "cy": float((gy0 + gy1) / 2),
            "h": float(gy1 - gy0),
            "text": text.strip(), "conf": float(conf),
        })
    return items


def _dedup_items(items, iou_thr=0.5):
    """按 归一化文本+IoU 去重（条带重叠区同一行文字会被两个条带各报一次）。"""
    def _norm(t):
        return "".join(t.split()).lower()

    def _iou(a, b):
        ax0, ay0, ax1, ay1 = a["box"]
        bx0, by0, bx1, by1 = b["box"]
        iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        ih = max(0.0, min(ay1, by1) - max(ay0, by0))
        inter = iw * ih
        if inter <= 0:
            return 0.0
        ua = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
        return inter / ua if ua else 0.0

    items = sorted(items, key=lambda it: -it["conf"])
    kept = []
    for it in items:
        dup = False
        for k in kept:
            if _norm(it["text"]) == _norm(k["text"]) and _iou(it, k) > iou_thr:
                dup = True
                break
        if not dup:
            kept.append(it)
    kept.sort(key=lambda it: (it["box"][1], it["box"][0]))
    return kept


def run_ocr_tiled(img_or_path, rows=3, cols=1, overlap=90, max_workers=3):
    """分块并发 OCR：横向 rows 条带 × cols 列，overlap 防切行。

    返回格式与 run_ocr 一致。rows=3/overlap=90 实测对灰色小字检出明显更好，
    RTX 5060 Ti 上 3 线程并发耗时与整图单遍相当（~200ms）。"""
    if isinstance(img_or_path, str):
        img = cv2.imread(img_or_path)
    else:
        img = img_or_path
    if img is None:
        return []
    h, w = img.shape[:2]
    tiles = []
    band_h = h // rows
    col_w = w // cols
    idx = 0
    for r in range(rows):
        for c in range(cols):
            y0 = max(0, r * band_h - (overlap if r else 0))
            y1 = min(h, (r + 1) * band_h + (overlap if r < rows - 1 else 0))
            x0 = max(0, c * col_w - (overlap if c else 0))
            x1 = min(w, (c + 1) * col_w + (overlap if c < cols - 1 else 0))
            tiles.append((img, x0, y0, x1, y1, idx))
            idx += 1
    if len(tiles) <= 1:
        return run_ocr(img)
    results = list(_get_pool(max_workers).map(_ocr_tile, tiles))
    merged = [it for part in results for it in part]
    return _dedup_items(merged)


def recognize_line(crop, min_conf=0.35):
    """只走识别器（框已知跳检测），毫秒级。返回 (text, conf)。
    crop 可为 BGR 或单通道灰度（v6 识别器不吃灰度，内部自动转 BGR）。"""
    from rapidocr.ch_ppocr_rec.typings import TextRecInput
    if crop.ndim == 2:
        crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
    res = get_ocr().text_rec(TextRecInput(img=crop))
    if res.txts and res.scores[0] > min_conf:
        return res.txts[0].strip(), float(res.scores[0])
    return "", 0.0


def ocr_region(img, rect, pad=6, scale=1.5):
    """局部区域裁剪放大重新 OCR（用于全图 OCR 漏检的小气泡）。
    自 v1 _ocr_region 平移。"""
    x, y, w, h = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    x0, y0 = max(0, x - pad), max(0, y - pad)
    crop = img[y0:y + h + pad, x0:x + w + pad]
    if crop.size == 0:
        return ""
    if scale != 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_CUBIC)
    items = run_ocr(crop)
    if not items:
        # 检测器对单字符（如 "?"）无效：白字阈值 -> 紧裁剪 -> 反色 -> 只走识别器
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        _, white = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
        ys, xs = np.nonzero(white)
        if len(ys) == 0:
            return ""
        tight = white[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        inv = cv2.bitwise_not(tight)
        inv = cv2.copyMakeBorder(inv, 12, 12, 16, 16, cv2.BORDER_CONSTANT, value=255)
        inv = cv2.resize(inv, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        text, _ = recognize_line(inv)      # 灰度图，recognize_line 内部转 BGR
        return text
    items.sort(key=lambda it: (it["box"][1], it["box"][0]))
    return "\n".join(it["text"] for it in items)


def fill_missing_lines(img, rect, text, dark_text=False, max_bands=14):
    """气泡高度暗示的文本行数 > OCR 已得行数时，按行投影找到漏检行，
    裁剪后只走识别器补读（检测器对单字行如 "好" 无效）。
    dark_text=True 用于自己发的绿气泡（黑字），灰气泡是白字。
    自 v1 _fill_missing_lines 平移，行数模型换 v2 bubble_model。
    max_bands: 补读行带上限（防爆行）；跨屏超长泡（h>600）不补读，
    行数对齐由回填层拼接负责。"""
    x, y, w, h = (int(v) for v in rect[:4])
    if h > 600:
        return text
    have = text.count("\n") + 1 if text.strip() else 0
    expected = bubble_model.implied_lines(h)
    if have >= expected:
        return text
    crop = img[y:y + h, x:x + w]
    if crop.size == 0:
        return text
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # 去掉边缘：气泡边可能有暗色描边/阴影，会污染行投影
    inner = gray[6:-6, 24:-24] if h > 40 and w > 80 else gray
    fg = (inner < 90) if dark_text else (inner > 150)
    rows = fg.astype(np.uint8).sum(axis=1)
    bands, start = [], None
    for i, c in enumerate(rows):
        if c > 6 and start is None:
            start = i
        elif c <= 6 and start is not None:
            if i - start > 24:
                bands.append((start + 6, i + 6))
            start = None
    if start is not None and inner.shape[0] - start > 24:
        bands.append((start + 6, inner.shape[0] + 6))
    if len(bands) <= have:
        return text
    lines = []
    for y0, y1 in bands[:max_bands]:
        band = gray[max(0, y0 - 6):y1 + 6, 24:-24 if w > 80 else w]
        mask = ((band < 90) if dark_text else (band > 150)).astype(np.uint8)
        ys, xs = np.nonzero(mask)
        if len(ys) < 10:
            continue
        tight = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1] * 255
        inv = cv2.bitwise_not(tight)  # 黑字白底
        inv = cv2.copyMakeBorder(inv, 10, 10, 16, 16, cv2.BORDER_CONSTANT, value=255)
        big = cv2.resize(inv, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        line, _ = recognize_line(big, min_conf=0.3)
        lines.append(line)
    filled = "\n".join(l for l in lines if l)
    return filled if len(filled) > len(text) else text


def enhance_small_text(img, ocr_items, max_items=12):
    """小号灰字（昵称/时间/预览等）裁剪放大 2x 重识别，修正低置信度误字。
    框已知所以只走识别器（跳检测），每项约几 ms；按置信度升序最多处理 max_items 个。
    性能收口（2026-08-04）：conf 阈值 0.92->0.88、上限 20->12，只重识别最可疑的
    一小撮（0.88 以上的小字误字率已很低），18 样本回归验证无影响。
    自 v1 平移。"""
    cands = [it for it in ocr_items
             if 14 <= it["h"] <= 46 and it["conf"] < 0.88 and it["text"]]
    cands.sort(key=lambda it: it["conf"])
    for it in cands[:max_items]:
        x0, y0, x1, y1 = (int(v) for v in it["box"])
        pad = 6
        crop = img[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
        if crop.size == 0:
            continue
        big = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        text, conf = recognize_line(big)
        if conf > it["conf"] and text:
            it["text"] = text
            it["conf"] = conf
    return ocr_items


def ocr_badge_digit(img, bx, by, bw, bh):
    """角标数字兜底：红圆外的像素置白（隔绝头像亮色杂点），数字反色成
    黑字白底后只走识别器（检测器对单个数字无效）。自 v1 _ocr_badge_digit 平移。"""
    pad = 4
    x0, y0 = max(0, bx - pad), max(0, by - pad)
    crop = img[y0:by + bh + pad, x0:bx + bw + pad]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red = (((h < 10) | (h > 170)) & (s > 100) & (v > 140)).astype(np.uint8)
    contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    # 填充最大红色轮廓得到整个角标圆（数字是圆内的洞），侵蚀后取内部
    filled = np.zeros_like(red)
    cv2.drawContours(filled, [max(contours, key=cv2.contourArea)], -1, 1, cv2.FILLED)
    inner = cv2.erode(filled, np.ones((7, 7), np.uint8))
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray[inner == 0] = 0
    _, white = cv2.threshold(gray, 190, 255, cv2.THRESH_BINARY)
    if np.count_nonzero(white) < 8:
        return None
    inv = cv2.bitwise_not(white)  # 黑字白底
    inv[inner == 0] = 255
    big = cv2.resize(inv, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    text, _ = recognize_line(big, min_conf=0.0)   # 灰度图，内部转 BGR
    if text:
        text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        if text.isdigit():
            return int(text)
    return None
