# -*- coding: utf-8 -*-
"""media_pass.py — 采集完成后的多媒体独立处置 pass（2026-08-27）。

设计依据 docs/INTERACTION_LAYER.md §9：union 缝合采集中途 tap 媒体会打断
连续缝合/配准，媒体处置必须在采集完成后走独立 pass。

流程：
  ① 查该会话 media_status='' 的媒体条目（按 seq 降序 = 物理位置从旧
     到新：采集逐屏向更旧滚、逐屏 append，seq 越小物理越新；pass 从
     书签旧侧向最新滚正好顺路经过）；
  ② 每条目标用【存档裁图对当前屏 matchTemplate】定位——裁图像素与屏幕
     内容本就来⾃同一渲染，滑到哪屏匹配到哪屏，不做 union→device 的
     线性坐标映射（裁图高于屏时取头部子条做模板，长消息也能锚）；
  ③ 命中 → MediaTask → MediaHandler.handle()（自带 _verify_in_chat 前置
     校验 + _return_to_chat 兜底）→ message_log.update_media 按 id 写回：
       - link        → content = "[链接] <url>"（URL 进日志）
       - image/video → media_path = pull 到电脑的本地路径（视觉理解在 proxy）
       - sticker     → media_path = 裁块路径
       - chat_record → content = 解析文本（JSON）
       - file        → media_path = 本地路径
       - red_packet  → 不点击，直接标 done
     成功 media_status='done'，失败 'failed'（不再自动重试，网关可查）；
  ④ 单条定位超过 MAX_SCREENS_PER_ITEM 屏未命中 → 标 failed 跳下一个；
     预算（条数/总超时）到即停，剩下的下轮 journey 接着做。
"""

import json
import logging
import os
import time

import cv2

from . import realtime_scan as RS

log = logging.getLogger("interaction.media_pass")

PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", ".."))
WORKSPACE = os.path.join(PROJECT_ROOT, "workspace")

# 待处置的媒体 content_type（红包在内：标 done 但不点击）。
# multimedia = slice_chat 粗类（cutline 分段失败回退路径/历史遗留条目）
MEDIA_TYPES = ("image", "sticker", "link", "card", "chat_record",
               "file", "media", "video", "red_packet", "multimedia")

MATCH_THRESHOLD = 0.85   # 裁图对屏 matchTemplate 置信度（静态内容）
ANIMATED_FLOOR = 0.50    # 动图条带头部置信徒底（动画帧不同，实测真位置 ~0.65）
HEAD_H = 240             # 匹配用条带头部高度：窗口=内容区高(1820)-HEAD_H
                         # ≈1580 > 最大步长1450，保证总有一屏完整露出
                         # （360px 全带窗口只有 1460，一步跨过就永远错过——
                         #  2026-08-27 seq418 实测教训）
TEMPLATE_MAX_H = 400     # 模板取裁图头部高度上限（长消息锚头部即可）
TEMPLATE_MIN_H = 40      # 太矮的条带匹配不可靠
MAX_SCREENS_PER_ITEM = 8  # 单条定位最多滚几屏（找不到标 failed）


# ------------------------------------------------------------------ 工具
def _strip_avatar(template):
    """条带内的头像子块（静态锚）。动图表情包的人物/装饰逐帧变化，
    整带匹配永远到不了阈值（2026-08-27 猫猫群 seq418 实测：同一张动图
    表情包同屏两个实例渲染不同帧，conf 只有 0.65），但头像不变。"""
    from ..ports.android.perception.chat_slicer import (
        _build_masks, _detect_avatars, _merge_avatars)
    from ..ports.android.perception.img_utils import estimate_bg
    gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(template, cv2.COLOR_BGR2HSV)
    H = template.shape[0]
    try:
        bg = estimate_bg(gray, 0, H)
        _, _, nonbg = _build_masks(template, gray, hsv, bg, 0, H)
        avs = _merge_avatars(_detect_avatars(gray, hsv, nonbg, 0, H))
    except Exception:  # noqa: BLE001
        return None
    if not avs:
        return None
    a = max(avs, key=lambda a: a["w"] * a["h"])
    if a["w"] < 60 or a["h"] < 60:
        return None
    return a


