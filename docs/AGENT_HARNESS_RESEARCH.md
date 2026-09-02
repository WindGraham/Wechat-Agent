# Agent Harness 源码调研：多通道 Agent 架构对比

> 日期：2026-09-02。本报告**基于源码实测**（clone 真实仓库 + 逐文件读代码），
> 不是看 README/文档。目的是评估现有 agent harness 的**完成度**，重点回答：
> **如何接收不同通道的消息？如何处理历史消息？能否跨通道投递？能否热插拔？**
> —— 为 Wechat-Agent 的 Proxy「通用引擎 + 每通道配置 + 统一历史」改造找参照。

---

## 〇、调研对象

| 候选 | clone 仓库 | 规模 | 定位 |
|------|-----------|------|------|
| **hermes-agent** | NousResearch/hermes-agent | ~5046 py | 多平台 bot 网关（微信/QQ/Telegram/Discord/Slack/Signal/元宝...） |
| **clawbolt** | mozilla-ai/clawbolt | ~472 py | 消息总线 + 可插拔通道（Mozilla） |
| **AgentScope** | agentscope-ai/agentscope | ~632 py | 阿里 channel/harness 框架 |
| **Mem0** | mem0ai/mem0 | ~390 py | 跨通道统一记忆库 |
| **LangGraph** | langchain-ai/langgraph | ~450 py (libs) | 通用 agent 引擎 + checkpointer |

---

## 一、hermes-agent —— 最成熟的多平台 bot 网关

**确认的源码事实**：

1. **平台接入**：`gateway/platforms/` 下有大量现成平台适配器：
   `qqbot/`、`weixin.py`（微信）、`whatsapp_cloud.py`、`signal.py`、
   `bluebubbles.py`（iMessage）、`yuanbao.py`（腾讯元宝）、
   `telegram`/`discord`/`slack`/`matrix`/`mattermost` 等。
   → **多通道接收是真实、完整的**，且含中文平台（微信/QQ/元宝），非常贴合你的场景。

2. **每通道一份配置（正是你要的！）**：`gateway/config.py:595` 有
   ```python
   @dataclass
   class ChannelOverride:
       model: Optional[str] = None
       provider: Optional[str] = None
       system_prompt: Optional[str] = None
   ```
   `PlatformConfig` 里 `channel_overrides: Dict[str, ChannelOverride]`（config.py:686），
   注释原文：*"Enables different channels (e.g. Discord #daily vs #dev) to use
   different models and personas **without running separate gateway instances**."*
   → **每个 channel_id 可覆盖 system prompt / 模型 / provider，无需分开实例**。
   这就是你设想的「每通道一份操作配置」。查找逻辑在 run.py:4226 按
   `chat_id → thread_id → session` 逐级匹配。

3. **历史消息处理**：`gateway/` 下有 `memory` 相关（`agent_cache_pressure.py`、
   `delivery_ledger.py`、checkpoint 概念）。hermes 用持久化 session 状态
   （`hermes_state.py` / `hermes_state_*.py` 系列）管理会话历史。

4. **跨通道投递**：有 `delivery.py`（投递）/ `delivery_ledger.py`（投递台账）；
   `channel_directory.py` 统一管理所有平台的会话目录。

**完成度**：**成熟、生产级**（大量平台 + per-channel 配置 + 完整 auth/TTS/media/
缓存基础设施）。它是当前最接近你完整构想的现成系统——尤其"每通道 override prompt"
和"含微信/QQ 中文平台"两点。

**对你的参照**：`ChannelOverride`（model/provider/system_prompt）就是"操作配置"的
最小内核；加平台 = 在 `gateway/platforms/` 加一个适配器。

---

## 二、clawbolt —— 教科书级的"消息总线 + 可插拔通道"

**确认的源码事实**：

1. **统一消息总线**：`backend/app/bus.py:40` `class MessageBus`，有
   ```python
   self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
   self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
   async def publish_inbound(msg)    # 通道 → agent
   async def consume_inbound()       # agent 取
   async def publish_outbound(msg)   # agent → 通道
   async def consume_outbound()      # 通道取
   ```
   + 外加 request/response 关联（`register_response_future(request_id)`）。
   → **这正是你要的"单一 ingress + 出口按来源回传"的完整实现**。

2. **消息模型**：bus.py:28 `InboundMessage` / `OutboundMessage`：
   `channel`/`chat_id`/`content`/`media`/`request_id`。
   → 消息带 `channel` 来源字段，可路由到对应通道。

3. **可插拔通道注册（热插拔）**：`channels/manager.py:21` `class ChannelManager`：
   ```python
   self._channels: dict[str, BaseChannel] = {}
   def register(self, channel): self._channels[channel.name] = channel
   ```
   通道基类 `channels/base.py:67` `class BaseChannel(ABC)` 有
   `name()`/`start()`/`stop()`/`send_text()`/`send_media()`/`send_message()`/
   `handle_webhook_inbound()` 等接口。已实现通道：`webchat/twilio/telegram/
   linq/bluebubbles`。
   → **加通道 = 继承 BaseChannel + manager.register()，核心引擎不动**。

