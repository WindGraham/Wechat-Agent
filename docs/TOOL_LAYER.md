# 工具层（Tool Layer）

> 不自研。直接接入一个现成的 CLI agent 框架，为决策层提供自由任务执行能力。
> 首选：**Kimi Code CLI**（开源 MIT，`MoonshotAI/kimi-code`）。

## 一、选型

| 候选 | 结论 |
|---|---|
| **Kimi Code CLI**（首选） | MIT 开源可改内部；`kimi -p` 无头执行 + `--output-format stream-json` 结构化输出；`kimi web` 常驻 REST/WS 服务；子代理/Skills/Hooks/MCP 齐全；provider 可换（k3/DeepSeek/本地端点） |
| OpenCode（备选） | 开源、模型无关、server 模式；想脱离 Kimi 生态时用 |
| Claude Code | 闭源，排除 |

## 二、调用协议（已实测验证，v0.34.0）

### 下发

```bash
kimi -p "<任务简报>" --output-format stream-json
# 无头模式：不开 TUI，权限自动放行（auto 策略），不需要人审批
# 常驻/多任务场景：kimi web（REST + WebSocket，自带 /openapi.json）
```

### 返回（stdout 为 JSONL，一行一个对象）

```jsonl
{"role":"meta","type":"system.version","version":"0.34.0"}
{"role":"assistant","tool_calls":[{"type":"function","id":"tool_xxx","function":{"name":"Bash","arguments":"{...}"}}]}
{"role":"tool","tool_call_id":"tool_xxx","content":"..."}
{"role":"assistant","content":"最终回答文本"}
{"role":"meta","type":"session.resume_hint","session_id":"session_...","command":"kimi -r session_..."}
```

| 行类型 | 含义 | 用途 |
|---|---|---|
| `assistant` + `tool_calls` | 一次工具调用（名字+参数） | 执行轨迹/审计 |
| `tool` | 工具执行结果 | 执行轨迹/审计 |
| **最后一条带 `content` 的 `assistant`** | **任务产出（最终回答文本）** | `TaskResult.summary` 的素材 |
| `meta: session.resume_hint` | 会话 ID | 追问/补任务：`kimi -r <session_id>`（模拟层⇄核心对话能力的通道） |
| 进程退出码 | 0 = 正常完成 | `TaskResult.ok` |

**注意**：没有现成的"结果对象"（无 success 标志、无耗时/token 统计）。
产出就是模型最后那段纯文本；想要结构化产出，必须在任务简报里约定输出格式。

### 输出约定（写进每个 TaskBrief）

任务简报末尾固定一句：**"最后一律用一句话总结：成了什么、交付物在哪、
需要告诉用户什么。"** 薄封装取最后一条 assistant content 即可直接得到
`say_to_user` 素材。

思考过程和工具进度走 stderr，stdout 的 JSONL 是干净的可解析流。

## 三、本层做什么

工具层 = CLI 框架 + 一层薄封装（adapter）。薄封装负责：

1. **任务下发**：把决策层的 `TaskBrief` 翻译成 CLI 调用（无头模式 / `kimi web` REST）
2. **权限与安全**：用 CLI 的 permission 配置 + lifecycle hooks 实现
   （哪些目录可写、哪些命令要拦），不改源码
3. **结果解析**：读 JSONL → 最后一条 assistant content + 退出码 → 拼 `TaskResult`
4. **能力扩展**：以 CLI 的 Skills/MCP 机制添加能力（如把微信 UI 原语封装成
   MCP server 给它用），而不是自己造工具框架

## 四、与决策层的接口（锁定）

```python
run_task(brief: TaskBrief) -> task_id        # 异步
get_result(task_id) -> TaskResult | Pending  # 轮询/回调

TaskBrief(goal, context, tried, deliver)     # + 固定输出约定句
TaskResult(ok, summary, artifacts, say_to_user, session_id, trace)
```

- `say_to_user` 由决策层人格化后发出——工具层的原始输出不直接见人
- `session_id` 保留，供同会话追问（`kimi -r`）
- `trace` 为工具调用轨迹（可选，审计用）

## 五、横切规则

- 每次任务留痕：简报、执行轨迹、结果可审计
- 工作目录隔离：任务在指定 workspace 下执行，微信数据只读暴露
- 超时与取消：任务有 wall-clock 上限，决策层可取消
- 失败如实回传，由决策层决定重试、降级还是如实告诉用户

## 六、明确不做

- 不自己实现 bash/文件/搜索等工具（CLI 自带）
- 不感知微信协议和消息格式（需要操作微信时通过专用 MCP/skill 向交互层要能力）
- 不接触人格与回复话术
