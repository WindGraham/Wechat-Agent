# -*- coding: utf-8 -*-
"""history_collect.py — 增量新消息采集：书签 + 两屏重叠-残缺-缝合。

设计定稿：docs/DESIGN_OVERLAP_STITCH_COLLECTION.md（唯一设计依据，实现按它）。

目标（用户定稿）：有新消息 → 进入对话 → 采集到上次进入对话的新消息之后的
新消息 → 拼接 prompt。不是「续采整段旧历史」。

核心流程（设计 §0/§5/§6）：
  ① 进会话第一屏即最新屏 N1（用户实测：不需要 scroll_to_latest），捕获为
     first_img，结束时存成【下次】书签（内容区裁图 [200, 输入栏顶]）。
  ② 向更早逐屏翻，每屏与上一屏按实测位移 dy 用 stitch_union 拼成 union
     （= A/B 差分缝合段：A2 在上、B1 在下，接缝内容连续不丢行）。识别 union：
       - complete（[本条头像顶, 下条头像顶] 都在识别区域内）→ 入库（fuzzy 去重）
       - top_clipped（头像不是完整正方形/昵称不全）→ 跳过，下一 union 补全
       - bottom_clipped（尾被输入栏切）→ 完整版在更早 union 已采，跳过
         （仅首屏最后一条新消息例外：无更早完整版，落库截断实例 complete=0）
  ③ 每次滑动 0.75×消息区 ≈1417px（< 一屏 → 必有重叠），于是：
       - 两屏 union 必然在某时刻【完整包含】上次书签 N0 → 匹配到 = 新消息
         采完 → 停（设计 §1 前提 0 < dy < M 保证不会漏检）；
       - ≤1 屏高的消息必被某次 union 完整包含，不漏长消息（设计 §8）。
  ④ 结束时存 N1 为下次书签。

滑动前提（设计 §1）：0 < min ≤ dy ≤ max < 消息区高。

复用：realtime_scan.do_swipe / to_entry / save_crop、
scroll_stitch.find_overlap_dy / stitch_union / CONTENT_Y0、
roster_matcher（双因子）、message_log.append_incremental + fuzzy_eq。
"""

import hashlib
import logging
import os
import time
from types import SimpleNamespace

import cv2
import numpy as np

from . import realtime_scan as RS
from .scroll_stitch import CONTENT_Y0, INPUT_BAR_Y0, find_overlap_dy, stitch_union

log = logging.getLogger("interaction.history_collect")

PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", ".."))


def _runtime_cfg(key, default):
    """读 config/runtime.json（热读，文件缺失/损坏时用默认值）。"""
    try:
        import json
        with open(os.path.join(PROJECT_ROOT, "config", "runtime.json"),
                  encoding="utf-8") as f:
            return json.load(f).get(key, default)
    except (OSError, ValueError):
        return default
ANCHOR_DIR = os.path.join(PROJECT_ROOT, "workspace", "runtime")
ANCHOR_COVER_SUM = 0.8   # union 完整包含书签的匹配置信度（设计 §10 初值，真机校准）
# 书签展示副本（网关 /workspace/ 图片路由可读；runtime/ 被路由禁止，故放 bookmarks/）
BOOKMARK_DISPLAY_ROOT = os.path.join(PROJECT_ROOT, "workspace", "bookmarks")


def _bookmark_display_dir(group):
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in group)
    return os.path.join(BOOKMARK_DISPLAY_ROOT, safe)


def _anchor_path(group):
    """群聊「最新屏」书签路径：上次会话进入时看到的最新屏（内容区裁图）。"""
    h = hashlib.sha1(group.encode("utf-8")).hexdigest()[:16]
    return os.path.join(ANCHOR_DIR, f"collect_anchor_{h}.jpg")


def _content_crop(img):
    """内容区裁图 [内容顶, CV实测输入栏顶]，剔除标题栏/置顶条/输入栏固定 UI。

    内容顶 = 置顶消息条下边界（有置顶条时），否则 CONTENT_Y0（设计 §9 #5：
    书签只存消息内容区；置顶条是固定 UI 不算文本区，2026-08-14 用户反馈）。
    """
    from ..ports.android.perception.page_detector import (
        detect_input_bar_top, detect_pinned_bar_end)
    top = detect_pinned_bar_end(img) or CONTENT_Y0
    bottom = detect_input_bar_top(img) or INPUT_BAR_Y0
    bottom = max(bottom, top + 1)
    return img[top:bottom]


def _screen_bounds(img):
    """整屏识别区域：内容区 [内容顶, 输入栏顶]（CV 实测，回退固定值）。"""
    from ..ports.android.perception.page_detector import (
        detect_input_bar_top, detect_pinned_bar_end)
    top = detect_pinned_bar_end(img) or CONTENT_Y0
    bottom = detect_input_bar_top(img) or INPUT_BAR_Y0
    return top, max(bottom, top + 1)


def _load_anchor(group):
    p = _anchor_path(group)
    if not os.path.exists(p):
        return None
    img = cv2.imread(p)
    if img is None:
        return None
    if img.shape[0] > 2200:
        # 旧格式整屏书签 → 裁内容区兼容（设计 §9 #5：书签只存内容区）
        img = _content_crop(img)
    return img


