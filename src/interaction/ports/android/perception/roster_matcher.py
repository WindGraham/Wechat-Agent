# -*- coding: utf-8 -*-
"""src/interaction/ports/android/perception/roster_matcher.py — 双因子 (抗偏色 Hue 色彩直方图+BGR模板匹配 + 昵称 OCR) 群成员匹配与动态学习库。

核心逻辑：
  1. 加载 workspace/group_rosters/<group_name>/members_roster.json 及对应的 PNG 圆角头像；
  2. 提取抗偏色图像特征：结合 Hue 色调直方图相关性 + 归一化 BGR 模板匹配，免疫夜间/护眼模式色彩滤镜偏色；
  3. 建立双因子匹配判定：只有在 (图像特征强匹配) 且 (昵称/群昵称在候选名单中) 时，
     才直接确定身份 (sender)；
  4. 若出现未在花名册中同时认出的新头像/新昵称，标记为未准确认定实体 (uncertain_entity=True)，
     触发资料页确认与动态学习。
"""

import json
import logging
import os
import re
import cv2
import numpy as np

log = logging.getLogger("perception.roster_matcher")

PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "..", "..", ".."))

ROSTERS_DIR = os.path.join(PROJECT_ROOT, "workspace", "group_rosters")
MANIFEST_PATH = os.path.join(ROSTERS_DIR, "manifest.json")


def sanitize_filename(filename: str) -> str:
    clean_name = re.sub(r'[\\/*?:"<>|]', '_', filename or "").strip()
    return clean_name if clean_name else "unnamed"


