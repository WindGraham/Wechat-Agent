#!/usr/bin/env python3
"""
表情包标注脚本
- 10 并发 Gemini 2.5 Flash Lite
- GIF: 单帧直接送，多帧取 25%+75% 帧联合分析
- 重命名: 000001.ext 递增，不分格式
- 索引: SQLite + FTS5，支持双向搜索
- 断点续跑: progress.jsonl

用法:
    python scripts/label_emojis.py              # 全量
    python scripts/label_emojis.py --test 10    # 冒烟10张
    python scripts/label_emojis.py --dry-run    # 预览
"""

import argparse, asyncio, base64, io, json, os, re, sqlite3, time
from pathlib import Path

import aiohttp
from PIL import Image

# ─── 配置 ────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR      = PROJECT_ROOT / "assets" / "emojis"
RENAMED_DIR  = SRC_DIR / "renamed"
INDEX_DB     = SRC_DIR / "index.db"
PROGRESS     = SRC_DIR / "progress.jsonl"

API_URL      = "https://api.aixhan.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
API_KEY      = os.environ.get("AIXHAN_API_KEY", "")
CONCURRENCY  = 500
MAX_RETRIES  = 3
RETRY_DELAY  = 2.0
IMAGE_EXTS   = {".gif", ".png", ".jpg", ".jpeg", ".webp"}

PROMPT = (
    '输出JSON，描述这张表情包。'
    '如果图上有文字，把文字原样填入text字段，没有则填null。'
    '"is_real"填true或false：这是一张真人照片（非绘画/卡通/3D）吗？'
    '"keywords"标签需包含：画风、人物基础动作。'
    'JSON格式：{"description":"内容描述","text":"文字或null",'
    '"is_real":true/false,"style":"风格","mood":"情绪",'
    '"use_case":"适合场景","keywords":["标签1","标签2"]}。'
    '只输出JSON，不要其他内容。'
)

