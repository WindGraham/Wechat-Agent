# -*- coding: utf-8 -*-
"""shared/emoji_index.py — 表情包索引（决策层与交互层共享的数据检索）。

数据源：assets/emojis/index.db（label_emojis.py 生成，25110 条已标注），
文件在 assets/emojis/renamed/（000001.gif 等按 seq 编号）。

两个消费方：
- 决策层 emoji 工具：search_semantic() 检索候选 + format_candidates() 回灌 LLM
- 交互层 bundle_sender：get()/resolve() 按 seq 定位文件路径发送

检索策略（2026-08-13 用户定）：
- 模糊匹配：LIKE 子串（description/text_content/mood/use_case/style/keywords）
- 词义匹配：同义词表扩展（"开心"→高兴/兴奋/快乐…），组内 OR、组间 AND

可注入 db_path / img_dir 供离线单测（不碰真实 assets）。
"""

import json
import logging
import os
import sqlite3

log = logging.getLogger("shared.emoji_index")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "assets", "emojis", "index.db")
DEFAULT_IMG_DIR = os.path.join(PROJECT_ROOT, "assets", "emojis", "renamed")

_QUERY_COLS = ("description", "text_content", "style", "mood",
               "use_case", "keywords")

# 词义匹配同义词表：常见情绪/场景词 → 同义词组。
# 查询命中 key 或任一 value 时，整组词都参与模糊匹配（组内 OR）。
SYNONYMS = {
    "开心": ["高兴", "兴奋", "快乐", "喜悦", "好耶", "嘻嘻", "爽"],
    "笑": ["大笑", "哈哈", "哈哈哈", "搞笑", "好笑", "咧嘴"],
    "无语": ["无奈", "汗", "尴尬", "翻白眼", "服了", "崩溃", "扶额"],
    "生气": ["愤怒", "恼火", "火大", "怒", "不爽", "气死"],
    "难过": ["伤心", "悲伤", "哭", "委屈", "泪", "丧", "低落"],
    "惊讶": ["震惊", "吓", "目瞪口呆", "愣", "懵"],
    "赞": ["点赞", "棒", "厉害", "优秀", "牛", "强", "佩服"],
    "爱": ["喜欢", "爱心", "比心", "么么哒", "表白"],
    "累": ["疲惫", "困", "想睡", "躺", "瘫"],
    "感谢": ["谢谢", "感恩", "磕头", "跪谢"],
    "道歉": ["对不起", "抱歉", "跪", "认错"],
    "拒绝": ["不行", "不要", "摇头", "别"],
    "同意": ["好的", "可以", "行", "没问题", "收到", "OK"],
    "疑问": ["问号", "疑惑", "什么", "为啥", "不懂"],
    "加油": ["努力", "冲", "拼搏"],
    "晚安": ["睡", "困了", "睡觉"],
}


def _expand_query(query):
    """query 拆词，每词扩展同义词组。返回 [[词1组], [词2组], ...]。"""
    words = [w for w in (query or "").split() if w]
    groups = []
    for w in words:
        group = {w}
        for key, vals in SYNONYMS.items():
            if w == key or w in vals:
                group.add(key)
                group.update(vals)
        groups.append(sorted(group))
    return groups