4. **历史消息处理**：`channels/` 有 `channel_state.py`（通道状态）；数据库存于
   `alembic`（含 channel_route 相关表）。历史/会话状态用 DB 持久化。

**完成度**：**成熟**（专注通道总线解耦，核心思路正是"bus-based outbound"——
这与 clawbolt 的演进 issue #264/#564/#576 完全一致且已落地）。

**对你的参照**：`MessageBus`（inbound/outbound 两队列）+ `BaseChannel` 抽象 +
`ChannelManager.register()` 热插拔 → **这就是你要的 Proxy 骨架的最直接模板**。

---

## 三、AgentScope —— schema-driven 的通道配置注册

**确认的源码事实**：

1. **通道注册表（schema-driven，正是你要的！）**：`channel/_registry.py:41`
   `class ChannelTypeRegistry`：
   ```python
   def register(self, channel_cls)          # 注册通道类型
   def schema_of(self, ...)                 # 返回前端 schema
   def create_channel(self, ...)            # 按类型构造
   def list_types(self) -> list[ChannelTypeSchema]
   ```
   → **每个通道类型注册时带 schema**，加通道 = 注册一个带 schema 的类。
   这和你设计的"加来源 = 注册一份带结构的操作配置"完全吻合。

2. **跨通道绑定 / 统一 session**：`channel/_routing.py:41` `def resolve(...)`，
   `ChannelEvent` + `ChannelBinding`，用 `(channel, agent, scope_key)` 生成稳定
   session id（`_SESSION_NAMESPACE = uuid5(...)`）。注释：*"There is no persisted
   channel→session mapping table... both the target agent and the session id"*。
   → **跨通道用同一 `(channel, agent, scope_key)` 绑定会话**，正是"统一历史分片"的关键。

3. **通道实现**：`channel/_feishu/`、`channel/_discord/`、`channel/_dingtalk/`；
   `channel/_gateway.py`（网关）、`channel/_dispatcher.py`（分发）、
   `channel/_stream.py`、`channel/_clients.py`、`channel/_routing.py`。

4. **历史消息**：`app/storage/_sql/` + `_model/_channel.py`（通道模型，DB 持久化）。

**完成度**：**成熟**（有完整 Channel/{registry,gateway,dispatcher,routing,stream} 子系统，
含第三方平台 + schema 配置）。

**对你的参照**：`ChannelTypeRegistry.register(schema)` + `ChannelBinding` 的
`(channel, agent, scope_key)` 绑定 → **怎么把"每通道配置"做成带 schema 的注册、
怎么把不同的通道映射到稳定的统一会话**。

---

## 四、Mem0 —— 跨通道统一记忆（但不是完整历史）

**确认的源码事实**：

1. **统一记忆库**：`memory/main.py:487` `class Memory(MemoryBase)`；
   `self.db = SQLiteManager(self.config.history_db_path)`（main.py:500）。
   → 一个 SQLite 存全部记忆。

2. **分片键（区分通道/会话）**：main.py:314 `_build_filters_and_metadata`：
   ```python
   user_id: Optional[str]
   agent_id: Optional[str]
   run_id: Optional[str]
   base_metadata_template = _strip_identity_keys(deepcopy(input_metadata), ...)
   ```
   **身份键（user_id/agent_id/run_id）由实体参数设置，禁止通过 metadata 设置**。
   → **每个会话/通道用 user_id/agent_id/run_id 分片 + metadata 自由拓展**，
   正是"不同通道的对话按身份隔离、metadata 记通道"。

3. **历史 → 记忆的转化**：`add()` 接受 str/dict/list[dict]（main.py:780），
   内部 `extract_memories` 提炼成记忆条目，调 `self.db.batch_add_history(...)`
   （main.py:1077）+ `add_history(memory_id, ...)`。

4. **关键区别**：Mem0 的 `history()`（main.py:1946）是按 **memory_id** 查
   **记忆的变更历史**（UPDATE/ADD 等操作），**不是完整的对话消息流水**。
   → **Mem0 存"提炼后的记忆 + 记忆变更史"，不是"原始对话原文"**。

**完成度**：**成熟**（记忆库生产级，多向量存储后端 + REST server + CLI + 多语言 SDK）。

**对你的参照 / 你的差异化**：Mem0 证明了"一个 agent 记住所有通道"是对的，但它是
**记忆库**不是**历史库**——它不留完整对话原文，只留提炼记忆。而你的构想是
**"统一历史库"（存完整对话）+ 记忆**。这是你的差异化优势：**Mem0 只解决"记住",
你要的是"完整上下文可回看 + 跨通道"**。

---

## 五、LangGraph —— 通用引擎 + checkpointer（历史存储）

**确认的源码事实**：

1. **通用 agent 引擎**：`libs/langgraph/` 下 graph/state/pregel 编排，`libs/prebuilt/` 预置。

2. **历史/状态存储（checkpointer）**：`libs/checkpoint-postgres/`、
   `libs/checkpoint-sqlite/`。SQLite saver（`checkpoint-sqlite/.../sqlite/aio.py:319`）：
   ```sql
   CREATE TABLE checkpoints (
       thread_id TEXT NOT NULL,
       checkpoint_ns TEXT NOT NULL DEFAULT '',
       checkpoint_id TEXT NOT NULL,
       ... PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
   )
   ```
   → **历史按 `thread_id + checkpoint_ns + checkpoint_id` 存**，`metadata` 可带任意字段。

