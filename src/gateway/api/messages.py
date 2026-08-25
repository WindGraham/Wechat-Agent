# -*- coding: utf-8 -*-
"""gateway/api/messages.py — 消息浏览 API 蓝图。

提供：/api/messages/groups（各群聊 + 消息数 + 最新时间）
      /api/messages/list（分页查询消息，按采集时间倒序=最新在前）
"""

import os

from flask import Blueprint, jsonify, request

from ...interaction.msglog import message_log


def create_bp(ctx: dict) -> Blueprint:
    root = ctx["root"]
    bp = Blueprint("messages_api", __name__)

    def _db_path():
        return os.path.join(root, "workspace", "chatlogs", "chatlog.db")

    def _query(sql, params=()):
        """开一个只读连接执行查询，用完即关（Flask 多线程安全）。"""
        conn = message_log.connect(_db_path())
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    @bp.route("/api/messages/groups")
    def api_groups():
        """列出所有会话（群聊/私聊）+ 消息数 + 最新消息时间，按发送时间倒序。"""
        rows = _query(
            "SELECT s.name, s.is_group, COUNT(m.id) AS cnt, "
            "       MAX(m.ts_captured) AS last_ts, MAX(m.ts_hint) AS last_hint "
            "FROM sessions s LEFT JOIN messages m ON m.session_id = s.session_id "
            "GROUP BY s.session_id "
            "ORDER BY COALESCE(MAX(m.ts_hint), MAX(m.ts_captured), 0) DESC")
        groups = [dict(r) for r in rows]
        return jsonify({"ok": True, "groups": groups})

    @bp.route("/api/messages/list")
    def api_list():
        """分页查询某个会话的消息，按发送时间倒序（最新在前）。

        排序键：COALESCE(ts_hint, ts_captured) —— 有时间分割线锚的消息用
        发送时间 ts_hint（真·最新），无锚的消息用采集时间 ts_captured 兜底。
        这样 backfill 采集的历史消息（发送时间早）不会因采集时刻晚而排到前面。

        参数：group（会话名，必填）、page（1 起）、page_size（默认 50，上限 200）
        返回：{ok, messages, total, page, page_size, has_more}
        """
        group = request.args.get("group", "").strip()
        if not group:
            return jsonify({"ok": False, "error": "缺少 group 参数"}), 400
        try:
            page = max(1, int(request.args.get("page", 1)))
            page_size = min(200, max(1, int(request.args.get("page_size", 50))))
        except ValueError:
            return jsonify({"ok": False, "error": "page/page_size 必须是整数"}), 400

        # 总数
        total = _query(
            "SELECT COUNT(*) AS n FROM messages m "
            "JOIN sessions s ON s.session_id = m.session_id WHERE s.name = ?",
            (group,))[0]["n"]

        # 分页（最新在前：发送时间 ts_hint 倒序，无锚用 ts_captured，seq 兜底）
        rows = _query(
            "SELECT m.seq, m.sender, m.is_mine, m.content_type, m.content, "
            "       m.ts_hint, m.ts_captured, m.media_path, m.complete, "
            "       m.crop_path "
            "FROM messages m JOIN sessions s ON s.session_id = m.session_id "
            "WHERE s.name = ? "
            "ORDER BY COALESCE(m.ts_hint, m.ts_captured) DESC, m.seq DESC "
            "LIMIT ? OFFSET ?",
            (group, page_size, (page - 1) * page_size))
        messages = [dict(r) for r in rows]
        return jsonify({
            "ok": True,
            "messages": messages,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": page * page_size < total,
        })

    @bp.route("/api/messages/latest")
    def api_latest():
        """按采集时间 ts_captured 增量拉取新入库消息（供前端实时刷新）。

        与 /api/messages/list 的区别：list 按发送时间 ts_hint 倒序（展示用）。
        采集历史（往更早方向翻）时，新入库消息的发送时间更旧、永远排不到
        页首，靠 list 无法检测「有没有新采集」。这里按 ts_captured 排序 +
        since_ts 增量拉取，专门给前端 pollNew 探测新数据。
        """
        group = request.args.get("group", "").strip()
        if not group:
            return jsonify({"ok": False, "error": "缺少 group 参数"}), 400
        try:
            since_ts = float(request.args.get("since_ts") or 0)
            limit = min(500, max(1, int(request.args.get("limit", 200))))
        except ValueError:
            return jsonify({"ok": False, "error": "since_ts/limit 必须是数字"}), 400

        rows = _query(
            "SELECT m.seq, m.sender, m.is_mine, m.content_type, m.content, "
            "       m.ts_hint, m.ts_captured, m.media_path, m.complete, "
            "       m.crop_path "
            "FROM messages m JOIN sessions s ON s.session_id = m.session_id "
            "WHERE s.name = ? AND m.ts_captured > ? "
            "ORDER BY m.ts_captured ASC LIMIT ?",
            (group, since_ts, limit))
        messages = [dict(r) for r in rows]
        max_ts = messages[-1]["ts_captured"] if messages else None
        return jsonify({"ok": True, "messages": messages, "max_ts": max_ts})

    return bp
