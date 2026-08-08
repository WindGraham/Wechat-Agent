# -*- coding: utf-8 -*-
"""types.py — 层间契约字段级定义（CONTRACTS.md 的代码实现）。

三层共享的契约类型全部在此定义，字段增删必须先到 docs/CONTRACTS.md。
所有类型用 dataclass 实现，可 JSON 序列化。
"""

from dataclasses import dataclass, field
from typing import Optional


# ═══════════════════════════════════════ 消息与日志


@dataclass
class Message:
    """一条消息。msg_uid 为幂等键，重复同步不重复入库。"""
    session: str              # 会话名
    is_group: bool
    sender: str               # 发送人昵称；自己的消息为 "我"
    is_mine: bool
    content: str              # 文本内容；多媒体为标注后的描述文本
    content_type: str         # text/image/sticker/voice/video/quote/system/
                              # time_divider/multimedia(未标注)
    mentions: list = field(default_factory=list)  # @ 到的昵称列表
    media_path: Optional[str] = None  # 多媒体裁图归档路径（未标注条目用）
    ts: float = 0.0           # 采集时刻 epoch
    seq: int = 0              # 会话内自增序号（唯一、严格递增）
    msg_uid: str = ""         # 幂等键（重复同步不重复入库）


@dataclass
class LogUpdated:
    """交互层 → 决策层，唯一上行通知（轻量，不带消息正文）。

    version 是该会话日志的同步批次号：首次同步=1，每次成功回传 +1。
    决策层用 get_new_since(session, last_seq) 差分出新增消息。
    """
    session: str
    version: int              # 该会话日志的同步版本号
    mention_hint: bool = False  # 本次新增里疑似含 @我（快速提示）


# ═══════════════════════════════════════ 动作与结果


@dataclass
class ActionResult:
    """submit_bundle 的返回。"""
    ok: bool
    error: Optional[str] = None
    retryable: bool = True       # False = 不许重试（如目标会话不存在）
    escalation_hint: Optional[str] = None  # 可移交任务的现场描述


@dataclass
class ActionBundle:
    """决策层 → 交互层：一个会话的一包 XML 动作块。

    blocks 是原始 XML 文本（含 <reply>/<task>/<silent/> 等块）。
    交互层 sender 负责解释执行。
    """
    session: str
    blocks_xml: str            # 原始 XML 动作块文本
    ref: Optional[str] = None  # 引用的消息编号（m1..mN）


# ═══════════════════════════════════════ 任务（决策层 ⇄ 工具层）


@dataclass
class TaskBrief:
    """决策层 → 工具层：任务简报。"""
    goal: str                  # 要达成什么
    context: str = ""          # 相关聊天上下文
    tried: list = field(default_factory=list)  # 已尝试步骤与失败现象
    deliver: str = "reply"     # 结果交付方式（"reply+file" / "reply" / "file"）


@dataclass
class TaskResult:
    """工具层 → 决策层：任务结果。"""
    ok: bool                   # 子进程退出码 0 且有最终文本
    summary: str = ""          # 最后一条 assistant content
    artifacts: list = field(default_factory=list)  # 交付物本机绝对路径
    say_to_user: str = ""      # 可直接人格化的一句话
    cli_session_id: str = ""   # CLI 会话 ID（追问用）
    trace_path: str = ""       # 执行轨迹文件（审计）


# ═══════════════════════════════════════ 队列条目（交互层统一时间序队列）


@dataclass
class QueueEntry:
    """统一时间序队列中的一条记录。"""
    kind: str = ""             # "notify" | "action"
    session: str = ""
    ts: float = 0.0            # 入队时间（排序依据）
    mention: bool = False      # @我/主人 → 插队队首
    payload: str = ""          # action 时为 XML bundle 原文；notify 时为空
    attempts: int = 0          # 行动条目已尝试次数（上限 2，耗尽排队尾）
