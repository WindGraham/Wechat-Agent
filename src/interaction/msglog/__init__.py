# -*- coding: utf-8 -*-
"""msglog — 消息日志（全系统的记忆底座）。

每会话独立持久化（SQLite + 文本导出），msg_uid 幂等。
支持版本号/水位差分：决策层用 get_new_since() 差分出新增消息。
"""

from .message_log import (
    connect,
    get_or_create_session,
    get_sync_version,
    increment_sync_version,
    get_new_since,
    get_context,
    session_tail,
    merge_stack,
    append_incremental,
    update_content,
    export_text_log,
    normalize,
    fuzzy_eq,
)
