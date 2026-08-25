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

# 双因子匹配预筛：缩略图 L2 距离取 top-K 再全量 matchTemplate。
# 实测全量 500 模板 matchTemplate ~1.6s/条（唯一瓶颈）；预筛 top-K 降到 ~150ms。
# top-K 之外漏掉的候选最坏后果是标 uncertain（等价于无花名册时的纯 OCR 昵称），
# 不会误判身份，故 K 取 50 在速度与召回间足够稳健。
PRUNE_TOPK = 50

# 快速通道得分落在 [灰区下界, 阈值) 时，触发鲁棒通道（多尺度滑窗+中心带）复核。
# 灰区下界取 0.45：低于此分即使鲁棒通道抬分也难越阈值，不值得花算力。
GRAY_ZONE_LO = 0.45
ROBUST_TOPN = 10


def _trim_uniform_borders(img, tol=12, min_side=40, max_frac=0.10):
    """去掉四边的纯色裁切缝（拼接缝/裁切残留通常是 std≈0 的纯色行/列）。

    实测截断缝多为纯黑/纯白 4~8px。必须限制修剪深度：白色/纯色背景的
    头像边缘本身也是纯色行，无上限会把真实内容修掉（评测 60→56），
    故每边最多修 max_frac（默认 10%）。
    """
    out = img
    max_h = max(1, int(img.shape[0] * max_frac))
    max_w = max(1, int(img.shape[1] * max_frac))
    n = 0
    while out.shape[0] > min_side and n < max_h and out[0].std() < tol:
        out = out[1:]; n += 1
    n = 0
    while out.shape[0] > min_side and n < max_h and out[-1].std() < tol:
        out = out[:-1]; n += 1
    n = 0
    while out.shape[1] > min_side and n < max_w and out[:, 0].std() < tol:
        out = out[:, 1:]; n += 1
    n = 0
    while out.shape[1] > min_side and n < max_w and out[:, -1].std() < tol:
        out = out[:, :-1]; n += 1
    return out if out.size else img


def sanitize_filename(filename: str) -> str:
    clean_name = re.sub(r'[\\/*?:"<>|]', '_', filename or "").strip()
    return clean_name if clean_name else "unnamed"


def _lcs_len(a: str, b: str) -> int:
    """最长公共子序列长度（O(n*m)，中文昵称短，开销可忽略）。"""
    if not a or not b:
        return 0
    n, m = len(a), len(b)
    dp = [0] * (m + 1)
    for i in range(1, n + 1):
        prev = 0
        for j in range(1, m + 1):
            tmp = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev + 1
            elif dp[j] < dp[j - 1]:
                dp[j] = dp[j - 1]
            prev = tmp
    return dp[m]


def _nick_similarity(ocr_nick: str, roster_nick: str) -> float:
    """昵称匹配分（0~1）：
      - 完全相等 / 一方包含另一方（如 OCR 多读/漏读几个字符）→ 1.0
      - 否则用 LCS 归一化相似度（2*LCS/(len_a+len_b)），OCR 变体（如
        「2600小登」vs「26ee小登」）也能给出中间分。
    """
    a = (ocr_nick or "").strip()
    b = (roster_nick or "").strip()
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    lcs = _lcs_len(a, b)
    return 2.0 * lcs / (len(a) + len(b))