def _local_maxima(res, thr):
    """匹配图里 ≥thr 的局部极大点 [(x, y, score)]（同一头像多条消息会多峰）。"""
    import numpy as np
    mask = (res >= thr).astype(np.uint8)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        sub = res[y:y + h, x:x + w]
        _, mx, _, ml = cv2.minMaxLoc(sub)
        out.append((x + ml[0], y + ml[1], float(mx)))
    return out
def _crop_abs(crop_path: str) -> str:
    if not crop_path:
        return ""
    return crop_path if os.path.isabs(crop_path) \
        else os.path.join(WORKSPACE, crop_path)


def _load_template(crop_path: str):
    """存档裁图 → 匹配模板（高于上限取头部子条）。"""
    p = _crop_abs(crop_path)
    if not p or not os.path.exists(p):
        return None
    img = cv2.imread(p)
    if img is None:
        return None
    if img.shape[0] > TEMPLATE_MAX_H:
        img = img[:TEMPLATE_MAX_H]
    return img


def _find_on_screen(screen, template, threshold=MATCH_THRESHOLD):
    """template 在 screen 上的匹配框 (x, y, w, h)；未命中返回 None。

    只用条带头 HEAD_H(240) 像素参与匹配与定位（矮目标保证跨步长总有
    一屏完整露出；返回的 bbox 也只是头部区域，点击目标由 _content_block
    在该区域内找主内容块）。两阶段：条带内有头像 → 头像锚定（静态，
    阈值 0.85）+ 头部复核（≥ANIMATED_FLOOR，容忍动图帧差异，取最优
    候选）；无头像 → 头部全局匹配（threshold）。
    """
    if template is None or screen is None:
        return None
    th, tw = template.shape[:2]
    sh, sw = screen.shape[:2]
    if th > sh or tw > sw or th < TEMPLATE_MIN_H:
        return None
    head = template[:min(HEAD_H, th)]
    hh = head.shape[0]
    sg = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    hg = cv2.cvtColor(head, cv2.COLOR_BGR2GRAY)
    full_res = cv2.matchTemplate(sg, hg, cv2.TM_CCOEFF_NORMED)

    av = _strip_avatar(head)
    if av is not None:
        at = hg[av["y"]:av["y"] + av["h"], av["x"]:av["x"] + av["w"]]
        ares = cv2.matchTemplate(sg, at, cv2.TM_CCOEFF_NORMED)
        best = None
        for cx, cy, _score in _local_maxima(ares, threshold):
            top = cy - av["y"]
            left = cx - av["x"]
            if top < 0 or top + hh > sh or left < 0 or left + tw > sw:
                continue
            conf = float(full_res[top, left])
            if conf >= ANIMATED_FLOOR and (best is None or conf > best[0]):
                best = (conf, (int(left), int(top), int(tw), int(hh)))
        return best[1] if best else None

    _, maxv, _, maxloc = cv2.minMaxLoc(full_res)
    if maxv < threshold:
        return None
    return (int(maxloc[0]), int(maxloc[1]), int(tw), int(hh))


def _content_block(screen, bbox):
    """定位条带内的主内容块包围框（排除头像列）。

    条带是整行宽（1080），其几何中心对左对齐小图/表情包会落在灰底上
    （2026-08-27 猫猫群 seq417 实测：点灰底不开页，signature=unknown）。
    取条带内最大非背景连通块作为 MediaTask.bbox，点击必中气泡本体。
    """
    from ..ports.android.perception.media_classifier import _nonbg_comps
    x, y, w, h = bbox
    strip = screen[y:y + h, x:x + w]
    if strip.size == 0:
        return bbox
    try:
        comps, _bg = _nonbg_comps(strip)
    except Exception:  # noqa: BLE001
        return bbox
    comps = [c for c in comps
             if not (c["box"][0] < 150 and c["box"][2] < 130)]  # 排除头像列
    if not comps:
        return bbox
    bx, by, bw, bh = max(comps, key=lambda c: c["area"])["box"]
    return (x + bx, y + by, bw, bh)


