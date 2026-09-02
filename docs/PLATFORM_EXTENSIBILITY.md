# 平台可插拔：多消息入口 + 可组装 prompt 块 + 热插拔

> 目标：Proxy 从"微信专用"升级为"通用多平台 Agent 决策中枢"，支持
> **便捷添加新消息入口（如 QQ）**、**按入口组装不同 prompt 块**、
> **proxy 层不重启、交互层可随时重启（热插拔）**。

---

## 一、调研结论：现有架构已经具备哪些？

### 1.1 已具备：热插拔的天然基础

| 能力 | 现状 | 说明 |
|------|------|------|
| **prompt 块热重读** | ✅ `PromptLibrary` 按 mtime 缓存 | 改 `config/prompts/*.md` 即生效，**无需重启** |
| **人格卡热重读** | ✅ `PersonaRenderer` 按 mtime 缓存 | 改 `config/personas/*.yaml` 即生效 |
| **runtime.json 热重读** | ✅ `RuntimeConfig` 按 mtime 热读 | 改配置即生效 |
| **事件处理器热插拔** | ✅ `handlers/registry.py` 自动注册 | 新增 `handlers/<name>.py` 即注册（但需导入） |
| **进程间 HTTP 通信** | ✅ 已存在 agent 侧回调端口 13015 | `task_done`/`aside`/`status`/`decision_model` |
| **网关热重载** | ✅ `HotReloadServer` | 改网关代码自动 reload，不影响 agent |

**关键结论**：`PromptLibrary` 已经支持 mtime 热重读——所以"加一个 prompt 块拼进去"这个需求，**实际上最简单的方式是往 `config/prompts/` 加文件 + 改 `order.txt`，完全不需要重启**。

### 1.2 当前缺口（要做热插拔入口需要补的）

| 缺口 | 现状 | 需要的改动 |
|------|------|-----------|
| **无平台概念** | 假设都是微信 | 引入 `platform` 字段贯穿 |
| **无统一事件** | `notify_log_updated` 是微信专用概念 | 引入 `MessageEvent` 统一入口 |
| **输出格式硬编码** | `output_protocol.md` 写死微信 XML | 按平台选格式块 |
| **进程边界单一** | proxy 与交互层同一进程 | 独立交互层走 HTTP 通信 |

---

## 二、核心设计：三条边界

要实现你的需求，本质是把 Proxy 拆成**三条边界**：

```
┌─────────────────────────────────────────────────────────────┐
│                      Proxy 决策核心（常驻）                    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  ① 消息总线（输入边界）: 统一 ingress + MessageEvent   │    │
│  │     - 单一端口接收所有来源，每条消息带 source 标记     │    │
│  │     - 热插拔：进程外 HTTP /ingress 注入（不重启）      │    │
│  │     - 内置：notify_message / inject_aside / task_done │    │
│  └───────────────────┬─────────────────────────────────┘    │
│                      │                                       │
│  ┌───────────────────▼─────────────────────────────────┐    │
│  │  ② 决策核心 (Decider)                                │    │
│  │     - 构建 prompt：人设 + 平台格式块 + 工具           │    │
│  │     - 调 LLM → 解析 XML 块                           │    │
│  │     - 按平台路由输出                                  │    │
│  └───────────────────┬─────────────────────────────────┘    │
│                      │                                       │
│  ┌───────────────────▼─────────────────────────────────┐    │
│  │  ③ 输出边界（路由 + 发送）: submit_bundle 按平台分发  │    │
│  │     - wechat → 交互层 queue（既有）                   │    │
│  │     - qq     → HTTP 回调到 QQ 交互层（新增）           │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
         ▲                              ▲
  微信交互层(同进程/如常)          QQ 交互层(独立进程/热插拔)
  LogUpdated → proxy              HTTP POST → proxy
         └──── proxy 输出 ────┘   └──── proxy 输出 ────┘
```

### 边界 ①：输入边界 = 统一 ingress 端口（进程外 HTTP 注入）

用户要求"proxy 接收不重启、交互层可重启"，且"所有来源从一个端口走、标清来源"。
这指向**单一 ingress 端口 + 来源标记**：微信/QQ/未来平台都 POST 到同一个入口，
消息里带 `source` 字段。

现有架构已有这个能力：`main.py:_start_task_done_callback()` 在 13015 端口起了一个 Flask 回调服务，
已经实现了 `task_done`/`aside` 的进程外注入。**只需扩展一个 `/ingress` 端点**。

```python
# main.py 的 agent 回调端口（13015）扩展为统一消息总线
@cb_app.route("/ingress", methods=["POST"])
def _ingress():
    body = request.get_json(silent=True) or {}
    source = body.get("source", "wechat")   # 标清来源
    ev = MessageEvent(source=source, **{k: v for k, v in body.items()
                                        if k in MessageEvent.__dataclass_fields__})
    proxy.notify_message(ev)
    return jsonify({"ok": True})
```

