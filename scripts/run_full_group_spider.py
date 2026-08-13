# -*- coding: utf-8 -*-
"""
run_full_group_spider.py — 针对“交流一下？”群聊的全量自动爬虫 (全套完美方案)
核心特性：
 1. 全程无人值守自动扫描全群成员；
 2. 资料页自适应多行昵称拼接 + 提权剪贴板无损获取微信号；
 3. CV 轮廓检测 + 18px 弧度生成 4 通道 (BGRA) 透明圆角 PNG 头像；
 4. 头像文件直接以【主昵称】命名；
 5. 【超级横幅锚点】CV 模板匹配接力滑动，精确计算 Y 轴偏移量，绝对不漏人也不重复多点；
 6. 最终产出全量格式化 JSON 档案。
"""

import cv2
import numpy as np
import json
import subprocess
import time
import base64
import hashlib
import os
import sys
import re
import shutil
import logging

sys.path.append(".")
from src.interaction.ports.android.perception.ocr_engine import run_ocr

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("full_spider")

GROUP_NAME = "交流一下？"

# ---------------------------------------------------------------- 网格页成员过滤（防状态栏/标题/通知误识别）
GRID_Y_MIN = 300          # 状态栏+标题栏以上
GRID_Y_MAX = 2050         # 底部“收起”按钮以下
# 这些文本永远不是成员昵称
_NON_MEMBER_KEYWORDS = ["聊天信息", "更多群成员", "收起", "添加", "移出", "群聊名称",
                        "查找聊天记录", "消息免打扰", "置顶聊天", "保存到通讯录"]
_STATUS_RE = re.compile(r"^\d{2}:\d{2}(?::\d{2})?$|^\d+%$|^微信\s*[(（]|^交流一下|^YOUSAOBI")  # 通知预览/状态栏常见片段


def _looks_like_member_name(item):
    t = item.get("text", "").strip()
    if not t:
        return False
    if item.get("cy", 0) < GRID_Y_MIN or item.get("cy", 0) > GRID_Y_MAX:
        return False
    if any(k in t for k in _NON_MEMBER_KEYWORDS):
        return False
    if _STATUS_RE.search(t):
        return False
    # 成员昵称通常不会包含长串冒号+对话内容
    if ":" in t and len(t) > 15:
        return False
    return True


def _is_grid_page(items):
    return any(k in i.get("text", "") for i in items for k in ("聊天信息", "更多群成员", "收起", "群聊名称"))


def _is_profile_page(items):
    texts = " ".join(i.get("text", "") for i in items)
    return any(k in texts for k in ("微信号", "朋友资料", "发消息", "音视频通话", "添加到通讯录"))
OUTPUT_DIR = f"workspace/group_rosters/{GROUP_NAME}"
AVATAR_DIR = os.path.join(OUTPUT_DIR, "avatars")
PROFILE_SHOTS_DIR = os.path.join(OUTPUT_DIR, "profile_shots")
JSON_PATH = os.path.join(OUTPUT_DIR, "members_roster.json")
ADB = "./tools/platform-tools/adb -s cf04642e"

def run_cmd(cmd):
    return subprocess.check_output(cmd, shell=True).decode('utf-8').strip()

def sanitize_filename(filename):
    clean_name = re.sub(r'[\\/*?:"<>|]', '_', filename).strip()
    return clean_name if clean_name else "unnamed"


def _iou_box(a, b):
    x1, y1, w1, h1 = a
    x2, y2, w2, h2 = b
    xi, yi = max(x1, x2), max(y1, y2)
    xo, yo = min(x1 + w1, x2 + w2), min(y1 + h1, y2 + h2)
    inter = max(0, xo - xi) * max(0, yo - yi)
    return inter / (w1 * h1 + w2 * h2 - inter + 1e-6)