def _locate(dev, template, max_screens, sleep_fn):
    """逐屏向最新方向滚，模板匹配定位目标消息。返回 (bbox, swipes)。

    每个位置双截屏（立即 + 等 0.5s 再截）：fling 后的糊帧曾把
    conf=1.0 的真位置漏掉（2026-08-27 猫猫群 seq417 实测：单截屏错过
    目标屏后，目标沉入输入栏裁切区 conf 只剩 0.4，8 屏白滚）。
    屏幕不再变化（已滚到底）连续 2 次 → 向上回一屏做最后尝试：
    目标若是最新一条，其条带下半被输入栏遮住永远匹配不上，
    回一屏让它浮出输入栏（同次实测 seq418）。
    """
    import numpy as np
    prev_gray = None
    stable = 0
    swipes = 0
    floated = False
    for _ in range(max_screens):
        for wait in (0.0, 0.5):          # 双截屏防糊帧
            if wait:
                sleep_fn(wait)
            screen = dev.capture_bytes()
            bbox = _find_on_screen(screen, template)
            if bbox is not None:
                return bbox, swipes
        # 到底检测：屏面几乎不变 → 已在最新位置
        g = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        if prev_gray is not None and prev_gray.shape == g.shape:
            diff = float(np.abs(g.astype(int) - prev_gray.astype(int)).mean())
            stable = stable + 1 if diff < 2.0 else 0
            if stable >= 2:
                if floated:
                    return None, swipes
                floated = True             # 回一屏让最新条浮出输入栏
                RS.do_swipe(dev, "earlier")
                swipes += 1
                sleep_fn(0.8)
                stable = 0
                prev_gray = None
                continue
        prev_gray = g
        RS.do_swipe(dev, "newer")
        swipes += 1
        sleep_fn(0.8)
    return None, swipes


def _pending_media(conn, session_id: int, max_items: int,
                   since_ts: float = 0):
    """待处置媒体条目（seq 降序 = 物理位置从旧到新）。

    采集是逐屏向更旧滚、逐屏 append 的：同一轮采集中 seq 越小物理上越新。
    pass 从书签（旧侧）出发向最新滚，必须按 seq 降序处理才顺路
    （2026-08-27 猫猫群 seq418 实测：按 seq 升序先处理新的，
    旧目标落在身后永远找不到）。
    since_ts>0 时只取 ts_captured >= since_ts 的条目——只处理本轮采集
    新入库的（屏幕就在这段范围内）；更老的条目在书签旧侧，
    滚 newer 永远找不到，白滚还会误标 failed。
    """
    q = ",".join("?" * len(MEDIA_TYPES))
    return conn.execute(
        f"SELECT id, seq, content_type, content, crop_path FROM messages "
        f"WHERE session_id=? AND media_status='' AND content_type IN ({q}) "
        f"AND ts_captured >= ? "
        f"ORDER BY seq DESC LIMIT ?",
        (session_id, *MEDIA_TYPES, since_ts, max_items)).fetchall()


def _msg_type_of(content_type: str):
    """DB content_type → MediaTask.msg_type（页面签名会再细分，粗映射即可）。"""
    if content_type in ("image", "sticker", "media", "video", "multimedia"):
        return "media"
    if content_type in ("link", "card", "chat_record", "file"):
        return "card"
    if content_type == "red_packet":
        return "red_packet"
    return None


def _apply_result(conn, msg_id: int, result):
    """MediaResult → 日志写回（按 id 精确更新）。"""
    from ..msglog import message_log
    if not result.success:
        message_log.update_media(conn, msg_id, media_status="failed")
        return
    mt = result.msg_type
    if mt in ("image", "video", "sticker"):
        # content = 主产物本地路径；占位符 content 不动（视觉理解在 proxy 侧）
        placeholder = {"image": "[图片]", "video": "[视频]",
                       "sticker": "[表情包]"}[mt]
        message_log.update_media(conn, msg_id, content=placeholder,
                                 media_path=result.content or "",
                                 media_status="done", content_type=mt)
    elif mt == "file":
        name = os.path.basename(result.content or "")
        message_log.update_media(conn, msg_id,
                                 content=f"[文件] {name}".strip(),
                                 media_path=result.content or "",
                                 media_status="done", content_type="file")
    elif mt == "link":
        url = result.content if isinstance(result.content, str) else ""
        if url.startswith("http"):
            message_log.update_media(conn, msg_id, content=f"[链接] {url}",
                                     media_status="done", content_type="link")
        else:
            message_log.update_media(conn, msg_id, media_status="failed")
    elif mt == "chat_record":
        text = result.content if isinstance(result.content, str) else \
            json.dumps(result.content, ensure_ascii=False)
        message_log.update_media(conn, msg_id, content=text,
                                 media_status="done",
                                 content_type="chat_record")
    else:  # red_packet / card 等：只标状态
        message_log.update_media(conn, msg_id, media_status="done")