QQ 交互层（独立进程）只要 `POST http://127.0.0.1:13015/ingress`
并带 `source:"qq"` 即接入，**proxy 不重启**；其他平台同理。

### 边界 ②：prompt 块组装 = 按平台选格式（已在 mtime 热重读基础上）

用户说"加一个 prompt 块，构建的时候拼进去"。这正好落在已有 `PromptLibrary` 的
mtime 热重读机制上。**方案：按平台分文件 + order.txt 条件装配**。

```
config/prompts/
├── order.txt                         # 装配清单（新增 [platform] 分段指令）
├── system/
│   ├── persona.md                    # 通用：人设
│   ├── tools.md                      # 通用：工具说明
│   └── formats/
│       ├── wechat.md                 # 微信输出格式（从 output_protocol.md 拆出）
│       └── qq.md                     # QQ 输出格式（新增）
└── user/                             # 不变
```

改 `PromptLibrary.system_blocks()` 支持平台条件装配：

```python
def system_blocks(self, platform="wechat", **slots) -> list:
    # order.txt 里 [platform:qq] / [platform:wechat] / [all] 分段
    # 只拼当前平台相关的块
    ...
```

**"加一个 QQ 入口的格式" = 新建 `config/prompts/system/formats/qq.md` + order.txt 加一行**
→ 改动是 `config/prompts/`，mtime 热重读，**不需要重启**。

### 边界 ③：输出路由 = 按平台分发 submit_bundle

```python
# proxy.py 增加平台注册表
# platform -> submit_fn(session, xml) -> ActionResult
self._platform_outs = {
    "wechat": submit_bundle,           # 既有：微信交互层
    # "qq": qq_http_submit,           # 新增：进程外 HTTP 回 QQ 交互层
}

def register_platform_out(self, platform, submit_fn):
    """热注册平台输出端（进程外 QQ 交互层可调用）。"""
    self._platform_outs[platform] = submit_fn
```

Decider 路由时按块上的 `platform` 选发送端：

```python
for b in reply_blocks:
    target_plat = parse_attrs(b.attrs).get("platform", "wechat")
    submit_fn = proxy._platform_outs.get(target_plat, self._submit_bundle)
    submit_fn(target_session, xml)
```

---

## 三、推荐方案：统一消息总线（单一 ingress 端口 + 来源标记 + 按来源回传）

> **核心思路**：不是"每平台一个连接"，而是 **Proxy 开一个统一的接收端口**，
> **所有来源（微信/QQ/未来平台）的消息都从这一个口进**，每条消息**标明来源**，
> 决策后 **Proxy 把输出回传到对应来源的那个接口**。这就是给 Proxy 装一个
> **通用消息总线**——加平台 = 往总线上挂一个新来源，不动 Proxy 主逻辑。

```
                    ┌──────────────────────────────────────────┐
                    │       网关 gateway (13014)  常驻/热reload  │
                    └──────────────────┬───────────────────────┘
                                       │ 管理 agent 子进程
                    ┌──────────────────▼───────────────────────┐
                    │      agent 子进程（常驻，proxy 在其中）      │
                    │  ┌─────────────────────────────────────┐  │
                    │  │  Proxy 消息总线（统一入口）            │  │
                    │  │  http://127.0.0.1:13015 (ingress)    │  │
                    │  │                                     │  │
                    │  │  POST /ingress ← 所有来源的消息汇到这  │  │
                    │  │     { source:"qq", session:"某群",     │  │
                    │  │       sender, content, mentions,... }  │  │
                    │  │       ↑ 每条消息标明来源 source        │  │
                    │  │                                     │  │
                    │  │  决策(Decider) → 解析 → 按 source 路由  │  │
                    │  │                                     │  │
                    │  │  POST out:<source> ← 回传对应来源接口  │  │
                    │  │    (.wechat→同进程queue; .qq→HTTP回QQ)  │  │
                    │  └─────────────────────────────────────┘  │
                    └──────────────────▲───────────────────────┘
                                       │ POST /ingress
              ┌────────────────────────┴───────────────────────┐
              │ 来源注册表 source → outbound 回调地址            │
              │  wechat: 同进程 submit_bundle（约定）            │
              │  qq:      http://127.0.0.1:xxxx/output          │
              │  future:  http://.../output （启动时注册）        │
              └────────────────────────────────────────────────┘
```

### 数据流（一条 QQ 消息为例）

