# 工具层（Tool Layer）

> 不自研。直接接入一个现成的 CLI agent 框架，为决策层提供自由任务执行能力。
> 首选：**Kimi Code CLI**（开源 MIT，`MoonshotAI/kimi-code`）。

## 一、选型

| 候选 | 结论 |
|---|---|
| **Kimi Code CLI**（首选） | MIT 开源可改内部；`kimi -p` 无头执行 + `--output-format stream-json` 结构化输出；`kimi web` 常驻 REST/WS 服务；子代理/Skills/Hooks/MCP 齐全；provider 可换（k3/DeepSeek/本地端点） |
| OpenCode（备选） | 开源、模型无关、server 模式；想脱离 Kimi 生态时用 |
| Claude Code | 闭源，排除 |

## 二、本层做什么

工具层 = CLI 框架 + 一层薄封装（adapter）。薄封装负责：

1. **任务下发**：把决策层的 `TaskBrief` 翻译成 CLI 调用
   - 一次性任务：`kimi -p "<简报>" --output-format stream-json`（无头模式权限自动放行）
   - 长驻/多任务：`kimi web` REST API（OpenAPI 文档自动生成）
2. **权限与安全**：用 CLI 的 permission 配置 + lifecycle hooks 实现
   （哪些目录可写、哪些命令要拦），不改源码
3. **结果回传**：解析 stream-json/REST 响应 → `TaskResult` 结构化返回决策层
4. **能力扩展**：以 CLI 的 Skills/MCP 机制添加能力（如把微信 UI 原语封装成
   MCP server 给它用），而不是自己造工具框架

## 三、与决策层的接口

```python
run_task(brief: TaskBrief) -> task_id        # 异步
get_result(task_id) -> TaskResult | Pending  # 轮询/回调
```

`TaskResult.say_to_user` 由决策层人格化后发出——工具层的原始输出不直接见人。

## 四、横切规则

- 每次任务留痕：简报、执行轨迹、结果可审计
- 工作目录隔离：任务在指定 workspace 下执行，微信数据只读暴露
- 超时与取消：任务有 wall-clock 上限，决策层可取消
- 失败如实回传，由决策层决定重试、降级还是如实告诉用户

## 五、明确不做

- 不自己实现 bash/文件/搜索等工具（CLI 自带）
- 不感知微信协议和消息格式（需要操作微信时通过专用 MCP/skill 向交互层要能力）
- 不接触人格与回复话术