# ------------------------------------------------------------------ 主流程
def run_media_pass(dev, conn, session: str, max_items: int = 5,
                   timeout_s: int = 180, since_ts: float = 0,
                   handler_cls=None, sleep_fn=time.sleep) -> dict:
    """采集后的媒体独立处置。返回 {handled, failed, skipped} 计数。

    调用前提：当前停留在 session 的聊天页（采集刚结束，屏幕在旧侧）。
    since_ts：只处置 ts_captured 不早于此的条目（本轮采集入库的；
    默认 0 = 不限，供脚本/补采场景）。handler_cls 可注入假实现（测试用）。
    """
    from ..msglog import message_log

    stats = {"handled": 0, "failed": 0, "skipped": 0}
    session_id = message_log.get_or_create_session(conn, session, True)
    rows = _pending_media(conn, session_id, max_items, since_ts=since_ts)
    if not rows:
        return stats

    if handler_cls is None:
        from ..ports.android.perception.media_handler import MediaHandler
        handler_cls = MediaHandler
    handler = handler_cls(dev)
    deadline = time.time() + timeout_s
    scrolled_any = False

    for row in rows:
        if time.time() >= deadline:
            stats["skipped"] += 1
            continue
        msg_id = row["id"]
        ctype = row["content_type"]

        # 红包：不点击（用户定的，避免资金/社交风险），直接标 done
        if ctype == "red_packet":
            message_log.update_media(conn, msg_id, media_status="done")
            stats["handled"] += 1
            continue

        msg_type = _msg_type_of(ctype)
        template = _load_template(row["crop_path"])
        if msg_type is None or template is None:
            log.warning("[%s] media #%s 无模板/类型(%s)，标 failed",
                        session, msg_id, ctype)
            message_log.update_media(conn, msg_id, media_status="failed")
            stats["failed"] += 1
            continue

        # 逐屏向最新方向滚，模板匹配定位目标消息（糊帧双截屏 + 到底提前停）
        bbox, swipes = _locate(dev, template, MAX_SCREENS_PER_ITEM, sleep_fn)
        scrolled_any = scrolled_any or swipes > 0

        if bbox is None:
            log.warning("[%s] media #%s (%s) 定位失败，标 failed",
                        session, msg_id, ctype)
            message_log.update_media(conn, msg_id, media_status="failed")
            stats["failed"] += 1
            continue

        from ..ports.android.perception.media_handler import MediaTask
        # bbox 细化为条带内主内容块（整行条带的几何中心可能落在灰底上，
        # 点灰底不开页——seq417 实测教训）
        screen_now = dev.capture_bytes()
        tap_bbox = _content_block(screen_now, bbox) \
            if screen_now is not None else bbox
        task = MediaTask(msg_id=str(msg_id), msg_type=msg_type,
                         bbox=tap_bbox, screen_path="", group_name=session)
        try:
            result = handler.handle(task)
        except Exception:  # noqa: BLE001
            log.exception("[%s] media #%s 处置异常", session, msg_id)
            message_log.update_media(conn, msg_id, media_status="failed")
            stats["failed"] += 1
            continue
        _apply_result(conn, msg_id, result)
        if result.success:
            log.info("[%s] media #%s (%s→%s) 处置完成",
                     session, msg_id, ctype, result.msg_type)
            stats["handled"] += 1
        else:
            log.warning("[%s] media #%s (%s) 处置失败: %s",
                        session, msg_id, ctype, result.error)
            stats["failed"] += 1

    # 滚回最新位置：journey 的收尾 sync 以「当前屏=最新屏」存下次书签，
    # 停在中段会污染书签（下次采集起点错误 / 新消息漏采）。
    if scrolled_any:
        try:
            RS.scroll_to_latest(dev)
            sleep_fn(0.8)
        except Exception:  # noqa: BLE001
            log.exception("[%s] media pass 滚回最新失败", session)

    log.info("[%s] media pass 结束: %s", session, stats)
    return stats
