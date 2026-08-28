#!/usr/bin/env python3
"""msg_log.py - 消息日志库（方向2 §1/§3.3 离线部分）

SQLite 主库（严谨性：事务/对齐/幂等）+ 文本 log 导出视图（库->文本单向生成）。
与 v1 session_manager/messages.db 完全分离，不回写。

四层职责：
  1. schema：sessions / messages / backfill_runs / frames 四张表（§1.2）
  2. 归一化与对齐键：normalize / fuzzy_eq / align_key（§1.4，L1+L2）
  3. 锚定合并：find_overlap（L3 序列锚定，连续>=3 条保序命中）+ merge_stack
     （单事务写入，绝不静默错拼；空间不足时对本会话 seq<anchor 段 REBASE）；
     日常增量续写走 append_incremental（session_tail+fuzzy_eq 分界续写，
     merge_stack 只做 backfill 前插和空日志首合并）
  4. 文本 log 导出：export_text_log（§1.3 格式，每次合并后全量重写）

merge_stack 消费的"栈条目"是 duck-typed 对象（frame_align.Entry），需要属性：
  kind('msg'/'divider') sender content content_type is_mine mentions
  ocr_conf complete partial_top partial_bottom
"""

import difflib
import hashlib
import os
import re
import sqlite3
import threading
import time
import unicodedata

# ---------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    name_full    TEXT,
    is_group     INTEGER NOT NULL,
    earliest_seq INTEGER,
    latest_seq   INTEGER,
    top_reached  INTEGER DEFAULT 0,
    sync_version INTEGER DEFAULT 0,
    roster_status INTEGER DEFAULT 0,   -- 花名册/身份信息获取状态：0=未获取 1=已获取 2=失败
    created_ts   REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(session_id),
    seq          INTEGER NOT NULL,
    ts_hint      REAL,
    ts_text      TEXT,
    ts_captured  REAL NOT NULL,
    sender       TEXT NOT NULL,
    is_mine      INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_norm TEXT NOT NULL,
    complete     INTEGER NOT NULL,        -- 0=截断实例 1=完整 2=跨屏拼接
    ocr_conf     REAL,
    source       TEXT NOT NULL,           -- backfill / incremental / self_send
    align_key    TEXT NOT NULL,
    msg_uid      TEXT NOT NULL UNIQUE,
    mentions     TEXT DEFAULT '',
    media_path   TEXT DEFAULT '',         -- 多媒体裁图归档路径（CONTRACTS §一）
    crop_path    TEXT DEFAULT '',         -- 本条消息在实时采集时的裁图（workspace/crops 相对路径）
    frame_phash  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_session_seq ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_msg_align ON messages(session_id, align_key);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(session_id, ts_hint);

CREATE TABLE IF NOT EXISTS backfill_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    state       TEXT NOT NULL,            -- running/aligned/merging/done/aborted
    started_ts  REAL, last_frame_ts REAL,
    frame_count INTEGER DEFAULT 0,
    stack_head_key TEXT, stack_tail_key TEXT
);

CREATE TABLE IF NOT EXISTS frames (
    frame_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES backfill_runs(run_id),
    frame_idx   INTEGER NOT NULL,
    captured_ts REAL NOT NULL,
    elements_json TEXT NOT NULL,
    dy_from_prev INTEGER,
    align_status TEXT DEFAULT 'pending'   -- pending/ok/suspect/motion_blur
);
"""


class MergeError(Exception):
    """栈与日志无重叠且未到顶端：拒绝合并（宁可失败不可错拼）"""


# 跨线程共享连接：主线程（journey 写入）与 Proxy 线程（决策读取/写回）
# 共用同一连接——check_same_thread=False + 全模块 RLock 串行化
_DB_LOCK = threading.RLock()


def _locked(fn):
    import functools
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with _DB_LOCK:
            return fn(*a, **kw)
    return wrapper


def connect(db_path):
    """打开/创建日志库。项目盘是 exfat：journal_mode 必须用 DELETE。"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# 老库兼容：schema 后加列时，对已存在的 db 文件做 ALTER TABLE 补列