class RosterMatcher:
    """单群聊花名册双因子匹配与模板库"""

    def __init__(self, group_name: str, tmpl_thresh: float = 0.70):
        self.group_name = group_name
        self.tmpl_thresh = tmpl_thresh
        self.group_dir = os.path.join(ROSTERS_DIR, group_name)
        self.json_path = os.path.join(self.group_dir, "members_roster.json")
        self.avatars_dir = os.path.join(self.group_dir, "avatars")
        
        self.member_profiles = []
        self.avatar_templates = []   # 每项含 profile + 图像特征（当前头像 + 曾用头像各一项）
        self._thumb_mat = None       # 预筛用的堆叠缩略图签名 (N, 256)
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
        for m in self.member_profiles:
            main_nick = m.get("main_nickname", "").strip()
            # 头像候选：当前头像 + 曾用头像（都参与双因子识别，并集）
            rel_paths = [m.get("avatar_image_path", "")
                         or f"avatars/{sanitize_filename(main_nick)}.png"]
            for frm in (m.get("former_avatars") or []):
                if frm and frm not in rel_paths:
                    rel_paths.append(frm)

            for rel in rel_paths:
                full_path = os.path.join(self.group_dir, rel)
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

                # 缩略图签名（16x16 灰度，去均值归一化）：预筛候选用。
                # 灰度对夜间/护眼模式偏色鲁棒。
                g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                g = cv2.resize(g, (16, 16), interpolation=cv2.INTER_AREA)
                thumb = g.astype(np.float32).flatten()
                thumb = thumb - thumb.mean()
                thumb = thumb / (np.linalg.norm(thumb) + 1e-6)

                self.avatar_templates.append({
                    "bgr": bgr,
                    "mask": mask,
                    "hist_hue": hist_hue,
                    "thumb": thumb,
                    "profile": m,
                    "key": main_nick,
                    "avatar_path": rel,
                })

        # 预筛用的堆叠签名矩阵 (N, 256)，与 avatar_templates 同步
        self._thumb_mat = (np.stack([t["thumb"] for t in self.avatar_templates])
                           if self.avatar_templates else None)

        log.info("[%s] 成功加载花名册，成员数=%d, 图像模板数=%d",
                 self.group_name, len(self.member_profiles), len(self.avatar_templates))

    def _candidates_for(self, crop_avatar_bgr):
        """预筛候选：按缩略图 L2 距离取 top-K，避免对全量模板逐个 matchTemplate。

        全量 matchTemplate 是唯一瓶颈（~3ms/个，500 模板 ≈ 1.6s/条）；
        预筛后只对 top-K 做全量匹配（~150ms/条）。漏掉 top-K 之外的候选最坏
        后果是标 uncertain_entity（等价于无花名册时的纯 OCR 昵称），不会误判。
        """
        if self._thumb_mat is None or len(self.avatar_templates) <= PRUNE_TOPK:
            return self.avatar_templates
        g = cv2.cvtColor(crop_avatar_bgr, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (16, 16), interpolation=cv2.INTER_AREA)
        v = g.astype(np.float32).flatten()
        v = v - v.mean()
        v = v / (np.linalg.norm(v) + 1e-6)
        dists = np.linalg.norm(self._thumb_mat - v, axis=1)
        idx = np.argsort(dists)[:PRUNE_TOPK]
        return [self.avatar_templates[i] for i in idx]

    def _match_image_score(self, crop_avatar_bgr, tmpl) -> float:
        """复合特征匹配：RGB 归一化得分 + Hue 色图相关性，彻底免疫护眼/夜间模式色彩滤镜偏色。

        对原图与修边图各算一次取 max：修边治裁切缝，但对纯色背景头像
        可能误修真实内容（10% 上限内仍可能），取 max 保证不退化。
        """
        return max(
            self._score_single(crop_avatar_bgr, tmpl),
            self._score_single(_trim_uniform_borders(crop_avatar_bgr), tmpl),
        )

    def _score_single(self, crop_avatar_bgr, tmpl) -> float:
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

    def _robust_image_score(self, crop_avatar_bgr, tmpl,
                            scales=(0.85, 1.0, 1.18)) -> float:
        """鲁棒通道：多尺度滑窗 + 中心带兜底，处理裁切偏差/截断/缝隙。

        快速通道（等尺寸拉伸）假设裁切框恰好套住头像；实际裁切常有
        偏移/缝隙/截断（实测 offcenter 退化下快速通道命中率仅 31/60，
        本通道 60/60）。仅对灰区候选调用，不参与常规打分。

        三路取 max：
          A. 修边后的等尺寸分（同快速通道，输入已修边）；
          B. 多尺度滑窗：模板缩放到 crop 附近尺度后在 crop 内滑动
             （容忍裁切框偏移/多切边）；模板比 crop 大时反向滑窗
             （crop 在模板里找位置，容忍头像被截断）；
          C. 中心带：裁掉 crop 各边 12% 只比核心区（容忍边缘缝隙），
             得分乘 0.985 降权（核心区匹配天然偏高）。
        """
        crop = _trim_uniform_borders(crop_avatar_bgr)
        if crop.shape[0] < 30 or crop.shape[1] < 30:
            crop = crop_avatar_bgr
        bgr = tmpl["bgr"]
        mask = tmpl["mask"]
        ch, cw = crop.shape[:2]
        best = self._match_image_score(crop, tmpl)

        # B. 多尺度滑窗
        for f in scales:
            tw = max(20, int(cw * f))
            t_s = cv2.resize(bgr, (tw, tw), interpolation=cv2.INTER_AREA)
            m_s = cv2.resize(mask, (tw, tw), interpolation=cv2.INTER_NEAREST) \
                if mask is not None else None
            if tw <= cw and tw <= ch:
                if m_s is not None:
                    res = cv2.matchTemplate(crop, t_s, cv2.TM_CCOEFF_NORMED, mask=m_s)
                else:
                    res = cv2.matchTemplate(crop, t_s, cv2.TM_CCOEFF_NORMED)
                best = max(best, float(res.max()))
            elif cw >= 24 and ch >= 24 and cw <= tw and ch <= tw:
                # 头像被截断（crop 比标准小）：crop 在模板内滑窗
                res = cv2.matchTemplate(t_s, crop, cv2.TM_CCOEFF_NORMED)
                best = max(best, float(res.max()))

        # C. 中心带兜底
        if ch >= 60 and cw >= 60:
            my, mx = int(ch * 0.12), int(cw * 0.12)
            core = crop[my:ch - my, mx:cw - mx]
            side2 = max(core.shape[:2])
            core_sq = cv2.resize(core, (side2, side2))
            t_c = cv2.resize(bgr, (side2, side2), interpolation=cv2.INTER_AREA)
            s2 = float(cv2.matchTemplate(core_sq, t_c, cv2.TM_CCOEFF_NORMED)[0][0])
            best = max(best, s2 * 0.985)

        # Hue 提权（同快速通道的抗偏色逻辑，下限放宽到 0.45）
        m_r = cv2.resize(mask, (max(cw, ch), max(cw, ch)),
                         interpolation=cv2.INTER_NEAREST) if mask is not None else None
        crop_sq = cv2.resize(crop, (max(cw, ch), max(cw, ch)))
        hsv_crop = cv2.cvtColor(crop_sq, cv2.COLOR_BGR2HSV)
        hist_crop = cv2.calcHist([hsv_crop], [0], m_r, [180], [0, 180])
        cv2.normalize(hist_crop, hist_crop, 0, 1, cv2.NORM_MINMAX)
        s_hue = float(cv2.compareHist(hist_crop, tmpl["hist_hue"], cv2.HISTCMP_CORREL))
        if s_hue >= 0.90 and best >= 0.45:
            best = max(best, 0.5 * best + 0.5 * s_hue)
        return best

    def match_dual_factor(self, crop_avatar_bgr, ocr_nickname: str) -> tuple:
        """双因子匹配判定 (图像特征 + 昵称 OCR)。

        昵称候选 = 主昵称 + 群昵称 + 曾用群昵称（并集）；
        图像候选 = 当前头像 + 曾用头像（并集，load_roster 已统一提取特征）。

        返回 (matched, name, info)，info 含匹配度数值（2026-08-14 新增）：
          - avatar_score: 最佳候选的头像复合匹配分（0~1，BGR 模板 + Hue 直方图）
          - avatar_cand: 该候选成员昵称
          - nick_score: 昵称因子匹配分（1.0=完全一致/包含；0.0=无昵称或完全不匹配；
            中间=最长公共子串/编辑相似度）
          - thresh: 本实例头像阈值 tmpl_thresh
        """
        info = {"avatar_score": -1.0, "avatar_cand": "", "nick_score": 0.0,
                "thresh": self.tmpl_thresh}
        if crop_avatar_bgr is None or crop_avatar_bgr.size == 0:
            return False, "", info

        clean_ocr_nick = (ocr_nickname or "").strip()
        best_score = -1.0
        best_member = None
        best_name = ""

        candidates = self._candidates_for(crop_avatar_bgr)
        scored = []
        for tmpl in candidates:
            score = self._match_image_score(crop_avatar_bgr, tmpl)
            scored.append((score, tmpl))
            if score > best_score:
                best_score = score
                best_member = tmpl["profile"]
                best_name = tmpl["key"]

        # 灰区复核：快速通道得分接近阈值但未达标时，对 top-N 跑鲁棒通道
        # （多尺度滑窗+中心带），挽救裁切偏移/缝隙/截断造成的误判失配。
        if GRAY_ZONE_LO <= best_score < self.tmpl_thresh and scored:
            top = sorted(scored, key=lambda x: -x[0])[:ROBUST_TOPN]
            for _, tmpl in top:
                rs = self._robust_image_score(crop_avatar_bgr, tmpl)
                if rs > best_score:
                    best_score = rs
                    best_member = tmpl["profile"]
                    best_name = tmpl["key"]
            info["robust"] = True

        info["avatar_score"] = round(float(best_score), 3)
        info["avatar_cand"] = best_name

        # ---- 昵称因子：与最佳候选的主/群/曾用群昵称计算匹配分 ----
        nick_score = 0.0
        if best_member is not None:
            nicks = [
                (best_member.get("main_nickname") or "").strip(),
                (best_member.get("group_nickname") or "").strip(),
            ]
            nicks += [n.strip() for n in (best_member.get("former_group_nicknames") or [])
                      if n and n.strip()]
            nicks = [n for n in nicks if n]
            if not clean_ocr_nick:
                # 无昵称 OCR：头像高分(>=0.85)视为昵称因子可信
                nick_score = 1.0 if best_score >= 0.85 else 0.0
            else:
                for n in nicks:
                    s = _nick_similarity(clean_ocr_nick, n)
                    nick_score = max(nick_score, s)
        info["nick_score"] = round(float(nick_score), 3)

        # 双因子：因子1 图像分达标 且 因子2 昵称命中
        if best_score >= self.tmpl_thresh and best_member is not None \
                and nick_score >= 0.60:
            log.debug("[%s] 抗偏色双因子匹配成功: name=%s, score=%.3f, ocr=%s",
                      self.group_name, best_name, best_score, clean_ocr_nick)
            info["matched_name"] = best_name
            return True, best_name, info

        log.debug("[%s] 双因子未同时匹配 (最高得分=%.3f, name=%s, ocr=%s)",
                  self.group_name, best_score, best_name, clean_ocr_nick)
        return False, "", info


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
