# 层间契约（字段级定义）

> 本文件是 `src/shared/` 的唯一蓝本。三层共享的契约类型全部在此定义，
> 字段增删必须先到本文。所有类型用 dataclass 实现，可 JSON 序列化。

## 一、消息与日志

```python
Message:
    session: str          # 会话名
    is_group: bool
    sender: str           # 发送人昵称；自己的消息为 "我"
    is_mine: bool
    content: str          # 文本内容；多媒体为标注后的描述文本
    content_type: str     # text/image/sticker/voice/video/quote/system/
                          # time_divider/multimedia(未标注)
    mentions: list[str]   # 消息里 @ 到的昵称列表（可为空）
    media_path: str|None  # 多媒体裁图归档路径（未标注条目用）
    ts: float             # 采集时刻 epoch
    seq: int              # 会话内自增序号（唯一、严格递增）
    msg_uid: str          # 幂等键（重复同步不重复入库）
```

```python
LogUpdated:               # 交互层 → 决策层，唯一上行通知
    session: str
    version: int          # 该会话日志的同步版本号：首次同步=1，每次成功回传 +1
    mention_hint: bool    # 本次新增里疑似含 @我（快速提示，以日志 mentions 为准）
```

**版本/水位语义：**
- `seq` 是消息级的；`version` 是同步批次的。两者都由交互层维护、持久化
- 决策层为每个会话保存水位 `last_seq`；`get_new_since(session, last_seq)`
  返回 `seq > last_seq` 的行（严格大于），按 seq 升序
- 决策层重启：水位从 `workspace/runtime/watermarks.json` 恢复；
  丢失则从当前尾部起算（不回放历史）
- 标注写回不改变 seq/version（content 原地更新）

```python
# 决策层 → 交互层 读取接口
get_context(session, n=200) -> list[Message]       # 尾部 n 条，seq 升序
get_new_since(session, last_seq) -> list[Message]  # 水位差分
```

## 二、动作与结果

```python
ActionResult:             # submit_bundle 的返回
    ok: bool
    error: str|None
    retryable: bool          # False = 不许重试（如目标会话不存在）
    escalation_hint: str|None  # 可移交任务的现场描述（None = 不建议升级）
```

## 三、任务（决策层 ⇄ 工具层）

```python
TaskBrief:
    goal: str             # 要达成什么
    context: str          # 相关聊天上下文
    tried: list[str]      # 已尝试步骤与失败现象（无则空）
    deliver: str          # 结果交付方式（"reply+file" / "reply" / "file"）
    # 末尾固定输出约定句："最后一律用一句话总结：成了什么、
    # 交付物在哪（本机绝对路径）、需要告诉用户什么。"

TaskResult:
    ok: bool                 # 子进程退出码 0 且有最终文本
    summary: str             # 最后一条 assistant content
    artifacts: list[str]     # 交付物本机绝对路径（从 summary/目录提取）
    say_to_user: str         # 可直接人格化的一句话
    cli_session_id: str      # CLI 会话 ID（追问用）
    trace_path: str          # 执行轨迹文件（审计）
```

`task.json`（workspace/tasks/<日期>/<任务目录>/ 台账）：

```json
{
  "task_id": "t0007",
  "session": "特高课",
  "refs": ["m3", "m5"],
  "ref_briefs": ["风图: 帮我把本周记录整理成 PDF", "..."],
  "desc": "整理本周记录生成PDF",
  "deliver": "reply+file",
  "status": "running|done|failed|timeout|cancelled",
  "started_at": 1786..., "finished_at": null,
  "cli_session_id": "session_..."
}
```

## 四、队列条目（交互层统一时间序队列）

```python
QueueEntry:
    kind: str       # "notify" | "action"
    session: str
    ts: float       # 入队时间（排序依据）
    mention: bool   # @我/主人 → 插队队首
    payload: str    # action 时为 XML bundle 原文；notify 时为空
    attempts: int   # 行动条目已尝试次数（上限 2，耗尽排队尾）
```

## 五、runtime.json（运行时配置字段表）

| 字段 | 默认 | 含义 |
|---|---|---|
| `max_concurrent_decisions` | 1 | 决策信号量上限 |
| `media_convert_concurrency` | 2 | 媒体转换并发数 |
| `history_size` | 200 | prompt 历史灌注条数 |
| `sweep_interval` | [45, 90] | 首页扫描间隔（秒，随机区间） |
| `notify_interval` | [3, 6] | 通知轮询间隔 |
| `paused` | false | 全局暂停（只捕获不动作） |
| `muted_until` | 0.0 | 全局静默截止 epoch |
| `owner` / `owner_nick` | - | 主人会话名 / 主人@我时的昵称 |
| `action_max_attempts` | 2 | 行动条目尝试上限 |
| `task_retention_days` | 14 | tasks/ 目录保留天数 |

## 六、@我 判定规则（Policy 用）

满足其一即"@我"：消息 `mentions` 中有昵称与 `owner_nick` 匹配
（归一化后相等或互为包含，容忍 OCR 粘连）；或 content 含 `@所有人`。
判定以日志 mentions 为准（`mention_hint` 只是调度提示）。
已回复登记：`workspace/runtime/replied_mentions.json`，
key = normalize(sender+content)，回复成功后登记，防重复回复。