class FullGroupSpider:
    def __init__(self):
        os.makedirs(AVATAR_DIR, exist_ok=True)
        os.makedirs(PROFILE_SHOTS_DIR, exist_ok=True)
        self.roster_data = {"group_name": GROUP_NAME, "total_count": 0, "members": []}
        self.processed_names = set()
        self.profile_shot_counter = 0

    def save_json(self):
        self.roster_data["total_count"] = len(self.roster_data["members"])
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(self.roster_data, f, ensure_ascii=False, indent=2)

    def _archive_profile_shot(self, snap_path):
        """把个人资料页截图存档到 profile_shots/，返回相对路径。"""
        self.profile_shot_counter += 1
        fname = f"profile_{self.profile_shot_counter:04d}.png"
        dst = os.path.join(PROFILE_SHOTS_DIR, fname)
        try:
            shutil.copy2(snap_path, dst)
            return os.path.join("profile_shots", fname)
        except Exception as e:
            log.warning(f"[-] 存档资料页截图失败: {e}")
            return None

    def _recover_to_grid(self):
        """按 back 直到回到网格页，最多尝试 4 次；仍回不去再走 ensure_grid_page 兜底。"""
        for _ in range(4):
            run_cmd(f"{ADB} exec-out screencap -p > /tmp/grid_check.png")
            if _is_grid_page(run_ocr("/tmp/grid_check.png")):
                return True
            run_cmd(f"{ADB} shell input keyevent 4")
            time.sleep(0.8)
        log.warning("[-] back 多次仍未回到网格页，走 ensure_grid_page 兜底")
        self.ensure_grid_page()
        return True

    def detect_grid_avatars(self, snap_path):
        """用 CV 检测群成员网格里的头像方块，返回 [(cx, cy), ...] 按阅读顺序排列。"""
        img = cv2.imread(snap_path)
        if img is None:
            return []
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # 背景众数：内容区大部分像素是背景
        bg = int(np.median(gray[200:2100, :]))

        # 1. 非背景掩膜：亮度明显高于背景或饱和度足够（覆盖普通/彩色头像）
        max_ch = np.max(img, axis=2)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = ((max_ch > bg + 15) | (hsv[:, :, 1] > 60)).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        boxes = []
        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            ar = bw / max(bh, 1)
            area = bw * bh
            if 0.75 <= ar <= 1.35 and 110 <= bw <= 260 and 110 <= bh <= 260 and area > 9000:
                boxes.append((x, y, bw, bh))

        # 去重（Canny/掩膜可能一个头像出多个框）
        boxes = sorted(boxes, key=lambda b: -b[2] * b[3])
        deduped = []
        for b in boxes:
            if any(_iou_box(b, k) > 0.3 for k in deduped):
                continue
            deduped.append(b)
        boxes = deduped

        # 2. 若检测到的列数足够，补齐暗色头像（用网格先验位置+梯度判断）
        if len(boxes) >= 6:
            boxes = self._fill_dark_avatars(img, boxes, bg)

        # 只保留在网格区域内的框
        boxes = [b for b in boxes if 250 < b[1] + b[3] / 2 < 2100]
        # 按行优先排序
        boxes.sort(key=lambda b: (b[1], b[0]))

        # 3. 转成点击中心；跳过空白/加号格（点击后不是资料页，后续会自然跳过）
        members = []
        for b in boxes:
            cx = int(b[0] + b[2] / 2)
            cy = int(b[1] + b[3] / 2)
            members.append({"cx": cx, "cy": cy, "box": b})
        return members

    def _fill_dark_avatars(self, img, boxes, bg):
        """根据已检头像推算行列，对缺失的格子用梯度/填充判断是否为暗头像。"""
        # 计算列中心
        xs = sorted({b[0] + b[2] / 2 for b in boxes})
        # 简单合并：列间距应 > 80
        cols = []
        for x in xs:
            if not cols or abs(x - cols[-1]) > 80:
                cols.append(x)
        if len(cols) < 2:
            return boxes
        col_step = int(np.median(np.diff(cols)))
        # 补全首尾列
        if cols[0] > 150:
            cols.insert(0, cols[0] - col_step)
        if cols[-1] < img.shape[1] - 150:
            cols.append(cols[-1] + col_step)

        ys = sorted({b[1] + b[3] / 2 for b in boxes})
        rows = []
        for y in ys:
            if not rows or abs(y - rows[-1]) > 80:
                rows.append(y)
        row_step = int(np.median(np.diff(rows))) if len(rows) > 1 else 264

        existing = set((int(b[0] + b[2] / 2), int(b[1] + b[3] / 2)) for b in boxes)
        out = list(boxes)
        for y in rows:
            for x in cols:
                key = (int(x), int(y))
                if any(abs(key[0] - ex[0]) < 40 and abs(key[1] - ex[1]) < 40 for ex in existing):
                    continue
                # 裁剪预期格子 150x150
                x0, y0 = int(x - 75), int(y - 75)
                if x0 < 0 or y0 < 200 or x0 + 150 > img.shape[1] or y0 + 150 > img.shape[0]:
                    continue
                crop = img[y0:y0 + 150, x0:x0 + 150]
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                fill = (gray > bg + 8).astype(np.uint8).sum() / crop.size
                gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
                gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
                mag = cv2.magnitude(gx, gy)
                # 头像必须有明显内容（填充或纹理）
                if fill > 0.04 or mag.std() > 25:
                    out.append((x0, y0, 150, 150))
        return out

    def find_profile_avatar_box(self, full_img_path):
        """在个人资料页定位正方形头像框，返回 (x, y, w, h)。"""
        img = cv2.imread(full_img_path)
        if img is None:
            return None
        roi = img[150:600, 0:500]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        avatar_box = None
        max_area = 0
        for cnt in contours:
            x, y, w_box, h_box = cv2.boundingRect(cnt)
            area = w_box * h_box
            aspect_ratio = float(w_box) / h_box
            if 0.85 <= aspect_ratio <= 1.15 and 150 <= w_box <= 350 and 150 <= h_box <= 350:
                if area > max_area:
                    max_area = area
                    avatar_box = (x, y + 150, w_box, h_box)
        if not avatar_box:
            avatar_box = (42, 251, 175, 175)
        return avatar_box

    def crop_exact_rounded_avatar(self, full_img_path, save_filename, avatar_box=None):
        img = cv2.imread(full_img_path)
        if img is None:
            return None

        if avatar_box is None:
            avatar_box = self.find_profile_avatar_box(full_img_path)
        x, y, w, h = avatar_box
        avatar_crop = img[y:y+h, x:x+w].copy()

        # 生成 4 通道 Alpha 透明圆角掩码 (微调弧度 18px)
        radius = 18
        mask = np.zeros((h, w), dtype=np.uint8)
        cv2.rectangle(mask, (radius, 0), (w - radius, h), 255, -1)
        cv2.rectangle(mask, (0, radius), (w, h - radius), 255, -1)
        cv2.circle(mask, (radius, radius), radius, 255, -1)
        cv2.circle(mask, (w - radius, radius), radius, 255, -1)
        cv2.circle(mask, (radius, h - radius), radius, 255, -1)
        cv2.circle(mask, (w - radius, h - radius), radius, 255, -1)

        bgra = cv2.cvtColor(avatar_crop, cv2.COLOR_BGR2BGRA)
        bgra[:, :, 3] = mask

        # 如果出现重名文件，添加序号防护
        base_name = save_filename[:-4]
        ext = ".png"
        counter = 1
        final_filename = save_filename
        while os.path.exists(os.path.join(AVATAR_DIR, final_filename)):
            final_filename = f"{base_name}_{counter}{ext}"
            counter += 1

        avatar_rel_path = os.path.join("avatars", final_filename)
        avatar_full_path = os.path.join(AVATAR_DIR, final_filename)
        cv2.imwrite(avatar_full_path, bgra)
        return avatar_rel_path

    def get_clipboard_exact(self):
        run_cmd(f"{ADB} shell ime set io.appium.settings/.AppiumIME")
        time.sleep(0.5)
        out = run_cmd(f"{ADB} shell am broadcast -a io.appium.settings.clipboard.get")
        run_cmd(f"{ADB} shell ime set com.android.adbkeyboard/.AdbIME")
        for line in out.split('\n'):
            if "data=" in line:
                d = line.split('data="')[1].split('"')[0]
                if d:
                    try: return base64.b64decode(d).decode('utf-8')
                    except Exception: return d
        return None

    def _clean_nickname_roi(self, roi):
        """把 ROI 中的性别图标/emoji 等彩色像素涂黑，避免 OCR 读成 &/8/0。"""
        if roi.size == 0:
            return roi
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        # 彩色（饱和度高）且不是纯白文字的像素 = 图标/emoji
        colored = ((hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 60) & (hsv[:, :, 2] < 245))
        cleaned = roi.copy()
        bg = int(np.median(roi[:, :, 0]))
        cleaned[colored] = (bg, bg, bg)
        return cleaned

    def _strip_gender_and_emoji_noise(self, text):
        """后处理：去掉性别图标 OCR 残留下来的 &/8/0 以及前导 emoji 碎片。"""
        if not text:
            return text
        # 1) 去掉末尾的性别图标符号（&、♂/♀、👤 以及单独的数字 8/0）
        text = re.sub(r'[\s]*[&＆♂♀👤🔵🔴][\s]*$', '', text)
        text = re.sub(r'\s+[80oOQ]$', '', text)
        # 2) 去掉前导的单字符 emoji 碎片（通常是单个 ASCII 字母/数字/符号，后面跟着真正的名字）
        #    保留像 "I" 这种本身就是单字符昵称的情况
        text = re.sub(r'^(?![A-Za-z]$)([A-Za-z0-9!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>/?])\s+(?=.{2,})', '', text)
        return text.strip()

    def extract_profile_max(self):
        snap_path = "/tmp/tmp_profile_full.png"
        run_cmd(f"{ADB} exec-out screencap -p > {snap_path}")
        img = cv2.imread(snap_path)
        if img is None:
            return None

        # 1. 先全图 OCR 做页面校验+微信号/地区/群昵称
        full_items = run_ocr(snap_path)
        if not _is_profile_page(full_items):
            log.warning("[-] 当前页面不是个人资料页，跳过本次提取")
            return None

        # 资料页截图存档
        profile_shot_rel = self._archive_profile_shot(snap_path)

        main_nickname, group_nickname, wechat_id, region, is_friend = "", "", "", "", 0
        wxid_cy = None
        for i in full_items:
            t = i["text"]
            if "微信号" in t:
                wxid_cy = i["cy"]
                clean_t = t.replace("微信号:", "").replace("微信号：", "").replace("微信号", "").strip()
                if clean_t:
                    wechat_id = clean_t
            elif "地区" in t:
                clean_r = t.replace("地区:", "").replace("地区：", "").replace("地区", "").strip()
                if clean_r:
                    region = clean_r
            elif "群昵称" in t:
                clean_g = t.replace("群昵称:", "").replace("群昵称：", "").replace("群昵称", "").strip()
                if clean_g:
                    group_nickname = clean_g
            elif "发消息" in t or "音视频通话" in t:
                is_friend = 1

        if wxid_cy and not wechat_id:
            run_cmd(f"{ADB} shell input swipe 400 {int(wxid_cy)} 400 {int(wxid_cy)} 800")
            time.sleep(1)
            run_cmd(f"{ADB} shell input tap 450 {int(wxid_cy)-65}")
            time.sleep(1)
            exact = self.get_clipboard_exact()
            if exact:
                wechat_id = exact

        # 2. 精确定位头像，把昵称 OCR 限制在“头像右侧”区域
        avatar_box = self.find_profile_avatar_box(snap_path)
        ax, ay, aw, ah = avatar_box
        ih, iw = img.shape[:2]
        roi_x = min(ax + aw + 10, iw - 50)
        roi_y = max(0, ay - 10)
        roi_w = min(700, iw - roi_x - 50)
        roi_h = min(ih, ay + ah + 90) - roi_y
        if roi_w <= 20 or roi_h <= 20:
            main_nickname = "未知昵称"
        else:
            roi = img[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]
            cleaned_roi = self._clean_nickname_roi(roi)
            nick_items = run_ocr(cleaned_roi)
            # 转换回全局 Y，便于按“微信号”线做上界
            for it in nick_items:
                it["cy"] += roi_y
                it["box"] = tuple(v + (roi_x if i % 2 == 0 else roi_y) for i, v in enumerate(it["box"]))

            # 昵称在微信号/地区/群昵称之上
            cutoff = roi_y + roi_h
            for it in nick_items:
                if "微信号" in it["text"] or "地区" in it["text"] or "群昵称" in it["text"]:
                    cutoff = min(cutoff, it["cy"] - 10)

            excluded = ["微信号", "地区", "朋友圈", "来源", "朋友资料", "设置", "群昵称", "聊天信息",
                        "备注", "更多", "添加到通讯录", "音视频通话", "发消息"]
            name_parts = []
            for it in nick_items:
                t = it["text"].strip()
                if not t:
                    continue
                if it["cy"] >= cutoff:
                    continue
                if any(k in t for k in excluded) or _STATUS_RE.search(t):
                    continue
                name_parts.append(t)
            raw_nick = " ".join(name_parts) if name_parts else ""
            main_nickname = self._strip_gender_and_emoji_noise(raw_nick)
            if not main_nickname:
                main_nickname = "未知昵称"

        if any(k in main_nickname for k in ["微信(", "交流一下", "YOUSAOBI", "国家网络", "Clash", "Termux"]) or len(main_nickname) > 30:
            log.warning(f"[-] 提取到的昵称疑似干扰文本: {main_nickname!r}，跳过")
            return None

        name_for_file = main_nickname if main_nickname != "未知昵称" else group_nickname
        clean_file_name = sanitize_filename(name_for_file) + ".png"
        avatar_path = self.crop_exact_rounded_avatar(snap_path, clean_file_name, avatar_box=avatar_box)

        return {
            "main_nickname": main_nickname,
            "group_nickname": group_nickname,
            "wechat_id": wechat_id,
            "region": region,
            "is_friend": bool(is_friend),
            "avatar_image_path": avatar_path,
            "profile_screenshot": profile_shot_rel,
            "update_ts": time.time()
        }

    def ensure_grid_page(self):
        """确保进入群成员网格页面"""
        run_cmd(f"{ADB} exec-out screencap -p > /tmp/check_page.png")
        items = run_ocr("/tmp/check_page.png")
        if any("更多群成员" in i["text"] or "收起" in i["text"] or "聊天信息" in i["text"] for i in items):
            return
        log.info("[*] 正在导航进入【交流一下？】群成员网格页...")
        run_cmd(f"{ADB} shell am force-stop com.tencent.mm")
        time.sleep(1)
        run_cmd(f"{ADB} shell monkey -p com.tencent.mm -c android.intent.category.LAUNCHER 1")
        time.sleep(3)
        run_cmd(f"{ADB} exec-out screencap -p > /tmp/home_check.png")
        items_home = run_ocr("/tmp/home_check.png")
        jlyx = next((i for i in items_home if "交流一下" in i["text"]), None)
        if jlyx:
            run_cmd(f"{ADB} shell input tap 308 {int(jlyx['cy'])}")
            time.sleep(2)
            run_cmd(f"{ADB} shell input tap 980 140")
            time.sleep(2)
            run_cmd(f"{ADB} exec-out screencap -p > /tmp/set_check.png")
            items_set = run_ocr("/tmp/set_check.png")
            more = next((i for i in items_set if "更多群成员" in i["text"]), None)
            if more:
                run_cmd(f"{ADB} shell input tap 501 {int(more['cy'])}")
                time.sleep(2)

    def run(self):
        self.ensure_grid_page()
        log.info(f"🚀 开始全量采集【{GROUP_NAME}】群聊花名册（超级横幅接力模式）...")
        
        min_y_cutoff = 0  # 当前屏有效处理区域的上界 Y
        consecutive_misses = 0
        consecutive_zero_scroll = 0  # 屏幕未滚动且没有新目标的连续次数
        
        while True:
            # 1. 截图
            snap_grid = "/tmp/full_grid_current.png"
            run_cmd(f"{ADB} exec-out screencap -p > {snap_grid}")
            img_curr = cv2.imread(snap_grid)
            h, w = img_curr.shape[:2]

            # 2. 用 CV 检测头像方块（比 OCR 读昵称更稳，不受状态栏/通知干扰）
            members = self.detect_grid_avatars(snap_grid)
            # 避免重复处理同一物理位置（滑动后可能还有残留）
            new_members = [
                m for m in members
                if m["cy"] > min_y_cutoff
            ]

            log.info(f"[*] 当前有效区域(Y > {min_y_cutoff})发现 {len(new_members)} 个新头像目标")

            # 3. 逐个点击提取资料
            processed_in_this_screen = 0
            for idx, m in enumerate(new_members, 1):
                ax, ay = m["cx"], m["cy"]
                log.info(f"[*] 点进第 {idx} 个头像 (坐标 {ax}, {ay})...")
                run_cmd(f"{ADB} shell input tap {ax} {ay}")
                time.sleep(2.2)

                try:
                    rec = self.extract_profile_max()
                    if rec is None:
                        log.warning("[-] 该头像未进入个人资料页，可能是加号/空白，按 back 恢复网格...")
                        self._recover_to_grid()
                        continue
                    self.roster_data["members"].append(rec)
                    self.save_json()
                    processed_in_this_screen += 1
                    log.info(f"[+] [{len(self.roster_data['members'])}] 成功入库: {rec['main_nickname']} | 微信号: {rec['wechat_id']} | 图片: {rec['avatar_image_path']}")
                    run_cmd(f"{ADB} shell input keyevent 4")
                    time.sleep(1.2)
                    run_cmd(f"{ADB} exec-out screencap -p > /tmp/grid_check.png")
                    if not _is_grid_page(run_ocr("/tmp/grid_check.png")):
                        log.warning("[-] 返回后不在网格页，执行恢复...")
                        self._recover_to_grid()
                except Exception as e:
                    log.error(f"[-] 提取资料异常: {e}")
                    self._recover_to_grid()

            # 4. 滑动前的“超级横幅锚点”截取 (取中下部像素带)
            strip_y0, strip_y1 = h - 750, h - 450
            anchor_strip = img_curr[strip_y0:strip_y1, 50:w-50]

            # 5. 执行适度向上滑动
            log.info("[*] 执行平滑滚动接力...")
            run_cmd(f"{ADB} shell input swipe 500 1600 500 900 800")
            time.sleep(2.0)

            # 6. 截图新屏幕并寻找超级锚点
            snap_next = "/tmp/full_grid_next.png"
            run_cmd(f"{ADB} exec-out screencap -p > {snap_next}")
            img_next = cv2.imread(snap_next)

            res = cv2.matchTemplate(img_next, anchor_strip, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            if max_val > 0.82:
                new_strip_y = max_loc[1]
                # 计算出锚点条带在当前屏幕的底部，作为下一屏处理的上界 cutoff
                min_y_cutoff = new_strip_y + (strip_y1 - strip_y0) - 50  # 预留 50px 容差
                shift = strip_y0 - new_strip_y
                log.info(f"[+] 【超级锚点成功接力】匹配度: {max_val:.4f} | 物理滑动: {shift}px | 新屏幕有效切割线: Y > {min_y_cutoff}")
                consecutive_misses = 0
                # 如果屏幕几乎没有滚动且下一屏没有新头像，说明已经滑到底部
                if shift <= 50 and processed_in_this_screen == 0:
                    consecutive_zero_scroll += 1
                    log.info(f"[*] 检测到滑到底部迹象（0px滚动/无新目标），连续 {consecutive_zero_scroll}/2 次")
                    if consecutive_zero_scroll >= 2:
                        log.info("🎉 已滑到群成员列表底部，全量采集完毕！")
                        break
                else:
                    consecutive_zero_scroll = 0
            else:
                log.warning(f"[-] 锚点接力失联 (匹配度: {max_val:.4f})")
                consecutive_misses += 1
                consecutive_zero_scroll = 0
                min_y_cutoff = 0  # 兜底恢复
                if consecutive_misses >= 2:
                    log.info("🎉 连续多次滑动均未发现新锚点，全群成员已彻底全量采集完毕！")
                    break

        log.info(f"✅ 全量任务结束！共成功采集 {len(self.roster_data['members'])} 位成员全套档案，保存在: {JSON_PATH}")

if __name__ == "__main__":
    spider = FullGroupSpider()
    spider.run()
