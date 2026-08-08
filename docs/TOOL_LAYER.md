# 工具层（Tool Layer）

> 被决策层调用的能力集合。纯能力、无状态、无决策。
> 每个工具 = 标准 schema 声明 + 执行体 + 结构化结果。

## 一、工具分组

### 1. 计算机工具（宿主侧）
`bash` / `read` / `write` / `glob` / `grep` —— 文件与 shell 能力。
任务模式的重度用户（自由探索、下载、处理文件）。

### 2. 网络工具
`web_search`（文本搜索）/ `web_fetch`（抓网页）/ `image_search`（图片直链搜索）。
找图、查资料的第一入口。

### 3. 视觉工具
`inspect_image`（多模态读图：本地图片/截图 → 文字描述）。
经决策层 provider 的多模态能力实现（k3 vision），工具层不绑定具体模型。

### 4. 微信动作工具（经交互层 ActionRequest 落地）
`send_image`（path/url/字图三通道）/ `quote_reply` / `plus_panel_tap` /
`send_file`（待实现）等。
这些是**剧本化工具**：内部是验证过的确定性流程，逐步 OCR/状态校验，不盲点。
剧本失败时返回可移交的结构化失败原因（供升级通道使用）。

### 5. 聊天记忆工具
`search_chat_log` / `recall_context` —— 跨时间检索消息日志。

### 6. UI 原语（任务模式专用）
`tap(label)` / `long_press(label)` / `swipe` / `back` / `type(text)` ——
label→元素→随机化落点。这是核心在剧本外自由操控微信的"手"。
原语只暴露语义目标，不暴露坐标。

## 二、工具定义规范

```python
ToolDef(
    name: str,
    description: str,          # 写给 LLM 的：什么时候用、怎么用、失败语义
    parameters: JSONSchema,
    timeout_ms: int,
    max_calls_per_turn: int,   # 每轮/每任务限流（计数随轮次重置）
    requires: list[str],       # 依赖注入声明（context / port / llm …）
    execute: fn(params, deps) -> ToolResult,
)
ToolResult(ok: bool, text: str, data: dict | None)
```

- 描述必须包含**失败后的指引**（如"下载失败就换下一张"），让 LLM 能自主纠错
- 结果文本面向 LLM 阅读，不是面向日志

## 三、横切规则

- **限流**：单轮/单任务调用上限，计数必须在轮次开始处重置
  （漏接重置会导致计数进程级累积、工具被永久封顶）
- **幂等**：同一参数重复调用不产生重复副作用（发图去重、消息发送校验）
- **可观测**：每次调用记录 tool_call 事件（参数、耗时、结果摘要）
- **无静默失败**：失败必须带原因文本；禁止"假装成功"
- 远期兼容 MCP：schema 与执行体分离，之后可整组导出为 MCP server

## 四、明确不做

- 不主动发起任何动作（只被调用）
- 不读人格、不做回复决策
- 不感知"当前在哪个会话"（会话上下文由调用方以参数/deps 传入）
