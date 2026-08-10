# -*- coding: utf-8 -*-
"""tools/warm_memory.py — 冷启动记忆预热工具（命令行）。

把积累的聊天记录按会话分批，注入 proxy 生成初始 memory（不回复）。
用于新部署/清空记忆后，让 agent 快速建立对历史会话与用户的了解。

用法：
    python -m src.tools.warm_memory                # 预热所有会话（分批）
    python -m src.tools.warm_memory --session 特高课  # 只预热指定会话
    python -m src.tools.warm_memory --dry-run      # 只打印分批计划，不注入
    python -m src.tools.warm_memory --batch 300    # 每批条数（默认 300）

原理：
  1. 读 workspace/chatlogs/chatlog.db 的 messages（按会话、seq 升序）
  2. 每会话切成 <= batch_size 的批次（大会话多批）
  3. 构造 Proxy（假 provider/不启动循环），逐批调 proxy.warm_memory()
  4. 批次间等待（等上一批处理完），同会话串行、跨会话也串行（简单可靠）

注意：
  - 只生成 memory（写 workspace/memory/），不回复、不影响交互层
  - 会调用 LLM（每批一次），用便宜模型可传 --model deepseek
"""

import argparse
import logging
import os
import sqlite3
import sys
import time

# 仓库根：src/tools/warm_memory.py → _FILE_DIR=src/tools → 向上 2 级
_FILE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(_FILE_DIR))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("warm_memory")

DB_PATH = os.path.join(PROJECT_ROOT, "workspace", "chatlogs", "chatlog.db")
DEFAULT_BATCH = 300


def load_session_history(conn, session_id, limit=5000):
    """取一个会话的历史（seq 升序，过滤占位/系统），返回 list[dict]。"""
    rows = conn.execute(
        "SELECT sender, is_mine, content, content_type FROM messages"
        " WHERE session_id=? AND content_type NOT IN"
        " ('time_divider','system') ORDER BY seq ASC LIMIT ?",
        (session_id, limit)).fetchall()
    out = []
    for r in rows:
        content = (r["content"] or "").strip()
        if not content or r["content_type"] in ("multimedia", "image",
                                                "sticker", "voice", "video"):
            continue    # 多媒体占位不参与预热（无文字内容）
        out.append({
            "sender": "我" if r["is_mine"] else (r["sender"] or "?"),
            "content": content,
        })
    return out


def chunk(items, size):
    """切成 size 一批。"""
    return [items[i:i + size] for i in range(0, len(items), size)]


class FakeMsg:
    """预热用的最小消息对象（sender/content/is_mine）。"""

    def __init__(self, sender, content):
        self.sender = sender
        self.content = content
        self.is_mine = (sender == "我")


def build_plan(conn, batch_size):
    """返回 [(session_name, [batch1, batch2, ...]), ...]，按消息量降序。"""
    plan = []
    sessions = conn.execute(
        "SELECT session_id, name FROM sessions ORDER BY session_id").fetchall()
    for s in sessions:
        hist = load_session_history(conn, s["session_id"])
        if not hist:
            continue
        batches = chunk(hist, batch_size)
        plan.append((s["name"], batches))
    # 消息多的会话在前（先预热重点会话）
    plan.sort(key=lambda p: -sum(len(b) for b in p[1]))
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.warm_memory",
        description="冷启动记忆预热：聊天记录分批 → 生成 memory（不回复）")
    parser.add_argument("--session", default="", help="只预热指定会话名")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                        help="每批条数（默认 %d）" % DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印分批计划，不注入")
    parser.add_argument("--model", default="k3",
                        help="预热用模型（默认 k3；可用 deepseek 省钱）")
    parser.add_argument("--gap", type=float, default=2.0,
                        help="批次间隔秒数（默认 2s）")
    args = parser.parse_args(argv)

    if not os.path.exists(DB_PATH):
        print(f"❌ 消息库不存在: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        plan = build_plan(conn, args.batch)
        if args.session:
            plan = [(s, b) for s, b in plan if s == args.session]
        if not plan:
            print(f"⚠️ 没有可预热的会话（{args.session or '全部'}）")
            return 0

        total_batches = sum(len(b) for _, b in plan)
        total_msgs = sum(len(m) for _, b in plan for m in b)
        print(f"===== 记忆预热计划 =====")
        for name, batches in plan:
            print(f"  {name}: {len(batches)} 批, "
                  f"{sum(len(b) for b in batches)} 条")
        print(f"共 {len(plan)} 会话 / {total_batches} 批 / {total_msgs} 条")
        if args.dry_run:
            print("（--dry-run，未注入）")
            return 0

        # 构造 Proxy（仅用 warm_memory，不启动事件循环）
        from src.decision.provider.factory import create_provider
        from src.decision.proxy.proxy import Proxy
        from src.shared.runtime import RuntimeConfig

        runtime = RuntimeConfig(os.path.join(PROJECT_ROOT, "config",
                                             "runtime.json"))
        provider = create_provider(prefer="kimi", model=args.model)
        proxy = Proxy(provider=provider,
                      reader=None,      # 预热不需要 reader
                      submit_bundle=lambda s, x: None,
                      runtime=runtime)

        # 逐批注入（每次 warm_memory 入队后，直接 run_once 处理该批，
        # 保证批次间串行、互不干扰）
        done = 0
        for name, batches in plan:
            for i, batch in enumerate(batches, 1):
                msgs = [FakeMsg(m["sender"], m["content"]) for m in batch]
                proxy.warm_memory(name, msgs)
                # 处理一个事件（该批的 memory warm）
                processed = proxy.run_once()
                if not processed:
                    log.warning("[%s] 批次 %d/%d 未被处理", name, i,
                                len(batches))
                done += 1
                log.info("[%s] 批次 %d/%d 完成（%d 条）", name, i,
                         len(batches), len(msgs))
                if args.gap and (done < total_batches):
                    time.sleep(args.gap)
        print(f"===== 预热完成：{done}/{total_batches} 批 =====")
        print("memory 已写入 workspace/memory/（可在网关观察 agent 后续使用）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