3. **多通道**：LangGraph 核心是通用引擎，本身**不内置微信/QQ 等通道适配器**；
   通道接入要靠自己写（或 `libs/langgraph-platform`）。它有 `interrupt`/`send` 边
   做多 agent / 状态路由。

**完成度**：**引擎成熟，但"多通道"不是它的原生强项**——它有强大的通用编排 + 
checkpointer（历史存储），但通道适配要自己搭。

**对你的参照**：`BaseCheckpointSaver` + `thread_id` 分片 → **"统一历史库"怎么设计
（一张表存所有会话历史，靠 thread_id/metadata 分片）**。LangGraph 的 checkpointer
就是你"统一历史库"的最佳参照物。

---

## 六、横向对比表

| 维度 | hermes-agent | clawbolt | AgentScope | Mem0 | LangGraph |
|------|-------------|----------|------------|------|-----------|
| **语言** | Python | Python | Python | Python | Python |
| **多通道接收** | ✅ 超全(微信/QQ/Telegram...) | ✅ 5+通道 | ✅ 3+通道 | ❌ 无 | ⚠️ 需自建 |
| **每通道一份配置** | ✅✅ `ChannelOverride` | ✅ 通道state | ✅✅ schema注册 | ⚠️ 仅metadata | ⚠️ 仅config |
| **统一消息总线** | ✅ gateway | ✅✅ `MessageBus` | ✅ dispatcher | ❌ | ❌ 无 |
| **历史存储** | ✅ session/state | ✅ DB | ✅ DB | ✅ 记忆库 | ✅✅ checkpointer |
| **存完整对话原文** | ✅ | ✅ | ✅ | ❌ 只留记忆 | ✅ checkpoint |
| **跨通道投递** | ✅ delivery | ✅ bus | ✅ routing | ❌ | ⚠️ send边 |
| **热插拔加通道** | ✅ 加适配器 | ✅✅ register() | ✅ register(schema) | n/a | 需自建 |
| **完成度** | 生产级 | 成熟 | 成熟 | 生产级(记忆) | 引擎成熟/通道弱 |

---

## 七、结论：哪个最贴合你的构想

你的构想要点：**通用引擎 + 每通道一份操作配置 + 统一历史库 + 跨通道投递 + 热插拔**。

### 单点最贴合

- **「每通道一份操作配置」** → **hermes-agent 的 `ChannelOverride`** 最直接
  （system_prompt/model/provider，注释明确说"不同 channel 不同人设，无需分开实例"）。
- **「统一消息总线 + 热插拔通道」** → **clawbolt 的 `MessageBus` + `BaseChannel` +
  `ChannelManager.register()`** 是教科书级模板。
- **「统一历史库分片」** → **LangGraph 的 `BaseCheckpointSaver`（thread_id 分片）**
  是最佳参照。
- **「跨通道统一记忆」** → **Mem0**（但它是记忆库，不是完整历史库）。

### 但没有一个同时满足全部 5 点

| 候选 | 缺哪点 |
|------|--------|
| hermes | 历史存储非"统一库"（每平台 session 各自）；热插拔要写完整适配器 |
| clawbolt | 历史用 DB 但专注通道总线；无 per-channel prompt 覆盖 |
| AgentScope | 跨通道绑定好，但 platform 覆盖不像 hermes 那么"每通道 prompt" |
| Mem0 | 不存完整历史；不是 agent 引擎 |
| LangGraph | 通道适配要自建；per-channel prompt 非原生 |

### 结论

**没有一个现成 harness 同时做到你要的"统一历史库 + 每通道操作配置 + 跨通道投递 +
热插拔"——你的构想在整合这三者上，确实比现有方案更进一步。**

但**每个单点你都能找到成熟参照**：
- **引擎形态** → 学 clawbolt 的 `MessageBus` + `BaseChannel` 热插拔
- **配置形态** → 学 hermes 的 `ChannelOverride`（每通道 override prompt/model）
- **历史/会话分片** → 学 LangGraph 的 checkpointer（thread_id 分片）+ Mem0 的身份键
  分片（user_id/agent_id/run_id + metadata）
- **统一历史** → 这是你的差异化（现成的都是记忆库或各自 session，没有"一个库装所有
  平台完整对话"）

**所以最合理的做法不是"抄某一个"，而是"融合这四家 + 你自己的统一历史库创新"**：
```
Proxy 引擎        ← clawbolt 的 MessageBus 形态（单一 ingress + 按来源 outbound）
每通道操作配置     ← hermes 的 ChannelOverride 形态（per-channel prompt/model/provider）
统一历史库        ← LangGraph checkpointer 分片 + Mem0 身份键分片（source+session 维度）
跨通道投递        ← 你的构想：改 prompt 让 LLM 选目标 channel（<reply source=...>）
热插拔           ← clawbolt BaseChannel register() / AgentScope schema 注册
```
