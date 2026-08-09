# 网关（Gateway）

> 系统的**控制平面**（control plane）。独立常驻进程，不处理消息主流程。
> 用户唯一入口：`./run.sh` 只启动网关，**agent 的一切操作（启动/停止/重启/
> 看日志）都在网关网页的"控制台"页完成**。
> 核心价值：**行为即文本**——agent 的输出由 prompt 决定，
> 而 prompt 的每一个块（含各群态度）都在网关里实时可改。

## 〇、运行模型（v5 架构）

```
用户执行 ./run.sh
      │
      ▼
┌─────────────────────────────────────┐
│ 网关进程（常驻，独立于 agent）          │
│  ├─ Web 服务（Flask :13014）          │
│  ├─ AgentSupervisor（agent 子进程管家）│
│  │    spawn / monitor / stop / restart│
│  ├─ 热重启（改 src/gateway/*.py 生效）  │
│  └─ 文件读边界（状态/日志/帧快照）       │
└───────────────┬─────────────────────┘
                │ subprocess
        ┌───────▼────────┐
        │ agent 子进程     │
        │ (python -m      │
        │  src.main)      │
        └────────────────┘
```

- **网关永远运行**：agent 退出/崩溃不影响网关（agent 是它的子进程）
- **agent 由网关管理**：网页"控制台"页 → 启动/停止/重启/看日志
- **网关自身常驻保障**：可选 systemd 服务（deploy/wechat-agent-gateway.service，
  开机自启 + 崩溃拉起）；或 restart.sh 手动重启网关
- **热重启**：改 `src/gateway/*.py` 自动 reload 重建 server，
  无需重启网关进程，更不影响 agent

## 一、职责

### 0. Agent 管理（v5 新增，控制台页）

- `POST /api/agent/start` / `stop` / `restart`：管理 agent 子进程
- `GET /api/agent/status`：running / stopped / crashed + pid + 启动时间
- `GET /api/agent/logs`：agent.log 尾部（控制台页展示）
- `POST /api/task_done`：进程外任务完成注入（转发到 agent 侧回调端口 13015）
- 默认**不自动拉起 agent**——用户进网关后手动点"启动"

### 1. Prompt 实时编辑（首要）

- `config/prompts/` 全部块文件（输出协议、工具说明、各 user 模板）的
  查看与编辑，保存即生效（Proxy 按 mtime 重读，下一次决策就用新版）
- `config/personas/` 人格卡编辑：**每个群聊/会话的态度就是一张卡**——
  想让它在某群话多/话少/只回@，改对应会话卡即可，不动任何代码
- 装配顺序（order.txt）可视化调整

### 2. 运行观测

- 统一时间序队列的实时状态（通知/行动条目、当前旅程）
- 决策日志（每次决策的 prompt 摘要、输出、路由结果）
- 任务台账（在飞 subprocess、task.json 状态）
- 首页红点快照（home_scan.json）、原子操作流水（interaction_ops.jsonl）
- （后续迭代）屏幕画面与 OCR 遮罩——帧源走文件边界
  （交互层写 `workspace/runtime/frame.jpg` + boxes JSON，网关只读展示）

### 3. 运行控制

- 暂停/恢复/静默、并发上限、扫描间隔等 runtime.json 项
- 配置变更只写文件，Proxy/循环组按 mtime 热读，网关不侵入任何层的内部状态

### 4. 密钥管理

- API key（LLM provider 等）**可在网关界面配置/修改**，但**只落本机
  环境**：写入本地 env 文件（`workspace/.env`，gitignore），不进任何
  配置文件、不进 git、不出现在日志
- 程序读取优先级：环境变量 > workspace/.env；网关编辑即写 .env 并即时生效
- 网关展示时脱敏（只显示前 4 位 + 后 2 位）

## 二、边界

- 网关是**可选组件**：不开网关，系统照常运行（直读文件）
- 网关只做"读文件/写文件 + 读状态接口 + agent 子进程管理"，
  **不调用交互层/决策层任何函数**，层对它的感知只有"文件变了"
- 默认只监听本机回环，鉴权后再考虑暴露

## 三、运行与运维

### 启动

```bash
./run.sh            # 前台跑网关（Ctrl+C 退出）
./run.sh -d         # 后台跑网关（日志 logs/gateway.log）
./run.sh stop       # 停止后台网关
```

网关起来后浏览器打开 http://127.0.0.1:13014/ → 控制台页启动 agent。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHAT_AGENT_GATEWAY_HOST` | 127.0.0.1 | 绑定地址 |
| `WECHAT_AGENT_GATEWAY_PORT` | 13014 | 端口 |
| `WECHAT_AGENT_GATEWAY_TOKEN` | 无 | 设置后所有请求需 `Authorization: Bearer <token>` |
| `WECHAT_AGENT_PYTHON` | ~/.venvs/wechat-agent/bin/python | agent 解释器路径 |
| `WECHAT_AGENT_WORKSPACE` | 无 | 传给 agent 的 --workspace |
| `WECHAT_AGENT_CONFIG` | 无 | 传给 agent 的 --config |
| `WECHAT_AGENT_CALLBACK_PORT` | 13015 | agent 侧 task_done 回调端口 |

### 热重启

改 `src/gateway/*.py`（app.py 路由/页面/group_config）→ 自动 reload 重建
server（1~2 秒短暂不可用），**agent 不受影响**。改 prompt/人格/runtime 等
内容文件则完全不需要重启。

### 网关自身重启

- systemd（推荐）：`systemctl restart wechat-agent-gateway`
- 手动：`./restart.sh`（轮转 logs/gateway.log 后重新拉起）

## 四、开发模式

`python -m src.main --with-gateway`：agent 进程内嵌网关线程（旧模式），
方便单进程调试；生产一律用独立网关（run.sh）。

## 五、agent 侧 task_done 回调

独立网关模式下，agent 进程内会启动一个本地回调端口
（默认 127.0.0.1:13015，`WECHAT_AGENT_CALLBACK_PORT` 可覆盖），
网关 `/api/task_done` 收到的任务完成事件**转发**到这个端口，
由 agent 内的 Proxy 走正常回执流程交付（2026-08-09 用户要求）。
