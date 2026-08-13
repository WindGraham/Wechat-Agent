# -*- coding: utf-8 -*-
"""gateway/api/emojis.py — 表情包搜索 API（优化版）"""

import json, sqlite3, os
from flask import Blueprint, jsonify, request, send_file

def create_bp(ctx):
    bp = Blueprint("emojis", __name__, url_prefix="/api/emojis")
    root = ctx["root"]
    db_path = os.path.join(root, "assets", "emojis", "index.db")
    img_dir = os.path.join(root, "assets", "emojis", "renamed")

    def _conn():
        if not os.path.exists(db_path): return None
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")  # 读优化
        return c

    @bp.route("/stats")
    def stats():
        conn = _conn()
        if conn is None:
            return jsonify({"ok": True, "total": 0, "with_text": 0})
        try:
            # MAX(seq) 在删档后会虚高，改用 COUNT(*)；with_text 按 text_content
            # 非空且非 "null" 占位串统计（与 /list 的 "null"→None 归一一致）
            total = conn.execute("SELECT COUNT(*) FROM emojis").fetchone()[0]
            with_text = conn.execute(
                "SELECT COUNT(*) FROM emojis "
                "WHERE text_content IS NOT NULL AND text_content != 'null'"
            ).fetchone()[0]
            return jsonify({"ok": True, "total": total,
                            "with_text": with_text})
        finally:
            conn.close()

    @bp.route("/list")
    def list_emojis():
        conn = _conn()
        if conn is None: return jsonify({"ok":True,"items":[],"total":0})
        try:
            page = max(1, request.args.get("page",1,type=int))
            pp = min(100, max(10, request.args.get("per_page",30,type=int)))
            q = request.args.get("q","").strip()
            to = request.args.get("text_only","0")=="1"
            sq = request.args.get("seq",type=int)

            w, p = "1=1", []
            if sq: w, p = "seq=?", [sq]
            elif q:
                # 按空格拆分，多个词 AND 逻辑
                words = [w for w in q.split() if w]
                if to:
                    for word in words:
                        w += " AND text_content LIKE ?"
                        p.append(f"%{word}%")
                else:
                    for word in words:
                        w += (" AND (description LIKE ? OR text_content LIKE ? "
                              "OR style LIKE ? OR mood LIKE ? "
                              "OR use_case LIKE ? OR keywords LIKE ?)")
                        p.extend([f"%{word}%"]*6)

            # 用 LIMIT+1 判断有无下一页，避免 COUNT(*)（慢）
            off = (page-1)*pp
            rows = conn.execute(
                "SELECT seq,filename,ext,description,text_content,"
                "is_real,style,mood,use_case,keywords,frames,filesize "
                "FROM emojis WHERE "+w+
                " ORDER BY seq LIMIT ? OFFSET ?",
                p+[pp+1, off]).fetchall()

            has_more = len(rows) > pp
            items = []
            for r in rows[:pp]:
                kw = json.loads(r["keywords"]) if r["keywords"] else []
                txt = r["text_content"]
                if txt=="null": txt=None
                items.append({
                    "seq":r["seq"],"filename":r["filename"],"ext":r["ext"],
                    "description":r["description"],"text":txt,
                    "is_real":bool(r["is_real"]),
                    "style":r["style"],"mood":r["mood"],
                    "use_case":r["use_case"],"keywords":kw[:10],
                    "frames":r["frames"],"filesize":r["filesize"]
                })

            return jsonify({
                "ok":True,"items":items,"page":page,"per_page":pp,
                "has_more":has_more,
            })
        finally: conn.close()

    @bp.route("/image/<int:seq>")
    def image(seq):
        conn = _conn()
        if conn is None: return "not found",404
        try:
            row = conn.execute("SELECT filename FROM emojis WHERE seq=?",(seq,)).fetchone()
            if not row: return "not found",404
            fn = row["filename"]
        finally: conn.close()
        path = os.path.join(img_dir,fn)
        if not os.path.exists(path): return "not found",404
        ext = os.path.splitext(fn)[1].lower()
        mt = {".gif":"image/gif",".png":"image/png",".jpg":"image/jpeg",
              ".jpeg":"image/jpeg",".webp":"image/webp"}
        return send_file(path, mimetype=mt.get(ext,"image/png"))
    return bp