_MIGRATIONS = (
    ("messages", "media_path", "ALTER TABLE messages ADD COLUMN media_path TEXT DEFAULT ''"),
    ("messages", "crop_path", "ALTER TABLE messages ADD COLUMN crop_path TEXT DEFAULT ''"),
    ("messages", "media_status", "ALTER TABLE messages ADD COLUMN media_status TEXT DEFAULT ''"),
    ("sessions", "roster_status", "ALTER TABLE sessions ADD COLUMN roster_status INTEGER DEFAULT 0"),
)


def _migrate(conn):
    cols_cache = {}
    for table, col, ddl in _MIGRATIONS:
        if table not in cols_cache:
            cols_cache[table] = {r["name"] for r in
                                 conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols_cache[table]:
            conn.execute(ddl)
    conn.commit()


@_locked
def get_or_create_session(conn, name, is_group, name_full=None):
    """按名字取会话，不存在才创建（is_group 只在新建时写入）。

    已存在会话**绝不覆写 is_group**——读取路径（get_context/get_new_since）
    拿不到真实 is_group，覆写会把群聊改成私聊（B4 数据破坏）。
    旅程实测到真实值时用 set_session_kind 显式更新。"""
    row = conn.execute("SELECT session_id FROM sessions WHERE name=?",
                       (name,)).fetchone()
    if row:
        return row["session_id"]
    cur = conn.execute(
        "INSERT INTO sessions(name, name_full, is_group, created_ts)"
        " VALUES(?,?,?,?)", (name, name_full, 1 if is_group else 0, time.time()))
    conn.commit()
    return cur.lastrowid


@_locked
def set_session_kind(conn, name, is_group):
    """旅程实测到真实 is_group 后显式更新（get_or_create_session 不覆写）。
    值无变化时不写库。返回是否发生了更新。"""
    row = conn.execute("SELECT session_id, is_group FROM sessions WHERE name=?",
                       (name,)).fetchone()
    if not row:
        return False
    val = 1 if is_group else 0
    if row["is_group"] == val:
        return False
    conn.execute("UPDATE sessions SET is_group=? WHERE session_id=?",
                 (val, row["session_id"]))
    conn.commit()
    return True


@_locked
def get_session_kind(conn, name):
    """查询会话 is_group（读路径补 Message.is_group 用）。未知会话返回 False。"""
    row = conn.execute("SELECT is_group FROM sessions WHERE name=?",
                       (name,)).fetchone()
    return bool(row["is_group"]) if row else False


# ---------------------------------------------------------------- 花名册状态
# 花名册/身份信息获取状态（理想态：每个会话只爬一次，凌晨3点扫未获取的）。
ROSTER_PENDING = 0    # 未获取
ROSTER_DONE = 1       # 已获取
ROSTER_FAILED = 2     # 获取失败（可重试）


@_locked
def get_roster_status(conn, name):
    """查询会话的花名册获取状态。未知会话返回 None。"""
    row = conn.execute("SELECT roster_status FROM sessions WHERE name=?",
                       (name,)).fetchone()
    return row["roster_status"] if row else None


@_locked
def set_roster_status(conn, name, status):
    """设置会话的花名册获取状态。返回是否更新成功。"""
    cur = conn.execute("UPDATE sessions SET roster_status=? WHERE name=?",
                       (int(status), name))
    conn.commit()
    return cur.rowcount > 0


@_locked
def list_sessions_needing_roster(conn):
    """列出所有「信息尚未获取」的会话（含群聊+私信），供凌晨3点休眠后扫描。

    返回 [{"name":..., "is_group":...}, ...]，按 name 排序稳定。
    仅含 roster_status=0（未获取）与 =2（失败待重试）的会话。
    """
    rows = conn.execute(
        "SELECT name, is_group FROM sessions "
        "WHERE roster_status != ? ORDER BY name",
        (ROSTER_DONE,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 归一化与对齐键
# OCR 易混字符 fold（归一化后才做相等/相似度比较，两边一致 fold 不影响语义）
_FOLD = str.maketrans({
    "O": "0", "o": "0",
    "l": "1", "I": "1", "|": "1",
    "S": "5", "s": "5",
    "B": "8",
    "，": ",", "、": ",", "。": ".",
    "：": ":", "；": ";",
    "！": "!", "？": "?",
    "“": '"', "”": '"', "‘": "'", "’": "'",
})
_EDGE_PUNCT = ".,:;!?'\"~-_/\\"


def normalize(text):
    """对齐/去重键用归一化：
    全角->半角(NFKC)、去全部空白、OCR 易混字符 fold（在 lower 之前，
    大写 I/O/S 才 fold，小写 i 保留以免误伤英文单词）、小写、去边缘孤立标点。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"\s+", "", t)
    t = t.translate(_FOLD).lower()
    return t.strip(_EDGE_PUNCT)


def align_key(sender, content):
    """L1 精确键：sha1(sender_norm|content_norm)[:16]"""
    raw = f"{normalize(sender)}|{normalize(content)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@_locked
def update_content(conn, session_id, sender, content, new_content):
    """按 sender+content 模糊匹配更新最近一条日志的内容（多媒体标注写回用）。
    返回更新的行数（0/1）。只查最近 20 条，防止误改老消息。"""
    rows = conn.execute(
        "SELECT id, sender, content FROM messages"
        " WHERE session_id=? ORDER BY seq DESC LIMIT 20",
        (session_id,)).fetchall()
    target = None
    for r in rows:
        if fuzzy_eq(sender, content, r["sender"], r["content"]):
            target = r["id"]
            break
    if target is None:
        return 0
    conn.execute("UPDATE messages SET content=?, content_norm=? WHERE id=?",
                 (new_content, normalize(new_content), target))
    conn.commit()
    return 1


@_locked
def update_media(conn, msg_id, content=None, media_path=None,
                 media_status=None, content_type=None):
    """按 id 精确更新媒体字段（媒体处置 pass 写回用）。

    占位符 "[图片]"/"[链接]" 在 update_content 的模糊匹配里会撞车，
    媒体 pass 持有 DB 行 id，直接按 id 更新。只更新非 None 的字段；
    content 更新时同步 content_norm。返回更新行数（0/1）。"""
    sets, vals = [], []
    if content is not None:
        sets += ["content=?", "content_norm=?"]
        vals += [content, normalize(content)]
    if media_path is not None:
        sets.append("media_path=?")
        vals.append(media_path)
    if media_status is not None:
        sets.append("media_status=?")
        vals.append(media_status)
    if content_type is not None:
        sets.append("content_type=?")
        vals.append(content_type)
    if not sets:
        return 0
    vals.append(msg_id)
    cur = conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?",
                       vals)
    conn.commit()
    return cur.rowcount


def _ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def fuzzy_eq(sender_a, content_a, sender_b, content_b, iou=None):
    """L2 模糊匹配（§1.4）：sender_norm 相同，且
    - 内容归一化后相等 -> True
    - 短消息（norm 长度<=6）必须完全相等；调用方若提供几何 IoU 则要求 IoU>0.4
    - 长消息 SequenceMatcher ratio >= 0.85

    多媒体标注容忍（2026-08-08）：标注写回会在 content 尾部追加
    "[多媒体消息…]描述"（或旧格式"\\n内容：…"），屏幕再读到的原文没有
    这段——比较前先剥掉标注后缀，否则已标注消息永远锚不上（反复 gap）。
    """
    if normalize(sender_a) != normalize(sender_b):
        return False
    na, nb = normalize(_strip_annotation(content_a)), \
        normalize(_strip_annotation(content_b))
    if not na or not nb:
        # 纯标点消息（"?" / "..."）normalize 后为空串：退化为原文精确比较，
        # 否则 "?" 永远无法匹配，增量补尾会把整段底部误判为新消息（218 实测）
        return na == nb and content_a.strip() == content_b.strip()
    if na == nb:
        if min(len(na), len(nb)) <= 6 and iou is not None and iou <= 0.4:
            return False
        return True
    if min(len(na), len(nb)) <= 6:
        return False
    return _ratio(na, nb) >= 0.85


def _strip_annotation(content):
    """剥掉多媒体标注后缀（新旧两种格式）。"""
    if not content:
        return ""
    for mark in ("[多媒体消息", "\n内容:"):
        i = content.find(mark)
        if i >= 0:
            content = content[:i]
    return content


# ---------------------------------------------------------------- 时间分割线
_HM_RE = re.compile(r"(\d{1,2})[:：](\d{2})")


def parse_ts_hint(ts_text, captured_ts):
    """把 '昨天 23:38' / '00:03' / '上午 9:12' / '8月1日 22:00' 之类分割线文本
    相对采集时间折算成 epoch（§9-1：无分割线的消息不伪造时间，返回 None）。"""
    if not ts_text or not captured_ts:
        return None
    lt = time.localtime(captured_ts)
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    t = re.sub(r"\s+", "", ts_text)
    m = _HM_RE.search(t)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if "下午" in t and hh < 12:
        hh += 12
    elif "晚上" in t and hh < 12:
        hh += 12
    elif "中午" in t and hh < 12:
        hh += 12
    day_off = 0
    if "昨天" in t:
        day_off = -1
    elif "前天" in t:
        day_off = -2
    else:
        mw = re.search(r"星期([一二三四五六日天])", t)
        if mw:
            wd = "一二三四五六日天".index(mw.group(1))  # 周一=0
            cur_wd = lt.tm_wday
            day_off = -((cur_wd - wd) % 7 or 7)
        else:
            md = re.search(r"(\d{1,2})月(\d{1,2})日", t)
            if md:
                mon, day = int(md.group(1)), int(md.group(2))
                year = lt.tm_year
                try:
                    base = time.mktime((year, mon, day, hh, mm, 0, 0, 0, -1))
                    if base > captured_ts:  # 跨年：日期在未来则取去年
                        base = time.mktime((year - 1, mon, day, hh, mm, 0,
                                            0, 0, -1))
                    return base
                except (ValueError, OverflowError):
                    return None
    return day0 + day_off * 86400 + hh * 3600 + mm * 60


# ---------------------------------------------------------------- L3 序列锚定
ANCHOR_MIN_HITS = 3       # 连续保序命中 >=3 才认定重叠
ANCHOR_FALLBACK_HITS = 2  # 放宽档：命中 2 条且其中至少 1 条 norm 长度 > 10
ANCHOR_FALLBACK_MINLEN = 10
STACK_ANCHOR_KEYS = 12    # 取栈底（最新端）12 条参与锚定
LOG_TAIL_N = 50           # 日志末尾参与锚定的条数


@_locked
def session_tail(conn, session_id, n=LOG_TAIL_N):
    """日志末尾 n 条，按 seq 升序（最新在尾）。mentions 一并取出（@我标注用）。"""
    rows = conn.execute(
        "SELECT seq, sender, content, content_type, mentions, media_path FROM messages"
        " WHERE session_id=? ORDER BY seq DESC LIMIT ?",
        (session_id, n)).fetchall()
    return [dict(r) for r in reversed(rows)]


MEDIA_TYPES = {"multimedia", "image", "sticker", "voice", "video",
               "unknown_nontext"}


def _media_eq(entry, row):
    """多媒体条目锚定（2026-08-08 YOUSAOBI gap 风暴修复）：媒体转换把日志行
    content 改写成视觉描述后，屏幕重读到的占位符/缩略图 OCR 文本永远匹配不上，
    增量分界零命中 -> 反复 gap -> backlog 锚不住 -> MergeError。
    两侧都是多媒体且发送人一致即视为同一条（先后顺序由 LCS/分界逻辑保证，
    媒体消息密度低，误锚风险可接受）。"""
    if row["content_type"] == "time_divider":
        return False
    if row["content_type"] not in MEDIA_TYPES and not row["media_path"]:
        return False
    if (getattr(entry, "content_type", "text") or "text") not in MEDIA_TYPES:
        return False
    return normalize(_entry_sender(entry)) == normalize(row["sender"])


def _entry_fuzzy_eq_row(entry, row):
    """栈条目 vs 日志行的 L2 判定。divider 用精确文本等值（分割线是锚定成员，
    但不做唯一键——同一文本可出现多次，靠 L3 保序兜底）。"""
    if getattr(entry, "kind", "msg") == "divider":
        return (row["content_type"] == "time_divider"
                and normalize(entry.content) == normalize(row["content"]))
    if row["content_type"] == "time_divider":
        return False
    if _media_eq(entry, row):
        return True
    return fuzzy_eq(entry.sender, entry.content, row["sender"], row["content"])


def find_overlap(stack_entries, log_tail):
    """L3 序列锚定（§3.3）：栈底 STACK_ANCHOR_KEYS 条与 log_tail 做
    保序 LCS 匹配（允许两侧跳条——浅重叠时栈底键不都在日志里、单侧
    漏识别也不应锚不住）；连续保序命中 >=3（或放宽档 2+长消息）才认定重叠。

    返回 {'stack_idx': 向上扩展后第一个未匹配栈条目下标,
          'log_idx': 对应日志行在 log_tail 中的下标,
          'hits': 命中数} 或 None。
    认定重叠后从首个命中对**向上继续扩展**（栈更上端可能已在日志里，
    如重复合并同一栈），扩展后的分界之前的栈条目才需要插入——
    这是合并幂等性的主要来源（msg_uid 只做最后兜底）。
    """
    keys = stack_entries[-STACK_ANCHOR_KEYS:]
    k0 = len(stack_entries) - len(keys)     # keys[0] 在全栈中的下标
    n, m = len(keys), len(log_tail)
    if not n or not m:
        return None
    match = [[False] * m for _ in range(n)]
    for i in range(n):
        for j in range(m):
            match[i][j] = _entry_fuzzy_eq_row(keys[i], log_tail[j])
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if match[i][j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    hits = dp[0][0]
    # 回溯取命中对
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if match[i][j] and dp[i][j] == dp[i + 1][j + 1] + 1:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    if hits < ANCHOR_FALLBACK_HITS or not pairs:
        return None
    ok = hits >= ANCHOR_MIN_HITS
    if not ok:
        # 放宽档：命中里至少 1 条 norm 长度 > 10（纯短消息群防误锚）
        ok = any(len(normalize(keys[i].content)) > ANCHOR_FALLBACK_MINLEN
                 for i, _ in pairs)
    if not ok:
        return None
    sp, lp = k0 + pairs[0][0], pairs[0][1]
    # 向上扩展：栈首个命中条目之上、日志锚定行之前，成对模糊相等则一并视为已入库
    while sp > 0 and lp > 0 and _entry_fuzzy_eq_row(stack_entries[sp - 1],
                                                    log_tail[lp - 1]):
        sp -= 1
        lp -= 1
        hits += 1
    return {"stack_idx": sp, "log_idx": lp, "hits": hits}


# ---------------------------------------------------------------- 合并事务
def _msg_uid(session_id, seq, akey):
    return hashlib.sha1(f"{session_id}|{seq}|{akey}".encode()).hexdigest()[:24]


def _entry_sender(entry):
    """栈条目的落库 sender：self/is_mine -> '我'，divider -> 'system'。"""
    if getattr(entry, "kind", "msg") == "divider":
        return "system"
    if getattr(entry, "is_mine", False) or getattr(entry, "sender", "") == "self":
        return "我"
    return getattr(entry, "sender", "")


def _insert_rows(conn, session_id, entries, first_seq, source, captured_ts,
                 top_reached=False):
    """按 first_seq.. 连续 seq 插入 entries（INSERT OR IGNORE，msg_uid 幂等）。
    merge_stack 与 append_incremental 共用。返回 (inserted, min_seq, max_seq)。"""
    inserted = 0
    last_divider = None
    min_seq, max_seq = None, None
    for i, e in enumerate(entries):
        seq = first_seq + i
        is_div = getattr(e, "kind", "msg") == "divider"
        if is_div:
            ctype = "time_divider"
            ts_text = e.content
            ts_hint = parse_ts_hint(ts_text, captured_ts)
            last_divider = ts_text
        else:
            ctype = e.content_type
            ts_text = last_divider or getattr(e, "time_hint", None)
            ts_hint = parse_ts_hint(ts_text, captured_ts)
        sender = _entry_sender(e)
        content = e.content
        content_norm = normalize(content)
        akey = align_key(sender, content)
        if getattr(e, "complete", 1) == 2:
            complete = 2
        elif (getattr(e, "partial_top", False)
              or getattr(e, "partial_bottom", False)) and not (
                  top_reached and i == 0):
            complete = 0
        else:
            complete = 1
        mentions = ",".join(getattr(e, "mentions", None) or [])
        media_path = getattr(e, "media_path", None) or ""
        crop_path = getattr(e, "crop_path", None) or ""
        frame_phash = getattr(e, "frame_phash", None)
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages"
            "(session_id, seq, ts_hint, ts_text, ts_captured, sender,"
            " is_mine, content_type, content, content_norm, complete,"
            " ocr_conf, source, align_key, msg_uid, mentions, media_path,"
            " crop_path, frame_phash)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, seq, ts_hint, ts_text, captured_ts, sender,
             1 if getattr(e, "is_mine", False) else 0, ctype, content,
             content_norm, complete, getattr(e, "ocr_conf", None), source,
             akey, _msg_uid(session_id, seq, akey), mentions, media_path,
             crop_path, frame_phash))
        inserted += cur.rowcount
        min_seq = seq if min_seq is None else min(min_seq, seq)
        max_seq = seq if max_seq is None else max(max_seq, seq)
    return inserted, min_seq, max_seq


@_locked
def merge_stack(conn, session_id, entries, source="backfill",
                top_reached=False, captured_ts=None):
    """临时栈 -> 正式日志，单事务（§3.3）。

    entries：全局有序（早->晚）的栈条目（frame_align.Entry 或兼容对象）。
    返回 {'inserted': n, 'anchor': bool, 'rebased': bool}。
    无重叠且日志非空且未到顶端 -> MergeError（绝不静默错拼）。
    """
    captured_ts = captured_ts or time.time()
    entries = [e for e in entries
               if not (e.content_type == "text" and not e.content.strip()
                       and getattr(e, "kind", "msg") == "msg")]
    log_tail = session_tail(conn, session_id)
    anchor = find_overlap(entries, log_tail) if log_tail else None

    conn.execute("BEGIN IMMEDIATE")
    try:
        rebased = False
        if anchor is None:
            if log_tail and not top_reached:
                raise MergeError("栈与日志无重叠且未到顶端，拒绝合并")
            to_insert = entries
            if log_tail:
                # 回填到顶仍无重叠（日志由别的路径建立/历史分叉）：
                # 整段 prepend 到 earliest_seq 之下，而不是从 1 开始
                # 撞上已有 seq 被 INSERT OR IGNORE 静默吞掉
                earliest = conn.execute(
                    "SELECT MIN(seq) s FROM messages WHERE session_id=?",
                    (session_id,)).fetchone()["s"]
                first_seq = earliest - len(to_insert)
            else:
                first_seq = 1
        else:
            sp = anchor["stack_idx"]
            anchor_seq = log_tail[anchor["log_idx"]]["seq"]
            to_insert = entries[:sp]
            n = len(to_insert)
            # REBASE：anchor 之下已有消息且空位不足时，把 seq<anchor_seq 段
            # 整体下移腾出 n 个位置（仅影响本会话，保序）
            max_below = conn.execute(
                "SELECT MAX(seq) s FROM messages WHERE session_id=? AND seq<?",
                (session_id, anchor_seq)).fetchone()["s"]
            if max_below is not None:
                free = (anchor_seq - 1) - max_below
                if free < n:
                    shift = n - free
                    conn.execute(
                        "UPDATE messages SET seq=seq-? "
                        "WHERE session_id=? AND seq<?",
                        (shift, session_id, anchor_seq))
                    rebased = True
            first_seq = anchor_seq - n

        inserted, min_seq, max_seq = _insert_rows(
            conn, session_id, to_insert, first_seq, source, captured_ts,
            top_reached=top_reached)

        # 会话游标：earliest 只往前挪，latest 取全局最大，top_reached 只置位
        if min_seq is not None:
            conn.execute(
                "UPDATE sessions SET"
                " earliest_seq = MIN(COALESCE(earliest_seq, ?), ?),"
                " latest_seq   = MAX(COALESCE(latest_seq, ?), ?),"
                " top_reached  = MAX(top_reached, ?)"
                " WHERE session_id=?",
                (min_seq, min_seq, max_seq, max_seq,
                 1 if top_reached else 0, session_id))
        elif top_reached:
            conn.execute("UPDATE sessions SET top_reached=1 WHERE session_id=?",
                         (session_id,))
        if rebased:  # REBASE 后 earliest_seq 已整体下移，重新取最小值
            conn.execute(
                "UPDATE sessions SET earliest_seq="
                "(SELECT MIN(seq) FROM messages WHERE session_id=?)"
                " WHERE session_id=?", (session_id, session_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"inserted": inserted, "anchor": anchor is not None,
            "rebased": rebased, "attempted": len(to_insert)}


# ---------------------------------------------------------------- 增量 append
# 文本/divider 锚定窗口：只认日志尾最近 N 行。
# 根因（2026-08-10 风图"你好呀"复现）：同一发送人发过内容完全相同的消息时，
# 屏底的新消息会锚到几十行前的旧副本上——split 被推到屏内最后一条，
# new=0 且不报 gap（有锚点），消息被静默吞掉且水位永久停摆
# （交流一下？ seq 2032 卡死同源）。媒体匹配已有 last-3 窗口（下方），
# 文本/divider 同样需要：底部增量读到的一定锚在末尾几行，窗口不影响正常锚定。
TEXT_ANCHOR_WINDOW = 10


def _entry_in_tail(entry, tail):
    """栈条目是否已在日志尾（L2 模糊判定）。divider 用归一化精确等值。

    多媒体匹配只认尾部最后 3 行（2026-08-09 JY君 新表情被吞修复）：
    媒体条目内容无法区分（转换后是描述、未转换是占位符），同发送人的
    新表情包会被误判命中 10 行前的旧媒体行 → 被当"已记录"丢弃。
    文本/divider 匹配只认尾部最后 TEXT_ANCHOR_WINDOW 行（见上方注释）。
    底部增量读到的一定是末尾几行，限制窗口不影响锚定。"""
    n = len(tail)
    if getattr(entry, "kind", "msg") == "divider":
        return any(row["content_type"] == "time_divider"
                   and normalize(entry.content) == normalize(row["content"])
                   for row in tail[-TEXT_ANCHOR_WINDOW:])
    sender = _entry_sender(entry)
    for i, row in enumerate(tail):
        if row["content_type"] == "time_divider":
            continue
        if i >= n - 3 and _media_eq(entry, row):
            return True
        if i >= n - TEXT_ANCHOR_WINDOW and \
                fuzzy_eq(sender, entry.content, row["sender"], row["content"]):
            return True
    return False


@_locked
def append_incremental(conn, session_id, entries, source="incremental",
                       captured_ts=None, gap_ok=False):
    """统一增量 append API（§4.2）：entries 屏内有序（早->晚）。

    语义（session_tail + fuzzy_eq 分界续写）：
      - 日志为空 -> 走 merge_stack 首合并（top_reached=False），全部视为新；
      - 日志非空 -> 屏内条目与日志尾 LOG_TAIL_N 条做 L2 模糊匹配，最后一个
        命中条目为分界，其后条目按 latest_seq+1.. 续写（绝不改已有段，
        msg_uid UNIQUE 幂等兜底）；
      - 零命中 = 缺口（中间消息没采到）：默认不落库，返回 gap=True，由调用方
        排 backfill（绝不静默错拼）；gap_ok=True 用于 self_send 这类"确定是
        新消息"的场景，无重叠也照追加。

    返回 {'inserted': n, 'attempted': n, 'new': [...], 'gap': bool}。
    """
    captured_ts = captured_ts or time.time()
    entries = [e for e in entries
               if not (e.content_type == "text" and not e.content.strip()
                       and getattr(e, "kind", "msg") == "msg")]
    tail = session_tail(conn, session_id)
    if not tail:
        r = merge_stack(conn, session_id, entries, source=source,
                        top_reached=False, captured_ts=captured_ts)
        return {"inserted": r["inserted"], "attempted": r["attempted"],
                "new": list(entries), "gap": False}

    split = -1                        # 最后一个已在日志中的条目下标
    for i, e in enumerate(entries):
        if _entry_in_tail(e, tail):
            split = i
    if split < 0 and not gap_ok:
        return {"inserted": 0, "attempted": 0, "new": [], "gap": True}
    new_entries = entries if split < 0 else entries[split + 1:]

    latest = conn.execute(
        "SELECT MAX(seq) s FROM messages WHERE session_id=?",
        (session_id,)).fetchone()["s"]

    conn.execute("BEGIN IMMEDIATE")
    try:
        inserted, _, max_seq = _insert_rows(
            conn, session_id, new_entries, latest + 1, source, captured_ts)
        if max_seq is not None:
            conn.execute(
                "UPDATE sessions SET latest_seq = MAX(COALESCE(latest_seq, ?), ?)"
                " WHERE session_id=?", (latest, max_seq, session_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"inserted": inserted, "attempted": len(new_entries),
            "new": new_entries, "gap": False}


# ---------------------------------------------------------------- 文本 log 导出
@_locked
def export_text_log(conn, session_id, out_dir):
    """库 -> 文本单向生成（§1.3）：全量重写 <out_dir>/<会话名>.log。
    时间分割线原样独立成行；非文本消息已是占位符（[图片]/[语音] 5秒…）；
    多行文本用 ' ⏎ ' 折成一行，保证一条消息一行。"""
    row = conn.execute("SELECT name FROM sessions WHERE session_id=?",
                       (session_id,)).fetchone()
    name = row["name"] if row else f"session_{session_id}"
    safe = re.sub(r"[\\/:*?\"<>|]", "_", name)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{safe}.log")

    rows = conn.execute(
        "SELECT seq, ts_hint, ts_text, sender, is_mine, content_type, content"
        " FROM messages WHERE session_id=? ORDER BY seq ASC",
        (session_id,)).fetchall()
    lines = []
    cur_date = None
    for r in rows:
        if r["ts_hint"]:
            d = time.strftime("%Y-%m-%d", time.localtime(r["ts_hint"]))
            if d != cur_date:
                lines.append(f"========== {d} ==========")
                cur_date = d
        ctype = r["content_type"]
        content = " ⏎ ".join(r["content"].split("\n"))
        if ctype == "time_divider":
            lines.append(r["content"])
        elif ctype == "system":
            lines.append(f"[系统] {content}")
        elif ctype == "recall":
            lines.append(f"[撤回] {content}")
        else:
            lines.append(f"{r['sender']}：{content}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------- 版本号与水位差分（v4 新增）
@_locked
def increment_sync_version(conn, session_id):
    """每次成功同步（日志回传）后调用，sync_version +1。
    返回新的 version 值。"""
    conn.execute(
        "UPDATE sessions SET sync_version = sync_version + 1"
        " WHERE session_id = ?", (session_id,))
    conn.commit()
    row = conn.execute(
        "SELECT sync_version FROM sessions WHERE session_id = ?",
        (session_id,)).fetchone()
    return row["sync_version"] if row else 0


@_locked
def get_sync_version(conn, session_id):
    """查询当前会话的同步版本号。"""
    row = conn.execute(
        "SELECT sync_version FROM sessions WHERE session_id = ?",
        (session_id,)).fetchone()
    return row["sync_version"] if row else 0


@_locked
def get_new_since(conn, session_id, last_seq):
    """水位差分：返回 seq > last_seq 的所有消息（严格大于），按 seq 升序。

    这是"哪些消息是新的"的唯一判定方式——不再需要通知正文猜测。
    返回 list[dict]，每条含 seq/sender/content/content_type/is_mine/mentions/
    media_path/ts_hint。
    """
    rows = conn.execute(
        "SELECT seq, sender, content, content_type, is_mine, mentions,"
        " media_path, ts_hint, ts_text, ts_captured"
        " FROM messages WHERE session_id = ? AND seq > ?"
        " ORDER BY seq ASC",
        (session_id, last_seq)).fetchall()
    return [dict(r) for r in rows]


@_locked
def get_context(conn, session_id, n=200):
    """按量拉取历史：返回尾部 n 条消息，seq 升序（最新在尾）。
    用于拼 prompt 的历史灌注。"""
    rows = conn.execute(
        "SELECT seq, sender, content, content_type, is_mine, mentions,"
        " media_path, ts_hint, ts_text"
        " FROM messages WHERE session_id = ?"
        " ORDER BY seq DESC LIMIT ?",
        (session_id, n)).fetchall()
    return [dict(r) for r in reversed(rows)]