def _save_anchor(group, img):
    if img is None:
        return
    # 黑屏/糊帧保护：整图亮度过低视为熄屏或采集失败，拒绝覆盖好书签
    if img.size:
        _g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if float(_g.mean()) < 5.0:
            log.warning("书签截图疑似黑屏(亮度 %.1f)，放弃保存，保留旧书签", _g.mean())
            return
    os.makedirs(ANCHOR_DIR, exist_ok=True)
    crop, top, bottom = _content_crop_bounds(img)
    cv2.imwrite(_anchor_path(group), crop,
                [cv2.IMWRITE_JPEG_QUALITY, 95])
    # 网关展示副本：全屏 + 内容区裁切（与采集用的 runtime 书签同一次截图）
    d = _bookmark_display_dir(group)
    os.makedirs(d, exist_ok=True)
    cv2.imwrite(os.path.join(d, "full.jpg"), img,
                [cv2.IMWRITE_JPEG_QUALITY, 95])
    cv2.imwrite(os.path.join(d, "crop.jpg"), crop,
                [cv2.IMWRITE_JPEG_QUALITY, 95])
    # 裁切范围元数据（网关展示页在整屏图上标绿框用）
    import json
    try:
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"full_h": int(img.shape[0]), "full_w": int(img.shape[1]),
                       "crop_top": int(top), "crop_bottom": int(bottom)}, f)
    except OSError:
        pass


def _content_crop_bounds(img):
    """返回 (内容区裁图, 内容顶, 输入栏顶)。内容顶排除置顶条（见 _content_crop）。"""
    from ..ports.android.perception.page_detector import (
        detect_input_bar_top, detect_pinned_bar_end)
    top = detect_pinned_bar_end(img) or CONTENT_Y0
    bottom = detect_input_bar_top(img) or INPUT_BAR_Y0
    bottom = max(bottom, top + 1)
    return img[top:bottom], top, bottom


def _anchor_in_union(anchor, union_img):
    """书签（内容区裁图）是否被 union【完整包含】：matchTemplate 全图检索。

    前提 dy < 消息区高 → 相邻两屏必有重叠 → 书签（1 屏高）从「完全在上一屏」
    到「完全在下一屏」必然经历跨两屏时刻，那一刻 union 完整包含书签 → conf≈1
    （设计 §2）。返回 0~1 匹配置信度。
    """
    ag = cv2.cvtColor(anchor, cv2.COLOR_BGR2GRAY)
    sg = cv2.cvtColor(union_img, cv2.COLOR_BGR2GRAY)
    if ag.shape[0] > sg.shape[0] or ag.shape[1] > sg.shape[1]:
        return 0.0
    res = cv2.matchTemplate(sg, ag, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, _ = cv2.minMaxLoc(res)
    return float(maxv)


def _key_of(sender, content_type, content):
    """精确去重键：time_divider 的 sender 归一为空串（与 to_entry 一致）。"""
    from ..msglog.message_log import normalize
    s = "" if content_type == "time_divider" else sender
    return f"{s}|{content_type}|{normalize(content)}"


def _key(e):
    dh = getattr(e, "dedup_hash", None)
    if dh is not None:
        # 媒体条目：占位符大家都一样，去重键带段裁图哈希（裁图文件名也因此唯一）
        from ..msglog.message_log import normalize
        return _key_of(e.sender, e.content_type,
                       f"{normalize(e.content)}#{dh:016x}")
    return _key_of(e.sender, e.content_type, e.content)


def _hamming(a, b):
    return bin(a ^ b).count("1")


MEDIA_DEDUP_HAMMING = 6   # 同一媒体跨 union 重渲染的容差（aHash 64bit）


def _known(e, existing, existing_keys, seen, seen_keys, media_hashes=()):
    """OCR 鲁棒去重：先精确(full content_norm)，后 fuzzy(同 content_type)。

    OCR 会把同一句读成「2600小登」/「26ee小登」这种变体，精确键失配，
    需 fuzzy_eq(ratio>=0.85) 兜底；time_divider 只走精确（避免模糊误杀时间戳）。
    媒体条目（带 dedup_hash）：占位符内容无区分度，按段裁图 aHash 的
    汉明距离判同图（<=MEDIA_DEDUP_HAMMING 视为已见）。
    """
    dh = getattr(e, "dedup_hash", None)
    if dh is not None:
        return any(_hamming(dh, h) <= MEDIA_DEDUP_HAMMING for h in media_hashes)
    from ..msglog.message_log import fuzzy_eq
    k = _key(e)
    if k in existing_keys or k in seen_keys:
        return True
    if e.content_type == "time_divider":
        return False
    content = e.content or ""
    for (es, ect, ec) in seen:
        if ect == e.content_type and fuzzy_eq(e.sender, content, es, ec):
            return True
    for (es, ect, ec) in existing:
        if ect == e.content_type and fuzzy_eq(e.sender, content, es, ec):
            return True
    return False


# 媒体段占位符（2026-08-27）：细分 content_type → 入库占位文本。
# 细分只为打标准确——真机处置时 MediaHandler 按页面签名会再判一次
# （图片/视频/表情包、链接/聊天记录/文件都自动分流）。
_MEDIA_PLACEHOLDER = {
    "image": "[图片]", "link": "[链接]", "chat_record": "[聊天记录]",
    "file": "[文件]", "red_packet": "[红包]",
}


def _phash(img):
    """平均哈希（64bit int）：媒体段去重身份（跨 union 同图同 hash）。"""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (8, 8), interpolation=cv2.INTER_AREA)
    avg = float(g.mean())
    bits = 0
    for v in g.flatten():
        bits = (bits << 1) | int(v >= avg)
    return bits


def _classify_text_suspect(rec_img, sg):
    """文本段复核 → (content_type, phash)；维持文本 → ("text", None)。

    只抓伪装成文本的卡片（链接卡/聊天记录卡/文件卡），不碰
    nonbg_block 媒体路径——那会误伤正常文本（自己绿气泡+头像、
    新消息胶囊压气泡，2026-09-02 全量回归实测）。
    """
    from ..ports.android.perception.media_classifier import (
        classify_text_suspect)
    y0, y1 = int(sg["y_top"]), int(sg["y_bottom"])
    crop = rec_img[max(0, y0):y1]
    if crop.size == 0:
        return "text", None
    try:
        label, detail = classify_text_suspect(crop, sg.get("content") or "")
    except Exception:  # noqa: BLE001
        return "text", None
    if label == "card":
        sub = (detail or {}).get("sub")
        ctype = {"chat_record": "chat_record", "file": "file"}.get(sub, "link")
        return ctype, _phash(crop)
    return "text", None