class EmojiIndex:
    """表情包索引。search 系列返回 dict（含绝对路径 path）。"""

    def __init__(self, db_path=DEFAULT_DB, img_dir=DEFAULT_IMG_DIR):
        self.db_path = db_path
        self.img_dir = img_dir

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_emoji(row, img_dir):
        kw = row["keywords"]
        try:
            kw = json.loads(kw) if kw else []
        except (TypeError, ValueError):
            kw = []
        txt = row["text_content"]
        if txt == "null":
            txt = None
        return {
            "seq": row["seq"],
            "filename": row["filename"],
            "ext": row["ext"],
            "path": os.path.join(img_dir, row["filename"]),
            "description": row["description"],
            "text_content": txt,
            "mood": row["mood"],
            "use_case": row["use_case"],
            "style": row["style"],
            "keywords": kw,
            "is_real": bool(row["is_real"]),
            "frames": row["frames"],
            "filesize": row["filesize"],
        }

    def get(self, seq):
        """按序号精确取一张。返回 dict 或 None。"""
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM emojis WHERE seq=?",
                               (int(seq),)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return self._row_to_emoji(row, self.img_dir)

    def search(self, query, exclude_real=True, limit=5):
        """关键词模糊匹配（LIKE），返回候选列表（按 seq 升序）。多词 AND。"""
        words = [w for w in (query or "").split() if w]
        if not words:
            return []
        conds, params = [], []
        if exclude_real:
            conds.append("is_real=0")
        for w in words:
            conds.append("(" + " OR ".join(f"{c} LIKE ?"
                                           for c in _QUERY_COLS) + ")")
            params.extend([f"%{w}%"] * len(_QUERY_COLS))
        where = " AND ".join(conds)
        conn = self._conn()
        try:
            rows = conn.execute(
                f"SELECT * FROM emojis WHERE {where} "
                f"ORDER BY seq LIMIT ?", (*params, int(limit))).fetchall()
        finally:
            conn.close()
        return [self._row_to_emoji(r, self.img_dir) for r in rows]

    def _search_text_exact(self, query, exclude_real=True, limit=3):
        """图上文字（text_content）精确匹配（文字表情包，斗图最贴切）。"""
        q = (query or "").strip()
        if not q:
            return []
        cond = "text_content=?"
        params = [q]
        if exclude_real:
            cond += " AND is_real=0"
        conn = self._conn()
        try:
            rows = conn.execute(
                f"SELECT * FROM emojis WHERE {cond} "
                f"ORDER BY seq LIMIT ?", (*params, int(limit))).fetchall()
        finally:
            conn.close()
        return [self._row_to_emoji(r, self.img_dir) for r in rows]

    def search_semantic(self, query, exclude_real=True, limit=10):
        """模糊匹配 + 词义匹配。

        每个 query 词扩展同义词组（组内 OR，任一命中即算），词与词之间 AND。
        返回去重后的候选（文字精确命中优先，其次词义匹配），按 seq 升序。
        """
        q = (query or "").strip()
        if not q:
            return []
        results, seen = [], set()
        # 1. 文字精确命中优先（图上文字 == query 原词）
        for emo in self._search_text_exact(q, exclude_real=exclude_real):
            if emo["seq"] not in seen:
                seen.add(emo["seq"])
                results.append(emo)
        # 2. 词义扩展模糊匹配
        groups = _expand_query(q)
        if groups:
            conds, params = [], []
            if exclude_real:
                conds.append("is_real=0")
            for group in groups:
                terms = []
                for w in group:
                    for c in _QUERY_COLS:
                        terms.append(f"{c} LIKE ?")
                        params.append(f"%{w}%")
                conds.append("(" + " OR ".join(terms) + ")")
            where = " AND ".join(conds)
            conn = self._conn()
            try:
                rows = conn.execute(
                    f"SELECT * FROM emojis WHERE {where} "
                    f"ORDER BY seq LIMIT ?",
                    (*params, int(limit) * 4)).fetchall()
            finally:
                conn.close()
            for r in rows:
                emo = self._row_to_emoji(r, self.img_dir)
                if emo["seq"] not in seen:
                    seen.add(emo["seq"])
                    results.append(emo)
        return results[:limit]

    def resolve(self, query=None, seq=None, exclude_real=True):
        """选一张表情返回 dict（含绝对路径），找不到返回 None。

        seq 精确指定 > 文字精确 > 词义匹配。只返回文件真实存在的结果。
        """
        if seq is not None:
            emo = self.get(seq)
            if emo and os.path.isfile(emo["path"]):
                return emo
            return None
        q = (query or "").strip()
        if not q:
            return None
        for emo in self.search_semantic(q, exclude_real=exclude_real, limit=10):
            if os.path.isfile(emo["path"]):
                return emo
        return None


def format_candidates(emojis, query="", limit=10):
    """候选列表 → 回灌给 LLM 的文本（决策层 emoji 工具用）。"""
    if not emojis:
        return f"表情库中没有匹配「{query}」的表情"
    lines = [f"emoji「{query}」找到 {len(emojis)} 个候选（用 seq 精确发送）："]
    for e in emojis[:limit]:
        txt = e.get("text_content") or "（无文字）"
        mood = e.get("mood") or "（无情绪标注）"
        anim = "动图" if (e.get("ext") == ".gif") else "静态"
        desc = (e.get("description") or "").replace("\n", " ")[:40]
        lines.append(f"  seq={e['seq']} | 文字:{txt} | 情绪:{mood} | {anim} | {desc}")
    return "\n".join(lines)
