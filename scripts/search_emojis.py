#!/usr/bin/env python3
"""表情包搜索 — 序号 ↔ 内容 双向检索"""
import argparse, json, sqlite3, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_DB = PROJECT_ROOT / "assets" / "emojis" / "index.db"

SELECT_COLS = ("seq, filename, ext, original_md5, description, text_content, "
               "is_real, style, mood, use_case, keywords, frames, filesize")

def _fmt(row):
    seq, fn, ext, md5, desc, txt, is_real, style, mood, case, kw, frames, fsize = row
    tags = json.loads(kw) if kw else []
    lines = [f"\n{'='*56}",
             f"  序号: {seq:06d}  |  文件: {fn}  |  格式: {ext}"]
    if is_real: lines.append(f"  ⚠ 真人照片")
    lines.append(f"  描述: {desc}")
    if txt and txt != "null": lines.append(f"  文字: {txt}")
    lines.extend([f"  风格: {style}  |  情绪: {mood}",
                  f"  场景: {case}",
                  f"  标签: {', '.join(tags[:8])}",
                  f"  帧数: {frames}  |  大小: {fsize/1024:.0f}KB"])
    return "\n".join(lines)

def search_by_seq(conn, seq):
    row = conn.execute(f"SELECT {SELECT_COLS} FROM emojis WHERE seq=?", (seq,)).fetchone()
    if row: print(_fmt(row))
    else: print(f"序号 {seq:06d} 不存在")

def search_by_text(conn, query, text_only=False, style=False, limit=10):
    conditions, params = [], []
    if text_only:
        conditions.append("text_content LIKE ?"); params.append(f"%{query}%")
    elif style:
        conditions.append("style LIKE ?"); params.append(f"%{query}%")
    elif not style:
        words = [w for w in query.split() if w]
        for word in words:
            conditions.append("(description LIKE ? OR text_content LIKE ? OR style LIKE ? OR mood LIKE ? OR use_case LIKE ? OR keywords LIKE ?)")
            params.extend([f"%{word}%"]*6)
    where = " AND ".join(conditions) if conditions else "1=1"
    rows = conn.execute(f"SELECT {SELECT_COLS} FROM emojis WHERE {where} ORDER BY seq LIMIT ?", (*params, limit)).fetchall()
    if not rows: print(f"未找到: {query}"); return
    print(f"找到 {len(rows)} 个: {query}\n")
    for r in rows:
        tags = json.loads(r[9]) if r[9] else []
        real = " [真人]" if r[6] else ""
        txt = f" [{r[5]}]" if r[5] and r[5]!="null" else ""
        print(f"  {r[0]:06d}  {r[1]}{real}  {(r[4] or '')[:55]}{txt}  |  {', '.join(tags[:5])}")

def random_sample(conn, n):
    rows = conn.execute(f"SELECT {SELECT_COLS} FROM emojis ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()
    print(f"随机 {n} 张:\n")
    for r in rows: print(_fmt(r))

def main():
    p = argparse.ArgumentParser(description="表情包搜索")
    p.add_argument("query", nargs="?", help="序号/关键词")
    p.add_argument("--text", action="store_true", help="仅搜文字")
    p.add_argument("--style", action="store_true", help="按风格")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--random", type=int, default=0)
    p.add_argument("--db", type=str)
    args = p.parse_args()
    db_path = Path(args.db) if args.db else INDEX_DB
    if not db_path.exists():
        print(f"DB 不存在: {db_path}"); sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    if args.random > 0: random_sample(conn, args.random)
    elif args.query is None:
        total = conn.execute("SELECT COUNT(*) FROM emojis").fetchone()[0]
        real = conn.execute("SELECT COUNT(*) FROM emojis WHERE is_real=1").fetchone()[0]
        print(f"总数: {total}  |  真人: {real}\n")
        print("用法: python search_emojis.py 001234")
        print("      python search_emojis.py 猫 无语")
        print("      python search_emojis.py --text 好的")
        print("      python search_emojis.py --random 10")
    elif args.query.isdigit(): search_by_seq(conn, int(args.query))
    else: search_by_text(conn, args.query, args.text, args.style, args.limit)
    conn.close()

if __name__ == "__main__": main()