```
QQ 交互层(独立进程)
   │ 收到一条 QQ 消息
   ▼
POST http://127.0.0.1:13015/ingress
   { source:"qq", session:"某QQ群", sender:"张三",
     content:"在吗", mentions:[], ts:2026-09-02T18:00 }
   │
   ▼
Proxy.notify_message(ev)   ← 统一入口，只认 source + 纯数据
   │ 入事件队列 → Decider 决策
   │   · 构建 prompt：人设 + formats/qq.md（按 source 选的格式块）
   │   · 调 LLM → 解析 <reply/> 块
   ▼
Proxy 路由：
   · 块带 platform="qq" → 查来源注册表 → 拿到 QQ outbound 地址
   · POST http://127.0.0.1:xxxx/output  { blocks_xml }
   │
   ▼
QQ 交互层: 解析 blocks_xml → 真的发出 QQ 消息
   → agent/proxy 全程不重启，QQ 交互层可随时重启
```

### 关键设计点

1. **单一 ingress 端口**：Proxy 只暴露**一个** `13015/ingress` 接收口，
   所有来源都 POST 到这里，**消息里带 `source` 字段**标明来自哪个平台。
   → 加平台不用改 Proxy 的输入逻辑，只多一个 source。
2. **来源注册表（source → outbound 地址）**：每个来源进程在启动/接入时
   **注册自己的回传地址**（QQ 交互层 POST `/register` 上报 `{source:"qq",
   callback:"http://127.0.0.1:xxxx/output"}`）。Proxy 决策后按 `source`
   **查表回传到对应接口**。这正好满足你说的"回传对应的接口"。
3. **Proxy 不重启**：注册、ingress、格式块全走 `runtime/prompts` 热重读
   + 内存注册表，加来源/改格式都**不碰主逻辑、不重启**。
4. **交互层可重启**：QQ 交互层崩了/重启，Proxy 只在下一条消息来时
   找不到 outbound 时降级记日志，**Proxy 本身无感**。
5. **加平台 = 注册来源 + 加一个格式块**：
   ```python
   proxy.register_source("qq", callback="http://127.0.0.1:xxxx/output")
   # 格式块：新建 config/prompts/system/formats/qq.md，自动热重读
   ```

---

## 四、可选实现路径对比

| 方案 | 说明 | 优缺点 |
|------|------|--------|
| **A. 进程外 HTTP（推荐）** | QQ 交互层独立进程，HTTP 注入 | ✅ proxy 不重启、交互层可重启、平台独立部署；❌ 需维护 HTTP 协议 |
| **B. 同进程插件** | 平台做成 proxy 内插件 | ✅ 简单；❌ 不符合"交互层可重启"，改插件代码要重启 |
| **C. 消息队列** | Redis/kafka 中转 | ✅ 最解耦；❌ 引入外部依赖，过重 |
| **D. 共享目录/MQ** | 轮询文件 | ❌ 延迟高、不实时 |

**推荐 A**：贴合你"独立 QQ 交互层可重启"的核心诉求，且现有 13015 端口已具备雏形。

---

## 五、实施步骤（分 3 步，每步可验证）

### Step 1：协议与类型（最小改动，微信行为不变）

1. `src/shared/types.py` 加 `MessageEvent`（含 `source` 字段）
2. `src/decision/proxy/events.py` 加 `EV_MESSAGE`
3. `src/decision/proxy/proxy.py` 加 `notify_message()` + 兼容 `notify_log_updated()`
4. `src/main.py` 回调端口加 `/ingress` 统一接收端点（含来源注册表 `/register`）
5. **验证**：POST /ingress 带 `source:"qq"` 能触发决策；微信走旧接口无感。

### Step 2：prompt 块按来源组装

1. `output_protocol.md` 拆为 `system/formats/wechat.md`
2. `PromptLibrary.system_blocks()` 支持 `[source]` 条件装配
3. `ContextBuilder.build()` 加 `source` 参数
4. **验证**：改 formats/qq.md 即时生效，无需重启。

### Step 3：QQ 交互层接入

1. 用户实现 QQ 交互层（独立进程）：监听 + POST /ingress（带 source="qq"）+ 收 out:<qq>
2. `proxy.register_source("qq", callback=...)` 注册来源回传地址
3. `Decider` 路由按 source 分发到对应 outbound
4. **验证**：QQ 消息 → proxy 决策 → 回 QQ；全程不重启 agent。

---

## 六、关键设计决策

- **输入走进程外 HTTP**：因为要"交互层可重启、proxy 不重启"
- **prompt 块走 mtime 热重读**：因为"加个块拼进去"，不碰代码
- **平台格式 = 纯配置文件**：`formats/<platform>.md`，加平台只加文件
- **session 带平台前缀**：`qq:某群` / `wechat:某群`，避免跨界冲突
- **跨平台转发**：`<reply platform="qq" session="...">` 支持

---

## 七、下一步：需要你确认的

1. **QQ 交互层是否独立进程？**（推荐是，理由见 §三）
2. **平台格式用配置文件（md）还是代码（py）？**（推荐 md，热重读最顺）
3. **是否需要跨平台转发**（微信消息 → QQ 回复）？
4. **先做 Step 1 吗？**（最小改动，验证微信无感）