class RosterMatcher:
    """单群聊花名册双因子匹配与模板库"""

    def __init__(self, group_name: str, tmpl_thresh: float = 0.70):
        self.group_name = group_name
        self.tmpl_thresh = tmpl_thresh
        self.group_dir = os.path.join(ROSTERS_DIR, group_name)
        self.json_path = os.path.join(self.group_dir, "members_roster.json")
        self.avatars_dir = os.path.join(self.group_dir, "avatars")
        
        self.member_profiles = []
        self.avatar_templates = {}
        self.load_roster()

    def load_roster(self):
        """从 JSON 和 avatars 目录加载全量成员档案与图像特征模板"""
        if not os.path.exists(self.json_path):
            log.warning("[%s] 花名册 JSON 不存在: %s", self.group_name, self.json_path)
            return

        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.member_profiles = data.get("members", [])
        except Exception as e:
            log.exception("[%s] 读取花名册 JSON 失败", self.group_name)
            return

        self.avatar_templates.clear()
        if os.path.exists(self.avatars_dir):
            for m in self.member_profiles:
                main_nick = m.get("main_nickname", "").strip()
                avatar_rel = m.get("avatar_image_path", "")
                if not avatar_rel:
                    avatar_rel = f"avatars/{sanitize_filename(main_nick)}.png"
                full_path = os.path.join(self.group_dir, avatar_rel)
                if not os.path.exists(full_path):
                    continue
                
                tmpl_img = cv2.imread(full_path, cv2.IMREAD_UNCHANGED)
                if tmpl_img is None or tmpl_img.shape[0] < 20 or tmpl_img.shape[1] < 20:
                    continue
                
                if tmpl_img.shape[2] == 4:
                    bgr = tmpl_img[:, :, :3]
                    alpha = tmpl_img[:, :, 3]
                    mask = (alpha > 128).astype(np.uint8)
                else:
                    bgr = tmpl_img
                    mask = None
                    
                # 提取 Hue 色调直方图
                hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
                hist_hue = cv2.calcHist([hsv], [0], mask, [180], [0, 180])
                cv2.normalize(hist_hue, hist_hue, 0, 1, cv2.NORM_MINMAX)

                self.avatar_templates[main_nick] = {
                    "bgr": bgr,
                    "mask": mask,
                    "hist_hue": hist_hue,
                    "profile": m
                }

        log.info("[%s] 成功加载花名册，成员数=%d, 图像模板数=%d",
                 self.group_name, len(self.member_profiles), len(self.avatar_templates))

    def _match_image_score(self, crop_avatar_bgr, tmpl) -> float:
        """复合特征匹配：RGB 归一化得分 + Hue 色图相关性，彻底免疫护眼/夜间模式色彩滤镜偏色。"""
        h, w = crop_avatar_bgr.shape[:2]
        mask = tmpl["mask"]
        bgr = tmpl["bgr"]
        hist_tmpl = tmpl["hist_hue"]
        
        # 截取框正方形拉伸对齐
        side_len = max(w, h)
        crop_sq = cv2.resize(crop_avatar_bgr, (side_len, side_len))
        bgr_r = cv2.resize(bgr, (side_len, side_len), interpolation=cv2.INTER_AREA)
        mask_r = cv2.resize(mask, (side_len, side_len), interpolation=cv2.INTER_NEAREST) if mask is not None else None
        
        # 1. 归一化 BGR 模板匹配得分
        if mask_r is not None:
            s_bgr = float(cv2.matchTemplate(crop_sq, bgr_r, cv2.TM_CCOEFF_NORMED, mask=mask_r)[0][0])
        else:
            s_bgr = float(cv2.matchTemplate(crop_sq, bgr_r, cv2.TM_CCOEFF_NORMED)[0][0])

        # 2. Hue 色调直方图相关性得分 (抗夜间/护眼模式滤镜偏色)
        hsv_crop = cv2.cvtColor(crop_sq, cv2.COLOR_BGR2HSV)
        hist_crop = cv2.calcHist([hsv_crop], [0], mask_r, [180], [0, 180])
        cv2.normalize(hist_crop, hist_crop, 0, 1, cv2.NORM_MINMAX)
        s_hue = float(cv2.compareHist(hist_crop, hist_tmpl, cv2.HISTCMP_CORREL))

        # 复合分：由于夜间模式降低了 RGB 分，当 s_hue 非常高 (>=0.90) 且 s_bgr >= 0.50 时提权
        if s_hue >= 0.90 and s_bgr >= 0.50:
            combo_score = max(s_bgr, 0.5 * s_bgr + 0.5 * s_hue)
        else:
            combo_score = s_bgr

        return combo_score

    def match_dual_factor(self, crop_avatar_bgr, ocr_nickname: str) -> tuple:
        """双因子匹配判定 (图像特征 + 昵称 OCR)。"""
        if crop_avatar_bgr is None or crop_avatar_bgr.size == 0:
            return False, "", {}

        clean_ocr_nick = (ocr_nickname or "").strip()
        best_score = -1.0
        best_member = None
        best_name = ""

        for main_nick, tmpl in self.avatar_templates.items():
            score = self._match_image_score(crop_avatar_bgr, tmpl)
            if score > best_score:
                best_score = score
                best_member = tmpl["profile"]
                best_name = main_nick

        # 双因子逻辑判断：
        # 因子1 (图像 score >= threshold) 且 因子2 (OCR 昵称匹配主昵称或群昵称)
        if best_score >= self.tmpl_thresh and best_member is not None:
            group_nick = (best_member.get("group_nickname") or "").strip()
            main_nick = (best_member.get("main_nickname") or "").strip()
            
            nick_matched = False
            if not clean_ocr_nick:
                nick_matched = best_score >= 0.85
            else:
                if (clean_ocr_nick in main_nick or main_nick in clean_ocr_nick or
                    (group_nick and (clean_ocr_nick in group_nick or group_nick in clean_ocr_nick))):
                    nick_matched = True

            if nick_matched:
                log.debug("[%s] 抗偏色双因子匹配成功: name=%s, score=%.3f, ocr=%s",
                          self.group_name, best_name, best_score, clean_ocr_nick)
                return True, best_name, best_member

        log.debug("[%s] 双因子未同时匹配 (最高得分=%.3f, name=%s, ocr=%s)",
                  self.group_name, best_score, best_name, clean_ocr_nick)
        return False, "", {}


def check_group_manifest(group_name: str) -> bool:
    if not os.path.exists(MANIFEST_PATH):
        return False
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("groups", {}).get(group_name, {}).get("status") == "completed"
    except Exception:
        return False


def update_group_manifest(group_name: str, status: str = "completed", member_count: int = 0):
    data = {"groups": {}}
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"groups": {}}

    data.setdefault("groups", {})[group_name] = {
        "status": status,
        "last_updated": float(os.path.getmtime(os.path.join(ROSTERS_DIR, group_name, "members_roster.json")))
        if os.path.exists(os.path.join(ROSTERS_DIR, group_name, "members_roster.json")) else 0.0,
        "member_count": member_count
    }

    os.makedirs(ROSTERS_DIR, exist_ok=True)
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST_PATH)
