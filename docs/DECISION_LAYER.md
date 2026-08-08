# 决策层（Decision Layer）

> 系统中唯一调用 LLM 的层。决定"回不回、回什么、做什么、什么时候求助"。
> 模型无关：provider 抽象，可热替换。

## 一、两种运行模式

### 1. 对话模式（高频、快）

由交互层的 `MessageEvent` 触发。流程：

```
事件 → 闸门（全局静默/暂停/群类型规则）→ 组装 prompt（人格 + 上下文 + 屏幕状态 + 工具表）
     → LLM 决策 → [SILENT] 或回复文本/工具调用 → 交互层执行
```

- 必回规则：私聊必回、群聊 @我 必回（逐条）、主人说话必回
- 群聊普通消息由 LLM 结合上下文决策，[SILENT] 是合法输出
- 上下文：msg_log 最近 N 条 + 早期摘要 + 当前屏幕状态摘要

### 2. 任务模式（低频、自由）

复杂任务（"帮我找张图发群里"、"总结一下这周的聊天记录整理成文件"）或
交互层剧本失败升级而来。核心循环：

```
TaskBrief → 规划 → 工具调用（可多个、可多轮、可起子代理）→ 观察结果 → … → TaskResult
```

- 任务模式允许长时运行（分钟级）、大额度工具调用、自由探索
- 子代理隔离：子任务起独立上下文，不污染主对话
- 结果回传后由人格包装成自然语言（"办好了，图发群里了"）

## 二、升级仲裁（什么时候从对话模式升到任务模式）

触发源：
1. LLM 在对话模式主动判定任务超出一轮决策能力 → 自行升级
2. 交互层剧本失败（`ActionResult.ok=False` 且可移交）→ 携带现场信息升级
3. 用户/主人显式指令（"/task …"）

`TaskBrief` 至少包含：目标、已尝试步骤、失败现象、当前屏幕状态、相关上下文。
`TaskResult` 至少包含：成功与否、结果摘要、交付物位置、可对人讲的一句话。

## 三、Provider 抽象

```python
class LLMProvider:
    def chat(messages, tools=None, max_tokens=...) -> Reply: ...
    def vision(image, question) -> str: ...       # 多模态（可选能力）
```

- 首选 k3（kimi-for-coding API）；备选 DeepSeek；可接本地推理端点
- 注意已知坑：k3 传 temperature≠1 会 400（客户端省略该参数）
- 目标：工具调用从文本标签协议升级为**原生 function calling**（tool_calls）

## 四、记忆调用

- 短期：本会话滑动窗口（交互层 SessionContext）
- 长期：search_chat_log / recall_context 工具按需检索
- 任务记忆：TaskBrief/TaskResult 归档，供复盘与审计

## 五、明确不做

- 不碰坐标、不直接操作设备（一切经交互层 ActionRequest 或工具层）
- 不持久化聊天内容（msg_log 归交互层）
- 不内置任何人格文案（人格来自交互层注入）