# ─── 数据库 ──────────────────────────────────────────
def init_db(path):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS emojis (
            seq         INTEGER PRIMARY KEY,
            filename    TEXT NOT NULL,
            ext         TEXT NOT NULL,
            original_md5 TEXT NOT NULL UNIQUE,
            description TEXT,
            text_content TEXT,
            is_real     INTEGER DEFAULT 0,
            style       TEXT,
            mood        TEXT,
            use_case    TEXT,
            keywords    TEXT,
            frames      INTEGER DEFAULT 1,
            filesize    INTEGER,
            processed_at TEXT
        );

    """)
    conn.commit()
    return conn

def insert_result(conn, seq, filename, ext, md5, result, frames, filesize):
    kw = json.dumps(result.get("keywords", []), ensure_ascii=False)
    conn.execute("""
        INSERT OR REPLACE INTO emojis
        (seq, filename, ext, original_md5, description, text_content,
         is_real, style, mood, use_case, keywords, frames, filesize, processed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
    """, (seq, filename, ext, md5,
          result.get("description", ""), result.get("text"),
          1 if result.get("is_real") else 0,
          result.get("style", ""), result.get("mood", ""),
          result.get("use_case", ""), kw, frames, filesize))
    conn.commit()

def load_progress(path):
    if not path.exists(): return set()
    return {line.strip() for line in open(path) if line.strip()}

def save_progress(path, md5):
    with open(path, "a") as f: f.write(md5 + "\n")

def current_max_seq(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COALESCE(MAX(seq),0) FROM emojis").fetchone()[0]
    finally: conn.close()

# ─── GIF 帧提取 ─────────────────────────────────────
def gif_info(img_path):
    """返回 (总帧数, [待发送的帧列表])。损坏的 GIF 当作单帧静态图。"""
    try:
        img = Image.open(img_path)
        total = 0
        try:
            while True: img.seek(total); total += 1
        except EOFError: pass
        except Exception: total = 1  # 损坏的 GIF 当单帧
        if total <= 1:
            img.seek(0)
            return 1, [img.copy()]
        q1 = int(total * 0.25)
        q3 = min(total - 1, int(total * 0.75))
        img.seek(q1); f1 = img.copy()
        img.seek(q3); f2 = img.copy()
        return total, [f1, f2]
    except Exception:
        # 完全无法打开的图，返回假数据
        return 1, []

def frame_to_b64(frame):
    buf = io.BytesIO()
    frame.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

# ─── Gemini API ─────────────────────────────────────
def parse_json(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"): text = text[:-3]
    if text.startswith("json"): text = text[4:]
    text = text.strip()
    try: return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try: return json.loads(m.group())
            except json.JSONDecodeError: pass
    print(f"  JSON解析失败: {raw[:200]}")
    return None

async def gemini(parts):
    headers = {"x-goog-api-key": API_KEY}
    payload = {"contents": [{"parts": parts}]}
    for attempt in range(MAX_RETRIES):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_URL, json=payload, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 200:
                        data = await r.json()
                        cand = data.get("candidates", [])
                        if cand:
                            txt = "".join(p.get("text","") for p in
                                          cand[0].get("content",{}).get("parts",[]))
                            return parse_json(txt)
                    elif r.status == 429:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue
                    else:
                        body = await r.text()
                        print(f"  API {r.status}: {body[:150]}")
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        except Exception as e:
            print(f"  网络错(尝试{attempt+1}): {e}")
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    return None

# ─── 单张处理 ───────────────────────────────────────
async def process_one(img_path, md5):
    ext = img_path.suffix.lower()
    mime_map = {".gif":"image/gif",".png":"image/png",".jpg":"image/jpeg",
                 ".jpeg":"image/jpeg",".webp":"image/webp"}
    mime = mime_map.get(ext, "image/png")

    if ext == ".gif":
        total_f, frames = gif_info(img_path)
        result = {"frames": total_f}
        if len(frames) == 0:
            # 损坏的 GIF，跳过
            return None
        elif len(frames) == 1:
            parts = [
                {"text": PROMPT},
                {"inlineData": {"mimeType":"image/png","data":frame_to_b64(frames[0])}},
            ]
        else:
            q1_idx = 1 + int(total_f * 0.25)
            q3_idx = 1 + min(total_f - 1, int(total_f * 0.75))
            prompt = (
                f"这是一张GIF表情包的第{q1_idx}帧(前段)和第{q3_idx}帧(后段)，"
                f"共{total_f}帧。请综合分析两帧，注意动效变化。\n{PROMPT}"
            )
            parts = [
                {"text": prompt},
                {"inlineData": {"mimeType":"image/png","data":frame_to_b64(frames[0])}},
                {"inlineData": {"mimeType":"image/png","data":frame_to_b64(frames[1])}},
            ]
    else:
        with open(img_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        result = {"frames": 1}
        parts = [
            {"text": PROMPT},
            {"inlineData": {"mimeType": mime, "data": b64}},
        ]

    ai = await gemini(parts)
    if ai is None:
        return None
    result.update(ai)
    result["filesize"] = img_path.stat().st_size
    return result

# ─── 重命名 ─────────────────────────────────────────
def rename_file(src, seq, ext):
    dst = RENAMED_DIR / f"{seq:06d}{ext}"
    try:
        os.link(src, dst)     # 硬链接，不占额外磁盘空间
    except OSError:
        import shutil
        shutil.copy2(src, dst)
    return dst

# ─── 主流程 ─────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="表情包标注")
    parser.add_argument("--test", type=int, default=0, help="冒烟N张")
    parser.add_argument("--dry-run", action="store_true", help="预览")
    args = parser.parse_args()

    RENAMED_DIR.mkdir(parents=True, exist_ok=True)
    conn = init_db(INDEX_DB)

    done = load_progress(PROGRESS)

    # 扫描待处理文件
    all_files = [
        (p, p.stem) for p in sorted(SRC_DIR.iterdir())
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ]
    pending = [(p, md5) for p, md5 in all_files if md5 not in done]

    if args.test > 0:
        pending = pending[:args.test]

    n = len(pending)
    if n == 0:
        print("全部完成！")
        conn.close(); return

    start_seq = current_max_seq(INDEX_DB)
    print(f"待处理: {n} | 起始序号: {start_seq+1:06d} | 并发: {CONCURRENCY}")
    print(f"重命名目录: {RENAMED_DIR}")
    print(f"索引文件:   {INDEX_DB}")

    if args.dry_run:
        print(f"\n[dry-run] 将处理 {n} 张，不执行。")
        conn.close(); return

    seq_counter = start_seq
    done_count = 0
    fail_count = 0
    t0 = time.time()
    results_lock = asyncio.Lock()

    async def worker(path, md5):
        nonlocal seq_counter, done_count, fail_count
        result = await process_one(path, md5)

        async with results_lock:
            nonlocal seq_counter, done_count, fail_count
            if result is None:
                fail_count += 1
                print(f"  [失败 {fail_count}] {md5}")
                return

            seq_counter += 1
            ext = path.suffix.lower()
            rename_file(path, seq_counter, ext)
            await asyncio.get_running_loop().run_in_executor(
                None, insert_result, conn,
                seq_counter, f"{seq_counter:06d}{ext}", ext, md5,
                result, result.get("frames", 1), result.get("filesize", 0),
            )
            save_progress(PROGRESS, md5)
            done_count += 1

            elapsed = time.time() - t0
            rate = done_count / elapsed if elapsed > 0 else 0
            eta = (n - done_count - fail_count) / rate if rate > 0 else 0
            desc = result.get("description", "")[:45]
            print(f"  [{done_count}/{n}] {seq_counter:06d}{ext} "
                  f"| {rate*60:.0f}/min | ETA {eta/60:.0f}min | {desc}")

    # 高并发：全部任务一起发，Semaphore 控制并发上限
    sem = asyncio.Semaphore(CONCURRENCY)
    async def bounded_worker(path, md5):
        async with sem:
            return await worker(path, md5)

    tasks = [bounded_worker(path, md5) for path, md5 in pending]
    await asyncio.gather(*tasks)

    conn.close()
    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"完成! 成功: {done_count} | 失败: {fail_count} | 耗时: {elapsed/60:.1f}min")
    print(f"序号: {start_seq+1:06d} ~ {seq_counter:06d}")
    print(f"速度: {done_count/elapsed*60:.0f} 张/分")

if __name__ == "__main__":
    asyncio.run(main())
