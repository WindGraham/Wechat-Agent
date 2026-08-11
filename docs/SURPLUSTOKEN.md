# SurplusToken 文档整理

> 来源：[docs.surplustoken.com](https://docs.surplustoken.com/start/introduction.html)，抓取整理于 2026-08-11。
> SurplusToken 是基于 Sub2API 开发的北大校内 AI 模型接入与资源协作平台，不是 OpenAI / Anthropic / Kimi 等模型厂商的官方网站。
> 平台为动态规则：可用模型、分组倍率、额度、渠道状态随时可能调整，**一律以平台实时页面为准**，本文只记录抓取时点的内容。

---

## 1. 平台概述

### 三项核心功能

| 功能 | 解决的问题 | 主要使用者 | 使用入口 |
|---|---|---|---|
| API 中转 | 用统一 API 地址和密钥接入不同模型与编程客户端 | 需要调用模型的普通用户 | API 密钥、可用渠道、使用记录 |
| 账号共享 | 将有权使用的上游账号贡献到账号池并参与调度 | 持有闲置或可共享账号的人 | 账号池、贡献保护、贡献余额 |
| 订阅拼车 | 多人共同承担订阅资源成本并使用关联分组 | 有稳定周期性需求的成员 | 拼车广场、我的拼车、我的订阅与订单 |

三项功能不互斥：普通用户可只做 API 中转；账号持有者可同时贡献资源；拼车成员仍通过自己的 SurplusToken API Key + 拼车关联分组接入客户端。

### 接入端点

- **OpenAI 兼容（通用）**：`https://surplustoken.com/v1`
- **Anthropic 兼容（Claude Code）**：`https://surplustoken.com`（Claude Code 会自动请求 `/v1/messages`，**不要追加 `/v1`**）
- 若平台控制台提供了校内专用地址，以控制台显示为准。

### API 中转流程

客户端用 SurplusToken API Key 发起请求 → 平台按密钥绑定分组检查模型范围 / 余额 / 并发 / 额度 / 有效期 → 调度系统从可用渠道或账号池选资源 → 返回响应并在使用记录中保存模型、Token、费用、时间与状态。

中转包含：统一鉴权、分组路由、模型兼容（GPT / Claude / Kimi 等）、用量与计费（仪表盘）、运行状态（渠道状态 / 使用记录）、密钥控制（额度、速率、IP、有效期、单独禁用/撤销）。

> **倍率不是模型价格**：分组倍率影响计费，不等于模型单价；实际扣费还受模型价格、渠道和平台规则影响。

### 平台中的其他功能

仪表盘（余额 / 请求 / 消费 / Token / 贡献数据 / 模型分布）、网页对话、使用记录与用量统计、渠道与状态监控、订阅 / 订单 / 充值（含兑换码）、邀请返利与个人资料。

---

## 2. 注册与登录

- 使用**北京大学邮箱（含校友邮箱）**注册、设置密码并登录。
- 目前仅允许 `*.pku.edu.cn`、`*.pku.org.cn`、`*.bjmu.edu.cn` 邮箱注册登录。
- 入口：https://surplustoken.com 注册页面。

---

## 3. 快速开始（推荐使用顺序）

1. **注册并登录**（仅北大邮箱）。
2. 了解**计费与充值**以及**密钥分组**。
3. **创建 API 密钥**（创建时必选分组）。
4. 按需配置客户端：Codex / Claude Code / Kimi Code / OpenCode。
5. 在仪表盘和使用记录中确认请求、模型、Token 与费用。
6. 需要贡献资源 → 读账号共享教程；需要稳定订阅资源 → 读拼车说明。

如何选择（摘要）：

| 你的需求 | 建议方式 |
|---|---|
| 偶尔使用，按量付费 | 创建 API Key，使用普通中转分组 |
| 在 Codex 等客户端统一管理多模型 | API 中转 + 为不同客户端建独立密钥 |
| 有合法闲置上游账号 | 添加贡献账号，设严格共享保护 |
| 持续、可预测的订阅需求 | 拼车广场对比车型、等级、周期、关联分组 |
| 已拼车但仍需按量模型 | 拼车分组与普通中转分组分别建密钥 |

**安全与合规**：只共享本人持有或已获明确授权的资源；不公开 API Key、密码、验证码、Cookie、Token、完整 OAuth 回调地址；发现异常立即禁用密钥、暂停共享并检查使用记录。

---

## 4. 创建 API 密钥

- 步骤：登录 → 密钥管理页面 → 创建密钥 → 输入便于识别的名称（如 `个人电脑 Codex`）→ **创建后立即保存完整密钥**。
- **完整密钥通常只显示一次**；丢失后创建新密钥并撤销旧密钥。
- 管理建议：不写入代码 / 不提交 Git；优先环境变量或客户端安全凭证存储；设备或客户端不再使用时及时撤销对应密钥。

---

## 5. 计费与充值

- 计费页面查看账户余额、调用记录、消费明细；请求费用从账户余额扣除。
- 充值：进入充值页面 → 选金额与支付方式 → 确认订单并支付 → 返回计费页面确认到账。**支付成功后余额可能延迟更新，避免状态未知时重复支付。**
- 费用异常：先撤销可能泄露的 API 密钥，再按调用记录核对时间、模型与请求来源。

---

## 6. API 密钥分组

创建密钥时必须选择一个分组；分组决定可用渠道、适用账号、计费倍率、模型范围与使用限制。

### 分组列表（创建密钥页面显示，抓取时点）

| 分组 | 页面显示倍率 | 备注 |
|---|---|---|
| plus/team | 0.15x | 有正式分组文档（Plus/Team 账号） |
| car1 | 1x | 待补充 |
| pro | 0.2x | 待补充 |
| car2 | 1x | 待补充 |
| claude | 1x | 待补充 |
| dynamic | 1x | 待补充 |
| cc-kiro | 0.2x | 待补充 |
| cc-max | 1.3x | 待补充 |
| cc-aws | 1.5x | 待补充 |
| claude-max | 1x | 待补充 |
| grok | 0.7x | 待补充 |
| kimi福利 | 0.001x | 待补充 |
| Z（GLM） | 0.6x | 待补充 |
| dynamic-kimi | 1x | 待补充 |

### plus/team 分组要点

- 适用账号：Plus 和 Team 账号；平台页面说明为「plus 和 team 账号请选择该分组」。
- 页面显示倍率 0.15x；推荐用途：Plus/Team 渠道接入支持的客户端。
- 创建密钥时选 `plus/team`，可设额度、速率、有效期或 IP 限制。

> Codex 教程的 plus/team 分组**不代表一定适用于 Claude Code**；Claude 模型支持情况以控制台和具体分组文档为准。

---

## 7. 账号共享（添加与贡献 OpenAI 账号）

把本人有权使用的 **OpenAI/ChatGPT OAuth 账号**接入 SurplusToken，由平台按分组、调度状态和保护策略统一使用。

**添加后默认立即开启「参与调度」**——其他符合条件请求可能马上使用该账号并消耗其上游额度；暂不想共享请立即取消参与调度或「更多 → 暂停共享」。

### 添加前须知

- 普通用户侧只支持 **OpenAI OAuth** 账号，不支持直接添加 API Key。
- 确认账号来源合法（本人持有或已获明确授权）；了解额度消耗、限流和上游服务条款风险。
- 移除与贡献无关的敏感信息和第三方授权。
- **不要向他人发送账号密码、验证码、Cookie、Refresh Token、Access Token；完整 OAuth 回调地址不对外分享**（但需在 SurplusToken 官方页面回填）。

### 流程四步

1. **进入账号池**：登录 → 左侧「账号池」→ 右上角「添加 OAuth 账号」（弹窗含「授权方式 / 完成授权」两步）。
2. **填写账号设置**（字段表摘要）：

   | 字段 | 默认/约束 | 建议 |
   |---|---|---|
   | 账号名称 | 必填 | 便于自己识别 |
   | 平台 / 类型 | OpenAI / OAuth | 当前不支持 API Key 账号 |
   | 模型白名单 | 留空 | 留空不额外限制；填写后只允许所选模型 |
   | 仅允许 Codex 官方客户端 | 默认关 | 开启后普通兼容客户端可能无法使用 |
   | 并发数 | 默认 5，最小 1 | 不确定保持默认 |
   | 过期时间 | 默认留空 | 留空=不设平台侧过期 |
   | 过期自动暂停调度 | 默认开 | 到期自动停止调度 |
   | 分组 | 默认未选 | 只能选当前可用且为 OpenAI 的分组 |
   | 代理 | 默认无 | 服务器直连 OpenAI 失败时选择并测试 |

   - 若添加后显示「未绑定分组」：点「编辑账号共享设置」补选 OpenAI 分组。
3. **设置贡献保护策略**（在上游额度不足前自动停止对他人共享）：
   - **按百分比预留**（默认）：5h 至少保留 / 周额度至少保留，默认均为 0%（0% = 不启用该窗口预留，不是保留全部）；如设 20%，则窗口已用约 80% 时停止对他人共享。
   - **按周预算**：限制其他用户在滚动 7 天内的消费；自己和共同所有者不计入；0 = 不设上限；要限制必须填 > 0。
   - **探测失败时**：默认「继续共享」，可选「暂停共享」「按本地账本兜底」；若百分比预留仍为 0%，探测失败策略不产生预留保护效果。
4. **完成 OpenAI OAuth 授权**：
   - 推荐默认「手动授权」：生成授权链接 → 新标签页打开 → 登录并完成授权 → 浏览器跳到类似 `http://localhost:1455/auth/callback?code=...&state=...` → **复制地址栏完整 URL** 回填到「授权链接或 Code」→ 完成授权。
   - **必须复制完整回调 URL**（含 code + state）；只粘贴裸 Code 会出现 `state is required` / `invalid oauth state`。
   - localhost 页面打不开是**正常现象**（回调地址栏变了即可复制）。
   - OAuth 会话有效期 **30 分钟**，保存在平台服务进程内存；超时 / 平台重启 / 跨轮次 / 重新生成链接后需重新生成授权链接。

### 如何确认添加成功

提示「OAuth 账号已添加」并刷新列表；新账号通常显示：归属=我的、平台/类型=OpenAI/OAuth、状态=启用、有效状态=可用、调度=参与调度已开启、保护状态=共享中（或已停止共享）。
**添加成功 ≠ 当前可调度**：实际能否处理请求取决于调度开关、分组、保护策略、过期时间、代理和上游账号状态；「是否出现自用 Key」不能作为唯一判断。

### 添加后管理

编辑账号共享设置 / 暂停-恢复共享 / 测试连接 / 查看统计 / 定时测试 / 重新授权 / 刷新令牌 / 设置隐私 / 恢复状态。
其他用户只能看到安全概览，不能修改你的账号。

### 暂停、删除与撤销

- 临时不共享：取消「参与调度」或「更多 → 暂停共享」（优先）。
- 删除账号：从 SurplusToken 移除并停止调度；**不代表已在 OpenAI 侧撤销 OAuth 登录/授权**，彻底撤权需同时到 OpenAI 账号侧处理。
- 其他导入方式（高级）：手动输入 RT（每行一个 OpenAI Refresh Token）、Codex JSON/AT 批量输入。均属高敏感凭据，优先手动 OAuth。

### 常见错误速查

| 现象 | 处理 |
|---|---|
| 完成授权按钮不可用 | 先生成链接并粘贴回调内容 |
| localhost 打不开 | 正常；复制完整 URL |
| state is required / invalid oauth state | 粘贴裸 Code 或跨轮次；重新生成并粘贴同一轮完整 URL |
| session not found or expired | 会话超 30 分钟或服务重启；重新生成 |
| 服务器无法连接 OpenAI | 回第一步选可用代理并测试（代理只作用于平台服务器，不替浏览器配网） |
| Refresh Token 验证失败 | 过期/撤销/轮换/方式不匹配；建议改手动 OAuth |
| 添加后显示不可用 | 检查调度、保护、周预算、过期、账号状态、分组，再测试连接 |
| 显示未绑定分组 | 编辑共享设置补选可用 OpenAI 分组 |
| 名称/并发/代理/分组错误 | 名称非空；并发≥1；代理存在且启用；分组可用且属 OpenAI |

### 安全建议

只在 SurplusToken 官方页面完成授权；截图不含完整回调 URL / Token / Cookie / 账号 ID；异常用量立即暂停共享并查统计；不再贡献时先停调度、删平台账号、再查 OpenAI 侧授权；不要短时间内反复生成链接或重复授权。

---

## 8. 订阅拼车

多人共同承担订阅资源成本，通过 SurplusToken 关联分组使用服务；成员间**不需要**互传上游账号或个人 API Key。

### 页面内容

拼车广场 / 我的拼车，可按名称、发起人、状态筛选。每辆车展示：名称、说明、发起人、服务类型、大车/小车与等级/账号数、已上车人数/总席位/剩余座位、预计开车时间与加入方式、状态与关联分组。

### 车型与等级（GPT 拼车）

- 小车：每级 +1 账号 + 较少席位；大车：每级 +1 账号 + 较多席位；等级决定账号数与总席位。
- 开车前需同时确定车型与等级；**确认后当月不再升降级**，下月按届时规则重新选择。
- 车型、等级、席位、价格、计算参数可能调整，以拼车页面当期规则为准。

### 加入拼车

拼车广场 → 筛选找到目标车 → 确认车型/等级/席位/价格/服务周期/开车时间 → 确认关联分组支持所需模型与客户端 → 阅读退出、退款、续期、封车、解散规则 → 按提示确认上车和支付 → 在「我的拼车」「我的订阅」检查状态。
「仍有座位」≠ 一定能加入（可能已封车 / 停止招募 / 等待管理员处理），以详情页按钮和状态为准。

### 发起拼车

右上角「发起拼车」→ 填服务类型、车型、等级、招募方式、预计时间、说明。发起前确认：了解当期计费与开车条件；车型等级覆盖预计成员数；说明准确不承诺规则外权益；知道未满员/退出/取消/续期处理；不要求成员私发密码、验证码、Token 或 API Key。可填字段与发起资格随平台设置变化。

### 拼车状态

| 状态 | 含义 |
|---|---|
| 招募中 | 正在找成员；能否加入取决于座位、方式与详情页按钮 |
| 已封车 | 停止继续招募，可能等待开通或结算 |
| 已开车 | 进入使用阶段；成员检查订阅与关联分组 |
| 已取消 | 本期不再继续，费用与后续以订单及平台通知为准 |

### 开车后使用

平台为车关联一个分组；成员用自己的 API Key 选该分组，按客户端文档配置模型。**建议为拼车单独创建密钥**（如 `codex-car1`）：用量独立统计、可单独设额度/速率/有效期、拼车结束直接禁用、便于判断请求是否走了正确分组。

### 成员注意事项

不把自己的平台密钥发给车主或其他成员；不索要/传播上游账号密码、验证码、Cookie、Token；留意开车时间、服务周期、续期与到期通知；定期核对使用记录；达上限先看渠道状态与拼车通知；退出/退款/转让前先读规则并保留订单信息。

### 拼车 FAQ

- 上车后还不能用：可能仍在招募、已封车未开车、订阅或分组未生效；同时检查状态、我的订阅、API Key 分组与通知。
- 有座位没按钮：已封车 / 停止公开招募 / 受限加入方式。
- 拼车结束后密钥还能用吗：密钥本身可能存在，但关联分组访问权限可能随订阅结束失效；建议独立密钥并在结束后禁用/删除。
- 能共用 API Key 吗：不建议；每人用自己账号和 Key，便于统计用量、控制额度、及时撤销。

---

## 9. 客户端接入

各教程公共前提：登录 → 创建 API 密钥 → 选支持目标模型/客户端的分组（倍率、模型 ID、额度、并发见对应分组文档）→ 确认余额充足。API Key 等同账号密码，不要写入源码、提交 Git、出现在截图/录屏/日志。

### 9.1 Codex CLI & Desktop（GPT 模型，Responses API）

- **Base URL：`https://surplustoken.com/v1`**（不要写成 `/v1/responses`，Codex 自动拼路径；不要只写 host）。
- 配置目录：macOS/Linux/WSL `~/.codex/`，Windows 原生 `%USERPROFILE%\.codex\`；主配置 `config.toml`，登录信息 `auth.json`。
- `config.toml` 关键内容（顶层字段须在第一个 `[表名]` 之前）：

  ```toml
  model_provider = "surplustoken"
  model = "gpt-5.6-sol"          # 以平台模型 ID 为准
  model_reasoning_effort = "high"
  disable_response_storage = true
  [model_providers.surplustoken]
  name = "SurplusToken"
  base_url = "https://surplustoken.com/v1"
  wire_api = "responses"
  requires_openai_auth = true
  ```

- 登录（推荐避免密钥进命令历史）：

  ```bash
  read -rsp "SurplusToken API Key: " SURPLUSTOKEN_API_KEY; echo
  printf '%s' "$SURPLUSTOKEN_API_KEY" | codex login --with-api-key
  unset SURPLUSTOKEN_API_KEY
  chmod 600 ~/.codex/auth.json
  ```

  `auth.json` 手动格式：`{"OPENAI_API_KEY": "<SURPLUSTOKEN_API_KEY>"}`（合法 JSON，无注释、末字段无逗号）。
- 验证：`codex` 内 `/status` 确认模型与 provider，发「只回复“连接成功”」；控制台用量记录应出现该请求。
- Desktop：读取当前 Agent Environment 对应的 `.codex`（macOS 读 `~/.codex`）；改配置后完全退出重启。
- 常见错误：
  - **401**：Key 错误/撤销/过期、有空格或换行、改错 `.codex` 目录；`codex login status` 检查，重新 `codex login --with-api-key`。
  - **404 / 模型不存在**：base_url 是否以 `/v1` 结尾、是否误写成 `/v1/responses`、模型 ID 与分组支持是否一致。
  - **429**：频率/并发/余额/订阅额度/上游受限；查控制台密钥状态、余额、用量记录后重试。
  - **配置不生效**：`/status` 看实际读取值；顶层字段是否写在 `[model_providers...]` 之后；是否存在项目级 `.codex/config.toml` 覆盖；完全重启。
  - **CLI 可用 Desktop 不可用**：Windows 检查 Desktop 用的是原生 Windows 还是 WSL 环境；macOS 确认同一系统账号。
- 切换 Provider 找回历史：进原项目目录 `codex resume --all`（或 `/resume`）；不要手动合并/改名/覆盖 `history.jsonl` 与 `sessions`，先备份整个 `.codex`。
- 更新：官方脚本 `curl -fsSL https://chatgpt.com/codex/install.sh | sh`，或 `brew upgrade --cask codex`。

### 9.2 Claude Code（Anthropic 兼容）

- **Base URL：`https://surplustoken.com`**（**不要追加 `/v1`**；Claude Code 自动请求 `/v1/messages`）。
- 用当前终端环境变量先验证：

  ```bash
  export ANTHROPIC_BASE_URL="https://surplustoken.com"
  read -rsp "SurplusToken API Key: " ANTHROPIC_AUTH_TOKEN; echo
  export ANTHROPIC_AUTH_TOKEN
  export ANTHROPIC_MODEL="<CLAUDE_MODEL_ID>"
  claude
  ```

  `ANTHROPIC_AUTH_TOKEN` 让 Claude Code 以 `Authorization: Bearer` 发送密钥；`<CLAUDE_MODEL_ID>` 必须替换为分组支持的准确模型 ID。
- 持久化非敏感配置：用户级 `~/.claude/settings.json`（Windows `%USERPROFILE%\.claude\settings.json`）写入 `env` 的 `ANTHROPIC_BASE_URL` 与 `ANTHROPIC_MODEL`；**不要把密钥写进任何 settings.json**，`ANTHROPIC_AUTH_TOKEN` 走终端注入或系统密钥管理。
- 验证：`claude` 内 `/status` 检查 Base URL / 认证来源 / 模型；模型不对可 `/model` 查看。`claude doctor` 检查安装。
- 常见错误：
  - **401/认证失败**：确认是 `ANTHROPIC_AUTH_TOKEN`、密钥完整未撤销、终端真的包含该变量、分组支持 Claude Code；`/status` 查认证来源。
  - **404/模型不存在**：Base URL 不加 `/v1/messages`；`ANTHROPIC_MODEL` 与平台 ID 一致；分组支持该模型。
  - **仍走原 Claude 账号**：仅设 Base URL ≠ 替换认证；同一终端须设置 `ANTHROPIC_AUTH_TOKEN` 并完全重启 Claude Code；取消网关环境变量才回到原登录。
- 会话：按项目目录保存；`claude --resume` / `--continue` / `/resume`；Desktop、Web、VS Code 扩展与 CLI 各自维护会话，互不可见。

### 9.3 Kimi Code

- 配置文件：`~/.kimi-code/config.toml`（Windows `%USERPROFILE%\.kimi-code\config.toml`；设了 `KIMI_CODE_HOME` 则用 `$KIMI_CODE_HOME/config.toml`）。**明文保存 API Key，不要上传仓库/外发/截图。**
- **Base URL：`https://surplustoken.com/v1`**（`type = "kimi"` 走对应 OpenAI 兼容协议，地址后不要加具体接口路径）。
- 配置注册三个模型（`[providers."surplustoken-kimi"]` 下填 `api_key`）：

  | 别名 | 实际模型 | 上下文 | 说明 |
  |---|---|---|---|
  | `surplustoken/k3` | k3 | 1,048,576 | 默认模型，thinking=max |
  | `surplustoken/kimi-for-coding` | kimi-for-coding | 262,144 | K2.7 Coding 标准版 |
  | `surplustoken/kimi-for-coding-highspeed` | kimi-for-coding-highspeed | 262,144 | K2.7 Coding 高速版 |

  关键配置：`default_model = "surplustoken/k3"`；`[thinking] enabled=true, effort="max", keep="all"`；模型均带 `thinking/always_thinking/image_in/video_in/tool_use` 能力；k3 额外 `support_efforts=["max"]`。
- 验证：`kimi doctor config` → 项目目录 `kimi` → `/model` 查看三个模型（默认 K3）；改配置后 `/reload` 或重启。
- 常见错误：
  - 找不到 `kimi` 命令：重开终端；macOS/Linux `source ~/.bashrc`（zsh 用 `~/.zshrc`）。
  - Windows 找不到 Git Bash：装 Git for Windows；自定义目录时设 `KIMI_SHELL_PATH` 指向实际 `bash.exe`。
  - `config.toml` 无法解析：`kimi doctor config`；检查英文双引号/标点、Key 引号完整、文件名不是 `config.toml.txt`、表名引号。
  - 401：Key 写在 `[providers."surplustoken-kimi"]` 下（CLI 不读 Shell 环境变量）；无多余空格、未撤销/过期。
  - 模型不存在：分组支持 `k3` / `kimi-for-coding` / `kimi-for-coding-highspeed`。
- 安装：`curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash`（免 Node），或 Node ≥ 22.19.0 时 `npm i -g @moonshot-ai/kimi-code`。

### 9.4 OpenCode

- **Base URL：`https://surplustoken.com/v1`**；Provider 包用 **`@ai-sdk/openai`**（Responses API；不要换成仅面向 `/v1/chat/completions` 的配置）。
- 用户级配置：`~/.config/opencode/opencode.json`（Windows 原生 `%USERPROFILE%\.config\opencode\opencode.json`）。示例：

  ```json
  {
    "$schema": "https://opencode.ai/config.json",
    "model": "surplustoken/gpt-5.6-sol",
    "provider": {
      "surplustoken": {
        "npm": "@ai-sdk/openai",
        "name": "SurplusToken",
        "options": { "baseURL": "https://surplustoken.com/v1" },
        "models": { "gpt-5.6-sol": { "name": "GPT-5.6 Sol" } }
      }
    }
  }
  ```

  模型 ID 不同时同时改 `model` 与 `models` 键。
- 保存密钥：`opencode` → `/connect` → 底部 `Other` → Provider ID 输入小写 `surplustoken`（须与配置完全一致）→ 粘贴 API Key。凭证存入用户数据目录 `auth.json`（**不写进 opencode.json**）；`opencode auth list` 检查。
- 验证：`/models` 选 `SurplusToken → GPT-5.6 Sol`，发「只回复“连接成功”」；控制台出现用量记录。
- 配置优先级：项目根 `opencode.json` 覆盖用户级设置；`OPENCODE_CONFIG` / `OPENCODE_CONFIG_CONTENT` 也会影响。
- 会话：`opencode --continue` / `--session <ID>`；原生 Windows 与 WSL2 数据目录不同，跨环境不自动出现原会话。
- 常见错误：**401** → `opencode auth list` 确认 `surplustoken` 存在、重跑 `/connect`（Provider ID 小写）、Key 完整有效分组正确；**404** → baseURL 为 `/v1`、模型 ID 一致、用 `@ai-sdk/openai`、分组支持；**配置不生效** → 完全重启、查项目级 `opencode.json`、查 `OPENCODE_CONFIG*`、`/models` 看实际加载。

---

## 10. 站点常见问题（FAQ）

- **应该选哪个系统？** 选运行客户端的系统，不是访问文档的设备系统。
- **为什么命令没变化？** 只有包含系统差异的命令块随右上角选项变化；三系统相同时内容不变。
- **API 密钥丢失？** 完整密钥通常无法再次查看；创建新密钥、更新客户端配置、撤销旧密钥。
- **客户端返回鉴权失败？** 确认密钥无多余空格、未被撤销、接入地址正确。
- **客户端无法连接？** 依次检查余额、服务状态、接入地址、API 密钥、本地网络；仍无法解决保留错误信息和时间便于排查。

---

## 11. 文档贡献（写给想改文档的人）

- 仓库：`https://github.com/ypd666/surplusai-docs`，内容在 `docs/`，通过 Pull Request 提交（**不要直接提交 main**）。
- 目录约定：入门/快速开始 → `docs/start/`；平台账号、密钥、计费等功能 → `docs/platform/`；客户端接入 → `docs/clients/`；FAQ → `docs/faq/`；图片 → `docs/public/images/`。
- 普通文档用 `.md`；需要按 Windows/macOS/Linux 切换内容用 `.mdx`；文件名小写英文+连字符。
- 新增页面后编辑同目录 `_meta.json` 加入侧边栏（不带扩展名的文件名）。
- 本地预览：`git clone` → `npm ci` → `npm run dev`；提交前 `npm run format && npm run lint && npm run build`。
- 合并后 Cloudflare 自动重新构建发布；PR 启用分支预览时出现 Preview URL。
- 进阶写法（操作系统切换组件）见仓库 `DOCS_AUTHORING.md`。

### 图片规范要点

- 正文操作截图统一 `<img width="720">`（主题保留 `max-width:100%`，窄屏自动缩小）；不使用 Markdown 图片语法。
- 小图标/徽标可用更小宽度并保持一致；不要把小图放大到 720（应重截清晰原图）。
- `alt` 必须描述图片内容（不用「截图 1」）；图片不含 API Key、密码、邮箱、余额、真实姓名。
- 同操作多文档复用同一图片；截图优先 `.jpg` 或压缩 `.png`，透明背景用 `.png/.svg`；扩展名与格式一致。
- 存放：`docs/public/images/<主题>/`，引用 `/images/<主题>/<file>`；不用本机绝对路径 / `file://`。
- 提交前检查：深浅色主题可辨认、桌面宽度一致、窄窗口不溢出、无敏感信息、路径/扩展名/格式一致、alt 有意义。

---

## 12. 与本项目（Wechat-Agent）的关系

- 本项目 LLM 层（`src/decision/provider/`）是 OpenAI 兼容实现（`LLMProvider.chat` 直接 POST `{base_url}/chat/completions`），密钥从环境变量或 `workspace/.env` 读取——**SurplusToken 的 `https://surplustoken.com/v1` 可作为新的 provider 端点接入**，从而让 agent 走 GPT / Claude / Kimi 等模型（示例模型 ID：`gpt-5.6-sol`、`k3`、`kimi-for-coding` 等，以平台为准）。
- 本项目 `config/runtime.json` 中的 `tool_model: kimi-code/k3`、`tool_model_text: deepseek/v4-pro` 等模型名与 Kimi Code 文档中的「provider/模型」命名风格一致；若接入 SurplusToken，需在 `factory.py` 增加 provider 子类并支持新的模型命名空间。
- 安全提醒与本文一致：API Key 不落库、不提交 Git、单独密钥便于撤销（类似拼车文档建议的独立密钥做法）。

---

## 附：页面清单（抓取来源）

| 页面 | URL |
|---|---|
| 平台介绍 | https://docs.surplustoken.com/start/introduction.html |
| 快速开始 | https://docs.surplustoken.com/start/quick-start.html |
| 注册与登录 | https://docs.surplustoken.com/platform/register.html |
| 创建密钥 | https://docs.surplustoken.com/platform/create-api-key.html |
| 计费与充值 | https://docs.surplustoken.com/platform/billing.html |
| 添加与贡献 OpenAI 账号 | https://docs.surplustoken.com/platform/contribute-account.html |
| 拼车 | https://docs.surplustoken.com/platform/carpool.html |
| API 密钥分组 | https://docs.surplustoken.com/groups/index.html |
| plus/team 分组 | https://docs.surplustoken.com/groups/plus-team.html |
| Codex 接入 | https://docs.surplustoken.com/clients/codex.html |
| Claude Code 接入 | https://docs.surplustoken.com/clients/claude-code.html |
| Kimi Code 接入 | https://docs.surplustoken.com/clients/kimi-code.html |
| OpenCode 接入 | https://docs.surplustoken.com/clients/opencode.html |
| 常见问题 | https://docs.surplustoken.com/faq/index.html |
| 提交文档 | https://docs.surplustoken.com/contribute/index.html |
| 图片规范 | https://docs.surplustoken.com/contribute/image-guidelines.html |