def _classify_media_seg(rec_img, sg):
    """媒体段 → (content_type, placeholder, phash)；unknown/system → (None, None, None)。

    用调色板锚定分类器在段裁图上细分；分类器认为是文本的（slice_chat
    粗判媒体的误检）按文本入库。phash 是媒体条目的去重身份。
    """
    from ..ports.android.perception.media_classifier import classify_segment
    y0, y1 = int(sg["y_top"]), int(sg["y_bottom"])
    crop = rec_img[max(0, y0):y1]
    if crop.size == 0:
        return None, None, None
    ph = _phash(crop)
    try:
        label, detail = classify_segment(crop, sg.get("content") or "")
    except Exception:  # noqa: BLE001
        label, detail = "media", {}
    if label == "card":
        sub = (detail or {}).get("sub")
        ctype = {"chat_record": "chat_record", "file": "file"}.get(sub, "link")
    elif label == "red_packet":
        ctype = "red_packet"
    elif label == "media":
        ctype = "image"
    elif label in ("text", "quote"):
        return "text", sg["content"], None
    else:
        return None, None, None
    return ctype, _MEDIA_PLACEHOLDER[ctype], ph


def collect_group_history(dev, conn, group, max_rounds=40,
                          stop_empty_rounds=2, on_new=None,
                          stop_when=None, stop_at_anchor=True,
                          use_cutlines=True, debug_dir=None,
                          reconcile=False, reconcile_max_per_round=2,
                          reconcile_max_total=12, low_conf_retries=0,
                          handle_media=True, media_max=None,
                          media_timeout_s=None, media_handler=None):
    """进群后从最新屏向更早翻，union 缝合识别，滚到上次书签即停（增量新消息）。

    dev: DeviceCtl；conn: msglog 连接；group: 群名（有花名册则双因子识别）。
    stop_empty_rounds>0：连续 N 屏入库 0 条（书签缺失/匹配失败的兜底）提前停。
    stop_when: 可选 callable(msg)->bool，命中 True 时本屏入库后停止。
    stop_at_anchor=False：忽略书签停止（整段历史深采到顶，
    供 scripts/collect_group_to_join.py 使用）。
    debug_dir：调试落盘目录（截图原图/裁切/拼接/单条信息裁切/识别结果），
    None 时不落盘。测试时期不启动自动删除，后续由用户开几小时/每天更新删除。
    reconcile=True：双因子失配消息在其仍在屏上时点头像进资料页调和
    （改名/换头像按 roster_update 四规则写回花名册；新成员动态学习入库），
    每屏最多 reconcile_max_per_round 条、每次采集最多 reconcile_max_total 条。
    handle_media=True（2026-09-01 用户定稿）：滚动中识别到本屏有完整露出的
    多媒体消息就立即点击处置（链接取 URL / 图片视频文件存电脑 / 表情包抠图），
    取完校验仍在原位置再继续滑动。交接处半显的媒体段本轮跳过不采，下一屏
    完整露出时才入库+处置。位置漂移则停止采集（已入库数据保留，下轮 journey
    从书签覆盖空洞）。media_max/media_timeout_s 为每次采集的处置预算，
    None 时读 runtime.json 的 media_handle_max_per_journey(5)/
    media_handle_timeout_s(180)；media_handler 供测试注入假实现。
    返回累计入库条数。
    """
    from ..msglog import message_log
    from ..ports.android.perception.ocr_engine import run_ocr
    from ..ports.android.perception.chat_slicer import (
        slice_chat, classify_message)
    from ..ports.android.perception.roster_matcher import RosterMatcher

    session_id = message_log.get_or_create_session(conn, group, is_group=True)
    rm = RosterMatcher(group)

    # 开采前校验：当前页必须是目标聊天页（OCR 顶部标题含群名）。
    # 2026-09-01 事故：媒体处置 _return_to_chat 过冲落到首页后，下一轮
    # sync 把首页会话列表当聊天采出 3 条假消息（联系人头像→[图片]）。
    probe0 = run_ocr(dev.capture_bytes())
    in_chat0 = any(group[:4] in i.get("text", "") and i.get("cy", 9999) < 220
                   for i in probe0)
    if not in_chat0:
        log.warning("[%s] 采集前校验：当前页标题不含群名，放弃本次采集"
                    "（防止把非聊天页当聊天采）", group)
        return 0

    # 调试落盘目录（测试时期不启动自动删除）
    if debug_dir is not None:
        import json
        os.makedirs(debug_dir, exist_ok=True)
        debug_manifest = {"group": group, "screens": []}

    # 全库已有消息（sender, content_type, content），供模糊去重；
    # 媒体条目的 frame_phash 供同图去重（占位符无区分度）
    existing = []
    media_hashes = []
    for r in conn.execute(
            "SELECT sender, content_type, content, frame_phash FROM messages "
            "WHERE session_id=?", (session_id,)).fetchall():
        existing.append((r["sender"], r["content_type"], r["content"]))
        if r["frame_phash"]:
            try:
                media_hashes.append(int(r["frame_phash"], 16))
            except (TypeError, ValueError):
                try:
                    media_hashes.append(int(r["frame_phash"]))
                except (TypeError, ValueError):
                    pass
    existing_keys = {_key_of(s, ct, c) for (s, ct, c) in existing}

    anchor = _load_anchor(group)   # 上次会话进入时的最新屏（内容区）→ 新消息起点
    seen = []                      # 本 run 已见（sender, content_type, content）
    seen_keys = set()
    total = 0
    empty_streak = 0
    cur_divider = None             # 最近时间分割线文本（跨屏传播，供 ts_hint）
    first_img = None               # 本次进入时的最新屏 → 存为下次书签
    prev_img = None
    reconciled = set()             # 本 run 已点头像调和过的昵称（防重复点）
    reconcile_done = 0             # 本 run 已调和次数（上限 reconcile_max_total）
    # 内联媒体处置状态（2026-09-01 用户定稿：滚动中识别到就处置）
    if handle_media:
        handle_media = bool(_runtime_cfg("media_handle_inline_enabled", True))
    if media_max is None:
        media_max = int(_runtime_cfg("media_handle_max_per_journey", 5))
    if media_timeout_s is None:
        media_timeout_s = int(_runtime_cfg("media_handle_timeout_s", 180))
    media_deadline = time.time() + media_timeout_s
    media_handled = 0              # 本 run 已处置条数（预算计数）
    media_drift_stop = False       # 处置后位置漂移 → 停止采集
    media_handler_inst = None      # MediaHandler 惰性单例（media_handler 参数可注入假实现）

    for rnd in range(max_rounds):
        img = dev.capture_bytes()
        if first_img is None:
            first_img = img.copy()   # 进会话第一屏 = 最新屏（下次书签）

        # ---- 位移检测 + 糊帧/懒加载处理（首屏无 prev，跳过）
        # 2026-08-14 用户定稿：重叠检测在【裁切后的消息区】上做（排除置顶条/
        # 输入栏固定 UI 污染），find_overlap_dy 已支持裁切图高度。
        dy, conf = 0.0, 0.0
        st = None
        if prev_img is not None:
            prev_c, _, _ = _content_crop_bounds(prev_img)
            cur_c, _, _ = _content_crop_bounds(img)
            dy, conf = find_overlap_dy(prev_c, cur_c)
            # conf<0.5：多半是滑动动画中截到糊帧 → 等待后重截（不重滑）
            if conf < 0.5:
                time.sleep(1.0)
                img = dev.capture_bytes()
                cur_c, _, _ = _content_crop_bounds(img)
                dy, conf = find_overlap_dy(prev_c, cur_c)
            # dy<40 且 conf 低：多半已到缓存顶（空滚/回弹），走懒加载分支；
            # dy>=40 但 conf 低才是真·内容变化/糊帧。
            if dy >= 40 and conf < 0.5:
                if low_conf_retries > 0:
                    # 深采模式：真·低置信时重滑续采而非停止。
                    # 重滑后 dy=两次滑动合计位移，仍 < 内容区高则可正常
                    # 缝合不丢消息；超过则 stitch_union 回退单屏（有缝）。
                    low_conf_retries -= 1
                    print(f"  [rnd{rnd + 1}] 重叠置信低(conf={conf:.2f})，重滑续采"
                          f"（剩 {low_conf_retries} 次）")
                    RS.do_swipe(dev, "earlier")
                    time.sleep(2.0)
                    continue
                # 停止前最后检查一次书签（可能已滚过书签位置但 conf 低）
                if stop_at_anchor and anchor is not None and prev_img is not None:
                    st_last = stitch_union(prev_img, img, dy)
                    if st_last is not None:
                        aconf = _anchor_in_union(anchor, st_last[0])
                        if aconf >= ANCHOR_COVER_SUM:
                            print(f"  [rnd{rnd + 1}] 重叠置信低(conf={conf:.2f})但命中书签(conf={aconf:.2f})，停止")
                            break
                print(f"  [rnd{rnd + 1}] 重叠置信过低(conf={conf:.2f})，停止")
                break
            # dy<40：滑到顶或懒加载卡顿 → 再滑一次确认。
            # 微信旧消息懒加载：滚到缓存尽头时滑动会"空滚"(dy≈0)，要等它把
            # 更早的消息渲染出来才能继续滚：等 3s + 再滑一次。
            if dy < 40:
                time.sleep(3.0)
                RS.do_swipe(dev, "earlier")
                time.sleep(3.0)
                img = dev.capture_bytes()
                cur_c, _, _ = _content_crop_bounds(img)
                dy2, conf2 = find_overlap_dy(prev_c, cur_c)
                if dy2 < 40 or conf2 < 0.5:
                    # 停止前最后检查一次书签
                    if stop_at_anchor and anchor is not None and prev_img is not None:
                        st_last = stitch_union(prev_img, img, dy2)
                        if st_last is not None:
                            aconf = _anchor_in_union(anchor, st_last[0])
                            if aconf >= ANCHOR_COVER_SUM:
                                print(f"  [rnd{rnd + 1}] 滑动异常(dy={dy2:.0f})但命中书签(conf={aconf:.2f})，停止")
                                break
                    print(f"  [rnd{rnd + 1}] 滑到没变化(dy={dy2:.0f})，停止")
                    break
                dy, conf = dy2, conf2
            # 两屏内容区按实测位移缝合（A2 上 + B1 下，接缝连续不丢行）
            st = stitch_union(prev_img, img, dy)

        # ---- 识别段：首屏=整屏；之后=union 缝合段
        if st is not None:
            rec_img, cy0, cy1 = st
            rec_hint = "union"
        else:
            cy0, cy1 = _screen_bounds(img)
            rec_img = img
            rec_hint = "screen"

        ocr = run_ocr(rec_img)
        res = slice_chat(rec_img, ocr, is_group=True, title=group,
                         roster_matcher=rm, content_y0=cy0, content_y1=cy1)

        # ---- 统一裁切线分段（2026-08-15 自动化接入）：优先用「头像上边缘 +
        # 时间戳边沿」分段（不依赖气泡完整性，跨接缝消息正确处理），每段带
        # 匹配度。失败（无有效段）回退 slice_chat 消息路径。----
        # 当前屏可见内容区（内联媒体处置的坐标映射基准；union 行 y_u →
        # 设备行 top_c + y_u，与 reconcile 同一映射）
        top_c = bottom_c = None
        if handle_media:
            from ..ports.android.perception.page_detector import (
                detect_input_bar_top, detect_pinned_bar_end)
            top_c = detect_pinned_bar_end(img) or CONTENT_Y0
            bottom_c = detect_input_bar_top(img) or INPUT_BAR_Y0

        def _seg_dev_y(sg):
            """段 rec_img 坐标 → 设备坐标 (y_top, y_bottom)。"""
            if st is None:
                return int(sg["y_top"]), int(sg["y_bottom"])
            return top_c + int(sg["y_top"]), top_c + int(sg["y_bottom"])

        def _seg_fully_visible(sg):
            """段完整露出在当前屏可点区域（上下留边排除半显交接段）。"""
            if top_c is None:
                return False
            yt, yb = _seg_dev_y(sg)
            return yt >= top_c + 20 and yb <= bottom_c - 20

        seg_entries = None
        segs = None
        if use_cutlines:
            from .cutline_segment import segment_cutlines
            try:
                segs = segment_cutlines(rec_img, roster_matcher=rm, title=group)
                if segs:
                    seg_entries = []
                    for sg in segs:
                        if sg["factor"] == "时间":
                            cur_divider = sg["content"] or cur_divider
                            continue
                        seg_type = sg.get("type") or "text"
                        if seg_type == "text" and not sg["content"]:
                            continue
                        # 自己的消息（右侧气泡）：cutline 分段已识别
                        # factor=="自己"，这里必须落成 sender="我"/is_mine=True，
                        # 否则群里自己发的消息无头像/昵称因子 → sender=""，
                        # 查重 fuzzy_eq 先比 sender，"我"对""永远不等，
                        # journey 重扫把自己的回复当匿名新消息重复入库
                        # （2026-09-01 猫猫群每条回复记两遍的事故）。
                        is_mine = sg["factor"] == "自己"
                        if is_mine:
                            sender = "我"
                        else:
                            sender = sg.get("avatar_cand") or sg.get("nickname") or ""
                            if not sender and sg["factor"] in ("未知",):
                                sender = "未知"
                        # 多媒体打标（2026-08-27）：非文本段用调色板锚定分类器
                        # 细分类型，入库占位符 + 裁图路径（打标即走，不点击；
                        # 真机处置由采集后的 media_pass 独立完成）。
                        ctype = "text"
                        content = sg["content"]
                        ph = None
                        if seg_type != "text":
                            ctype, content, ph = _classify_media_seg(
                                rec_img, sg)
                            if ctype is None:
                                continue   # unknown/system 段仍跳过
                            # 半显媒体段不采（2026-09-01 内联处置）：交接处
                            # 残段点不中/裁不全，下一屏完整露出时才入库+处置
                            if ctype != "text" and handle_media \
                                    and not _seg_fully_visible(sg):
                                continue
                        elif content:
                            # 文本段复核分长短两路（2026-09-02 定稿）：
                            # - ≤3 字短文本 → 全量分类（防 OCR 幻读：
                            #   表情包细节/图表被幻读成 "R"/"B"/"T"，
                            #   需要 nonbg_block 媒体检测才能揪出）；
                            # - 长文本 → 只过卡片嫌疑复核（链接卡标题是
                            #   长文本会粗判 text 漏检；nonbg_block 会
                            #   误伤正常文本：绿气泡+头像/胶囊压气泡）。
                            if len(content.strip()) <= 3:
                                ct2, _, ph2 = _classify_media_seg(rec_img, sg)
                            else:
                                ct2, ph2 = _classify_text_suspect(rec_img, sg)
                            if ct2 not in (None, "text"):
                                if handle_media and not _seg_fully_visible(sg):
                                    continue   # 半显媒体段同上不采
                                ctype = ct2
                                content = _MEDIA_PLACEHOLDER[ct2]
                                ph = ph2
                        e = SimpleNamespace(
                            sender=sender, is_mine=is_mine,
                            content=content, content_type=ctype,
                            complete=1, partial_top=False,
                            partial_bottom=False, mentions=[],
                            media_path="", ocr_conf=None,
                            kind="msg")
                        if ph is not None:
                            e.dedup_hash = ph
                            # 64bit 无符号 int 超 SQLite INTEGER 上限，存 hex
                            e.frame_phash = f"{ph:016x}"
                        e.time_hint = cur_divider
                        e.match_factor = sg["factor"]
                        e.avatar_score = sg.get("avatar_score")
                        e.nick_score = sg.get("nick_score")
                        e.ybounds = (int(sg["y_top"]), int(sg["y_bottom"]))
                        if not _known(e, existing, existing_keys, seen,
                                      seen_keys, media_hashes):
                            seen.append((e.sender, e.content_type, e.content))
                            seen_keys.add(_key(e))
                            if ph is not None:
                                media_hashes.append(ph)
                            e.crop_path = RS.save_crop(
                                rec_img, group, sg["y_top"], sg["y_bottom"], _key(e))
                            seg_entries.append(e)
            except Exception as exc:  # noqa: BLE001
                log.warning("统一裁切线分段失败，回退 slice_chat: %s", exc)
                seg_entries = None

        new_entries = []
        stop_now = False
        if seg_entries:
            new_entries = seg_entries
            print(f"  [rnd{rnd + 1}] {rec_hint} dy={dy:5.0f} conf={conf:.2f} "
                  f"裁切线分段 {len(new_entries)} 条")
        for m in ([] if seg_entries else res["messages"]):
            c = classify_message(m)
            ctype = m.get("content_type")
            if stop_when is not None and stop_when(m):
                stop_now = True
                continue
            if ctype == "time_divider":
                cur_divider = m.get("content") or cur_divider
                e = RS.to_entry(m, group)
                if not _known(e, existing, existing_keys, seen, seen_keys):
                    seen.append((e.sender, e.content_type, e.content))
                    seen_keys.add(_key(e))
                    new_entries.append(e)
                continue
            if c["state"] == "complete":
                e = RS.to_entry(m, group)
                e.time_hint = cur_divider
                e.ybounds = (int(c["y_top"]), int(c["y_bottom"]))
                # 媒体分型（与裁切线路径同一套规则，2026-09-02 定稿）：
                # slice_chat 粗类型非文本 → 全量细分；带内容文本分长短：
                # ≤3 字 → 全量复核（防 OCR 幻读 "R"/"B"/"T"）；
                # 长文本 → 卡片嫌疑复核（只抓伪装成文本的卡片）。
                c0 = e.content_type or "text"
                if c0 == "text" and e.content:
                    sg = {"y_top": c["y_top"], "y_bottom": c["y_bottom"],
                          "content": e.content}
                    if len(e.content.strip()) <= 3:
                        ct2, content2, ph2 = _classify_media_seg(rec_img, sg)
                        if ct2 is not None and ct2 != "text":
                            e.content_type = ct2
                            e.content = content2
                            if ph2 is not None:
                                e.dedup_hash = ph2
                                e.frame_phash = f"{ph2:016x}"
                    else:
                        ct2, ph2 = _classify_text_suspect(rec_img, sg)
                        if ct2 != "text":
                            e.content_type = ct2
                            e.content = _MEDIA_PLACEHOLDER[ct2]
                            if ph2 is not None:
                                e.dedup_hash = ph2
                                e.frame_phash = f"{ph2:016x}"
                elif c0 not in ("text", "quote", "time_divider"):
                    sg = {"y_top": c["y_top"], "y_bottom": c["y_bottom"],
                          "content": e.content}
                    ct2, content2, ph2 = _classify_media_seg(rec_img, sg)
                    if ct2 is None:
                        continue   # unknown/system 跳过
                    e.content_type = ct2
                    e.content = content2
                    if ph2 is not None:
                        e.dedup_hash = ph2
                        e.frame_phash = f"{ph2:016x}"
                if not _known(e, existing, existing_keys, seen, seen_keys,
                              media_hashes):
                    seen.append((e.sender, e.content_type, e.content))
                    seen_keys.add(_key(e))
                    e.crop_path = RS.save_crop(
                        rec_img, group, c["y_top"], c["y_bottom"], _key(e))
                    new_entries.append(e)
            elif c["state"] == "bottom_clipped" and prev_img is None:
                # 首屏最后一条 = 最新消息：尾部被输入栏截断时没有「更早完整版」
                # 可依赖（它是新消息），落库截断实例（complete=0）兜底不丢。
                # 次屏起的 bottom_clipped 完整版在更早 union 已采，直接跳过
                # （设计 §4：底部残缺不可在「滚更早」方向恢复）。
                e = RS.to_entry(m, group)
                e.time_hint = cur_divider
                if e.sender and e.content.strip() \
                        and e.content_type in ("text", "quote"):
                    # 底部截断段也过卡片嫌疑复核（2026-09-02：最新消息是
                    # 链接卡时被输入栏截断 → 走本分支存成 text，URL 永久丢
                    # ——截断文本与完整卡 fuzzy 比不中，完整版永远不会再采）。
                    # 截断卡不当屏点击（可能点不中/页面状态不全），标对类型
                    # 落 media_status=''，留给 media_pass / 下一屏完整露出。
                    sg = {"y_top": c["y_top"], "y_bottom": c["y_bottom"],
                          "content": e.content}
                    if len(e.content.strip()) <= 3:
                        # 短文本全量复核（防 OCR 幻读 "R"/"B"/"T"）
                        ct2, _, ph2 = _classify_media_seg(rec_img, sg)
                        if ct2 in (None, "text"):
                            ct2, ph2 = "text", None
                    else:
                        ct2, ph2 = _classify_text_suspect(rec_img, sg)
                    if ct2 != "text":
                        e.content_type = ct2
                        e.content = _MEDIA_PLACEHOLDER[ct2]
                        e.no_inline = True   # 截断卡不当屏点击（可能点不中），
                                             # 留给 media_pass/下一屏完整露出
                        if ph2 is not None:
                            e.dedup_hash = ph2
                            e.frame_phash = f"{ph2:016x}"
                    if not _known(e, existing, existing_keys, seen, seen_keys,
                                  media_hashes):
                        seen.append((e.sender, e.content_type, e.content))
                        seen_keys.add(_key(e))
                        e.crop_path = RS.save_crop(
                            rec_img, group, c["y_top"], c["y_bottom"], _key(e))
                        new_entries.append(e)
            # top_clipped / both_clipped / unidentifiable / system：跳过
            # （顶部残缺在下一 union 头像露全后变 complete，届时再采）

        n = 0
        if new_entries:
            r = message_log.append_incremental(
                conn, session_id, new_entries, source="incremental",
                gap_ok=True)
            n = r.get("inserted", 0)
            total += n
            for e in new_entries:
                existing.append((e.sender, e.content_type, e.content))
                existing_keys.add(_key(e))
                dh = getattr(e, "dedup_hash", None)
                if dh is not None:
                    media_hashes.append(dh)
            if on_new and n:
                on_new(new_entries)
        print(f"  [rnd{rnd + 1}] {rec_hint} dy={dy:5.0f} conf={conf:.2f} "
              f"新识别 {len(new_entries)} 入库 {n}（累计 {total}）")

        # ---- 调试落盘：截图原图/裁切/拼接/单条信息裁切/识别结果
        if debug_dir is not None:
            screen_data = {
                "rnd": rnd,
                "rec_hint": rec_hint,
                "dy": round(float(dy), 1),
                "conf": round(float(conf), 3),
                "new_entries": len(new_entries),
                "inserted": n,
                "total": total,
            }
            # 截图原图
            fn_full = f"screen_{rnd:02d}_full.jpg"
            cv2.imwrite(os.path.join(debug_dir, fn_full), img,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            screen_data["full"] = fn_full
            # 一次裁切（内容区裁切）
            if prev_img is not None:
                fn_crop = f"screen_{rnd:02d}_crop.jpg"
                cv2.imwrite(os.path.join(debug_dir, fn_crop), cur_c,
                            [cv2.IMWRITE_JPEG_QUALITY, 92])
                screen_data["crop"] = fn_crop
            # 一次拼接（union 缝合段）
            if st is not None:
                fn_union = f"screen_{rnd:02d}_union.jpg"
                cv2.imwrite(os.path.join(debug_dir, fn_union), rec_img,
                            [cv2.IMWRITE_JPEG_QUALITY, 92])
                screen_data["union"] = fn_union
            # 二次裁切（A/B 缝合段）
            if st is not None:
                fn_stitch = f"screen_{rnd:02d}_stitch.jpg"
                # A/B 缝合段：cur[split:dy] + prev[0:split]
                split_y = segs[0]["y_top"] if segs else 0
                parts = []
                if split_y is not None and split_y < dy:
                    parts.append(cur_c[split_y:int(dy)])
                if prev_img is not None:
                    prev_c, _, _ = _content_crop_bounds(prev_img)
                    parts.append(prev_c[0:split_y])
                stitch = np.vstack(parts) if parts else cur_c
                cv2.imwrite(os.path.join(debug_dir, fn_stitch), stitch,
                            [cv2.IMWRITE_JPEG_QUALITY, 92])
                screen_data["stitch"] = fn_stitch
            # 每个单条信息的裁切
            if segs:
                msg_files = []
                for i, sg in enumerate(segs):
                    fn_msg = f"screen_{rnd:02d}_msg_{i:02d}.jpg"
                    msg_crop = rec_img[sg["y_top"]:sg["y_bottom"]]
                    cv2.imwrite(os.path.join(debug_dir, fn_msg), msg_crop,
                                [cv2.IMWRITE_JPEG_QUALITY, 95])
                    msg_files.append({
                        "file": fn_msg,
                        "y_top": sg["y_top"],
                        "y_bottom": sg["y_bottom"],
                        "content": sg["content"][:50],
                        "factor": sg["factor"],
                        "type": sg.get("type"),
                        "avatar_score": sg.get("avatar_score"),
                        "nick_score": sg.get("nick_score"),
                        "avatar_cand": sg.get("avatar_cand"),
                        "nickname": sg.get("nickname"),
                    })
                screen_data["msgs"] = msg_files
            debug_manifest["screens"].append(screen_data)
            with open(os.path.join(debug_dir, "manifest.json"), "w",
                      encoding="utf-8") as f:
                json.dump(debug_manifest, f, ensure_ascii=False, indent=1)

        # ---- 多媒体内联处置（2026-09-01 用户定稿）：滚动中识别到本屏有
        # 完整露出的多媒体消息 → 立即点击处置（链接取 URL / 图片视频文件
        # 存电脑 / 表情包抠图），取完校验仍在原位置再继续滑动。交接处半显
        # 段本轮未入库不点（上面分段时已跳过，下一屏完整露出再采+处置）。
        # 处置后位置漂移则停止采集：已入库数据保留，下轮 journey 从书签
        # 覆盖空洞。预算热控 media_handle_inline_enabled /
        # media_handle_max_per_journey / media_handle_timeout_s，超预算的
        # 条目留 media_status='' 由采集后 media_pass 兜底。
        if handle_media and new_entries and media_handled < media_max \
                and time.time() < media_deadline:
            media_todo = [e for e in new_entries
                          if getattr(e, "content_type", "text")
                          in _MEDIA_PLACEHOLDER
                          and not getattr(e, "no_inline", False)]
            if media_todo:
                from .media_pass import (
                    _apply_result, _content_block, _msg_type_of)
                from ..ports.android.perception.media_handler import (
                    MediaHandler, MediaTask)
                if media_handler is None and media_handler_inst is None:
                    media_handler_inst = MediaHandler(dev)
                h = media_handler or media_handler_inst
                for e in media_todo:
                    if media_handled >= media_max \
                            or time.time() >= media_deadline:
                        break
                    row = conn.execute(
                        "SELECT id FROM messages WHERE session_id=?"
                        " AND crop_path=? ORDER BY id DESC LIMIT 1",
                        (session_id,
                         getattr(e, "crop_path", "") or "")).fetchone()
                    if row is None:
                        continue
                    mid = row["id"]
                    # 红包：不点击（用户定的，避免资金/社交风险），直接标 done
                    if e.content_type == "red_packet":
                        message_log.update_media(conn, mid, media_status="done")
                        media_handled += 1
                        continue
                    yt, yb = _seg_dev_y(
                        {"y_top": e.ybounds[0], "y_bottom": e.ybounds[1]})
                    strip = (0, yt, img.shape[1], yb - yt)
                    screen_now = dev.capture_bytes()
                    tap_bbox = _content_block(screen_now, strip) \
                        if screen_now is not None else strip
                    task = MediaTask(msg_id=str(mid),
                                     msg_type=_msg_type_of(e.content_type),
                                     bbox=tap_bbox, screen_path="",
                                     group_name=group)
                    pre_c, _, _ = _content_crop_bounds(img)
                    try:
                        result = h.handle(task)
                    except Exception:  # noqa: BLE001
                        log.exception("[%s] 媒体 #%s 内联处置异常", group, mid)
                        message_log.update_media(conn, mid,
                                                 media_status="failed")
                        media_handled += 1
                        continue
                    _apply_result(conn, mid, result)
                    media_handled += 1
                    log.info("[%s] 媒体 #%s (%s→%s) 内联处置%s", group, mid,
                             e.content_type, result.msg_type,
                             "完成" if result.success
                             else f"失败: {result.error}")
                    # 位置校验：处置返回后应仍在原屏（微信从详情页返回一般
                    # 恢复滚动位置）。漂移（conf 低/有位移）→ 重截一次复核，
                    # 仍漂移则停止采集。
                    time.sleep(0.5)
                    now = dev.capture_bytes()
                    now_c, _, _ = _content_crop_bounds(now)
                    dyv, confv = find_overlap_dy(pre_c, now_c)
                    if confv < 0.5 or dyv >= 40:
                        time.sleep(1.0)
                        now = dev.capture_bytes()
                        now_c, _, _ = _content_crop_bounds(now)
                        dyv, confv = find_overlap_dy(pre_c, now_c)
                    if confv < 0.5 or dyv >= 40:
                        log.warning("[%s] 媒体处置后位置漂移(dy=%.0f,"
                                    "conf=%.2f)，停止本次采集",
                                    group, dyv, confv)
                        media_drift_stop = True
                        break
            if media_drift_stop:
                break

        # ---- 双因子失配调和（2026-08-25 用户定稿：融入日常 journey 流水）。
        # 识别失配（uncertain_entity）的消息仍在屏上时，点其头像进资料页，
        # 按 roster_update 四规则调和（改名/换头像写回花名册；定位不到成员
        # 且资料完整时作为新成员动态学习入库）。在滑动前做，点完即回聊天页。
        # 坐标映射：union 行 y_u → 设备行 内容顶+y_u（cur 内容行 k 与 prev
        # 行 k-dy 是同一行，线性映射）；首屏/单屏识别时坐标即设备坐标。
        if reconcile and rm.avatar_templates \
                and reconcile_done < reconcile_max_total:
            from ..ports.android.perception import roster_update
            from ..ports.android.perception.page_detector import (
                detect_input_bar_top, detect_pinned_bar_end)
            top_c = detect_pinned_bar_end(img) or CONTENT_Y0
            bottom_c = detect_input_bar_top(img) or INPUT_BAR_Y0
            n_this = 0
            for m in res["messages"]:
                if n_this >= reconcile_max_per_round \
                        or reconcile_done >= reconcile_max_total:
                    break
                if not m.get("uncertain_entity"):
                    continue
                av = m.get("avatar") or {}
                # avatar.side 是 'L'/'R'（消息 side 才是 other/self）
                if m.get("side") == "self" or av.get("side") == "R" \
                        or av.get("x") is None:
                    continue
                if st is None:
                    dev_x, dev_y = av["x"], av["y"]
                else:
                    dev_x, dev_y = av["x"], top_c + av["y"]
                cy_dev = dev_y + av.get("h", 0) / 2
                if not (top_c + 20 < cy_dev < bottom_c - 20):
                    continue            # 已滚出当前屏，点不到
                key = (m.get("nickname") or m.get("matched_user_name")
                       or "").strip()
                if key and key in reconciled:
                    continue
                m_dev = dict(m)
                m_dev["avatar"] = {**av, "x": dev_x, "y": dev_y}
                try:
                    actions = roster_update.reconcile_on_mismatch(
                        dev, group, m_dev, rm=rm, auto_back=True)
                except Exception:  # noqa: BLE001
                    log.exception("[%s] 资料页调和异常", group)
                    actions = []
                if key:
                    reconciled.add(key)
                reconcile_done += 1
                n_this += 1
                if actions:
                    log.info("[%s] 资料页调和: %s → %s",
                             group, key or "(无昵称)", actions)
                    rm.load_roster()   # 花名册已写回，重载模板立即生效
            # 调和后必须仍在聊天页（auto_back 已尽力返回），否则停止采集：
            # 继续滑动会在错误页面上操作，本轮已入库数据保留，下轮 journey 再试
            if n_this:
                time.sleep(0.5)
                probe = run_ocr(dev.capture_bytes())
                in_chat = any(group[:4] in i.get("text", "")
                              and i.get("cy", 9999) < 220 for i in probe)
                if not in_chat:
                    dev.back()
                    time.sleep(1.0)
                    probe = run_ocr(dev.capture_bytes())
                    in_chat = any(group[:4] in i.get("text", "")
                                  and i.get("cy", 9999) < 220 for i in probe)
                if not in_chat:
                    log.warning("[%s] 调和后未能回到聊天页，停止本次采集", group)
                    break

        # ---- 书签检测：union 首次【完整包含】上次书签 N0 = 新消息采完 → 停。
        # 放在识别之后：命中那次的 union 里、书签下方（更新侧）的新消息已
        # 在上一段入库，不会漏。前提 dy < 消息区高 → 必有一次命中（设计 §2）。
        if stop_at_anchor and anchor is not None and st is not None:
            aconf = _anchor_in_union(anchor, rec_img)
            if aconf >= ANCHOR_COVER_SUM:
                log.info("已到上次书签（union 完整包含书签 conf=%.2f），"
                         "新消息采完，停止", aconf)
                print(f"  [rnd{rnd + 1}] 已到上次书签（conf={aconf:.2f}），"
                      f"新消息采完，停止")
                break

        if stop_now:
            print(f"  [rnd{rnd + 1}] 命中停止条件（stop_when），停止采集")
            break
        empty_streak = empty_streak + 1 if n == 0 else 0
        if stop_empty_rounds > 0 and empty_streak >= stop_empty_rounds:
            print(f"  连续 {empty_streak} 屏无新入库，已到上一次位置，停止")
            break

        prev_img = img
        if rnd < max_rounds - 1:
            RS.do_swipe(dev, "earlier")
            time.sleep(0.8)      # 大位移 fling 后等惯性停稳，避免下屏糊帧

    _save_anchor(group, first_img)   # 存本次进入时的最新屏，作下次「新消息起点」书签
    return total
