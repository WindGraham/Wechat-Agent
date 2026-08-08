# 工具层说明（本目录不放代码）

工具层 = 现成的 CLI agent 框架（当前适配 **Kimi Code CLI**）。
本层不开发任何代码；CLI 的调用由**决策层 Proxy 的 CLI 后端适配器**承担。

## 当前适配：Kimi Code CLI

```bash
# 一次性任务（无头，权限自动放行，模型钉 k3）
kimi -m kimi-code/k3 -p "<任务简报>" --output-format stream-json

# 追问/多轮
kimi -r <session_id> -p "<追问>" --output-format stream-json
```

- stdout 是 JSONL：最后一行带 content 的 assistant 消息 = 任务产出；
  `meta.session_id` 用于追问；退出码 0 = 成功
- 模型可用值：`kimi-code/k3`（默认）、`kimi-code/kimi-for-coding`、
  `kimi-code/kimi-for-coding-highspeed`、`kimi-code/k3-256k`
- 权限/安全：CLI 的 permission 配置 + hooks，不改源码

## 换 CLI 框架时

Proxy 的 CLI 后端是可插拔的（见 DECISION_LAYER.md「CLI 后端适配」）。
新增一个框架 = 在 Proxy 注册一个新的后端适配器，本文件加一节用法说明。
详细设计见 [../docs/TOOL_LAYER.md](../../docs/TOOL_LAYER.md)。
