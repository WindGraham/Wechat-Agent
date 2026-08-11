# 特殊 Prompt 与朋友圈自发流程设计（2026-08-11）

> 目标：让 agent 除了被动回复消息外，还能按概率触发“后台任务型”决策，
> 例如周期性整合记忆、写猫娘日记并发朋友圈。
> 核心思路：**scheduler 投硬币决定何时醒 → proxy 用专用 prompt 决定干什么 →
> 输出按模式路由到工具层/记忆层/文件层。**

## 一、背景与动机

当前 agent 是“消息驱动”的：只有某个会话日志更新时，proxy 才调 LLM 做决策。
这带来两个问题：

1. **记忆工具调用意愿弱**：只有当聊天里恰好需要回忆时，agent 才会主动用 memory；
   大量值得沉淀的日常信息没人整理。
2. **没有自发表达能力**：朋友圈/日记等“无人触发也要做”的行为没有入口。

本设计引入 **Special Prompt（特殊提示词）** 机制：每份特殊 prompt 是一份独立的
system prompt + 调度配置，由 scheduler 按泊松/指数间隔触发，走 proxy 的专用
决策路径，输出按固定模式处理。

---

## 二、总体架构

```
┌──────────────────────────────────────┐
│         SpecialScheduler             │
│  读 runtime.json，维护每个特殊 prompt  │
│  的下次触发时间；每分钟 tick 一次       │
└──────────────┬───────────────────────┘
               │ EV_SPECIAL_RUN
               ▼
┌──────────────────────────────────────┐
│           Proxy._handle()            │
│    → SpecialRunHandler.handle()      │
│      1. 加载特殊 prompt              │
│      2. 收集上下文                   │
│      3. 调 LLM（专用 system prompt）  │
│      4. 按 output_mode 解析输出      │
└──────────────┬───────────────────────┘
               │
     ┌─────────┼─────────┐
     ▼         ▼         ▼
  memory      task      text
  工具执行    工具层     保存到文件/memory
```

---

## 三、与现有机制的关系

| 现有机制 | 位置 | 本设计如何复用 |
|---|---|---|
| 事件注入 | `Proxy.inject_aside()` / `warm_memory()` | scheduler 注入 `EV_SPECIAL_RUN` |
| 事件处理器热插拔 | `decision/proxy/handlers/` | 新增 `special_run.py` |
| 专用 prompt | `ContextBuilder.build_warmup()` | 新增 `build_special(name, context)` |
| Prompt 文件管理 | `config/prompts/` | 新增 `config/prompts/special/` |
| 网关配置 | `/api/runtime` | 扩展 `runtime.json` schema |
| 记忆读写 | `MemoryTool` / `MemoryInjector` | special prompt 读/写记忆 |

---

## 四、Special Prompt 文件格式

存放路径：`config/prompts/special/<name>.md`

采用 YAML frontmatter + Markdown body：

```markdown
---
name: cat_diary
output_mode: task        # memory | task | text
rate_per_day: 1.0        # 日均触发次数
active_hours: [9, 23]    # 允许触发的小时区间 [start, end]
target: moments          # output_mode=task 时，交给工具层的目标标识
---

# 猫猫日记模块

你是陈曦的猫猫日记书写人格。当前时间：{time}。

## 今日素材

{context}

## 任务

1. 阅读上述素材，写一篇 50~150 字的猫娘日记，语气软、自然、像真人朋友圈。
2. 日记内容不要暴露自己是 AI/程序。
3. **只输出一个 <task> 块**，让后台执行核心帮你把日记发到微信朋友圈。
4. 禁止输出 <reply>、<text>、<silent/> 或任何解释性文字。

输出示例：

<task session="__moments__" ref="" desc="发今日猫娘日记" deliver="reply">
打开微信朋友圈，发布文字动态：
"今天阳光软乎乎的，趴在窗台上晒了半天。主人忙的时候我就安静陪着，
偶尔喵一声提醒她喝水。希望明天也能这么舒服呀~"
</task>
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | 是 | 标识符，与文件名一致 |
| `output_mode` | str | 是 | `memory` / `task` / `text` |
| `rate_per_day` | float | 是 | 日均触发次数，例如 `1.0` |
| `active_hours` | [int, int] | 否 | 默认 `[0, 24]` |
| `target` | str | `task` 模式建议有 | 目标动作标识，如 `moments` |

---

## 五、Scheduler：投硬币器设计

### 5.1 模型选择

**方案 A：每分钟 Bernoulli（最符合“投硬币器”直觉）**

```python
p = rate_per_day / 1440
if random.random() < p:
    push_special_run(name)
```

- 优点：实现最简单，与 run_loop 的分钟 tick 天然对齐。
- 缺点：可能连中两发，也可能连续多天不触发。

**方案 B：指数间隔（推荐）**

```python
lambda_per_sec = rate_per_day / 86400
next_at = last_trigger + random.expovariate(lambda_per_sec)
```

每分钟 tick 检查 `now >= next_at`，到期触发并重新抽样。

- 优点：间隔分布更均匀，不会出现极低概率的极端聚集。
- 缺点：需要维护每个 prompt 的 `last_trigger` / `next_at` 状态。

**结论**：采用方案 B，但 scheduler 仍保持“每分钟投一次是否到期”的硬币语义，
真正的随机性体现在等待间隔上。

### 5.2 位置与调用

`SpecialScheduler` 建议放在 **proxy 内部**：

- 初始化：`Proxy.__init__()` 中创建
- tick：`Proxy.run_forever()` 每轮 poll 时调用 `scheduler.tick()`
- 触发：scheduler 直接调用 `self._push_event({"type": EV_SPECIAL_RUN, ...})`

这样不占用交互层线程，也避免跨线程事件传递。

### 5.3 状态持久化

scheduler 需要持久化 `last_trigger` 和 `next_at`，防止 agent 重启后节奏错乱。
建议存到 `workspace/runtime/special_scheduler.json`：

```json
{
  "cat_diary": {
    "last_trigger": 1786350301,
    "next_at": 1786358942
  },
  "memory_consolidation": {
    "last_trigger": 1786348800,
    "next_at": 1786363200
  }
}
```

---

## 六、Proxy 事件与 Handler

### 6.1 新增事件类型

在 `decision/proxy/events.py` 中：

```python
EV_SPECIAL_RUN = "special_run"

_PRIORITY = {
    "owner": 0,
    "mention": 1,
    EV_TASK_DONE: 2,
    EV_ASIDE: 0,
    EV_SPECIAL_RUN: 4,    # 低于主人/@我/任务回执，高于普通消息
}
```

### 6.2 事件 payload

```python
{
    "type": EV_SPECIAL_RUN,
    "prompt_name": "cat_diary",
    "session": "__special_cat_diary__",   # 伪会话，用于会话锁
    "ts": time.time(),
}
```

伪会话名避免与真实聊天会话冲突，也避免特殊任务阻塞正常回复。

### 6.3 Handler 注册

新增 `decision/proxy/handlers/special_run.py`：

```python
from ..registry import EventHandler, register_handler
from ..events import EV_SPECIAL_RUN

@register_handler
class SpecialRunHandler(EventHandler):
    event_type = EV_SPECIAL_RUN

    def handle(self, proxy, ev):
        proxy._run_special(ev["prompt_name"], ev.get("session"))
```

### 6.4 Proxy 主方法

```python
def _run_special(self, prompt_name: str, session: str):
    spec = load_special_spec(prompt_name)
    if spec.output_mode == "memory":
        self._run_memory_consolidation(prompt_name, session)
    elif spec.output_mode == "task":
        self._run_special_task(prompt_name, session)
    elif spec.output_mode == "text":
        self._run_text_special(prompt_name, session)
```

---

## 七、三种输出模式

### 7.1 memory 模式：周期性记忆整合

**用途**：解决 agent 不主动调用 memory 的问题。

流程：
1. scheduler 触发（如 `rate_per_day: 0.5`，约每 2 天一次）
2. handler 加载 `memory_consolidation.md`
3. 收集最近 24~48h 的跨会话聊天历史（分批）
4. 调用 LLM，system prompt 强制：**只输出 `<tool name="memory" .../>`**
5. handler 只执行 memory 工具块，忽略 reply/task

与 `build_warmup()` 的区别：
- `build_warmup()` 是启动时一次性、按会话分批；
- memory consolidation 是周期性、跨会话、后台静默执行。

### 7.2 task 模式：猫猫日记 → 发朋友圈

**用途**：生成内容并委派工具层执行。

流程：
1. scheduler 触发 `cat_diary`
2. handler 收集上下文：
   - 最近 24h 记忆（memory search）
   - 今日聊天高光（各会话自己的消息 / 被 @ / 有趣片段）
   - 最近 7 天日记（避免重复）
   - 当前时间
3. 调用 LLM，system prompt = `cat_diary.md`
4. 解析 `<task>` 块
5. 先保存日记：
   - 文件：`workspace/memory/cat_diary/2026-08-11.md`
   - 记忆：global memory `2026-08-11 猫娘日记：...`
6. 再把 task 交给工具层，由后台 CLI 完成发朋友圈

### 7.3 text 模式：纯文本记录

**用途**：生成一段文字并保存，不触发动作。

流程：
1. scheduler 触发
2. handler 调用 LLM
3. 取原始文本输出
4. 保存到指定路径（由 prompt metadata 或 handler 固定）

---

## 八、ContextBuilder 扩展

新增方法：

```python
def build_special(self, prompt_name: str, context: dict) -> list:
    """组装特殊 prompt 的 messages。

    context: 由 handler 准备的结构化上下文，例如：
        {
            "time": "2026-08-11 15:30 周一",
            "memories": [...],
            "highlights": [...],
            "recent_diaries": [...],
        }
    """
    spec = self._lib.load_special(prompt_name)
    system = spec.system_prompt
    user = self._render_special_context(prompt_name, context)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
```

`PromptLibrary` 需要新增 `load_special(name)`，读取 YAML frontmatter 并返回 dataclass。

---

## 九、朋友圈 UI 动作（P4 实现）

cat_diary 目前依赖 `<task>` 交给工具层执行，因此需要在工具层新增
“发朋友圈”能力。

### 9.1 建议新增 action 模块

`src/interaction/ports/android/action/moments_poster.py`（**已实现并真机验证**）

核心流程：
1. 唤醒设备 + 拉起微信
2. 确保在微信首页
3. 点击“发现” tab
4. 点击“朋友圈”
5. 长按右上角相机图标 → 触发纯文字发表页
6. 处理首次“我知道了”实验功能提示
7. 输入文案（ADBKeyBoard 广播）
8. 点击“发表”
9. 页面离开发表页 / 回到朋友圈 feed → 确认发表成功

### 9.2 需要扩展的感知能力

- `page_detector.py`：识别“发表朋友圈”页面
- `state_builder.py`：返回朋友圈编辑页可用动作

### 9.3 先走 task 模式的原因

在 action 层没 ready 之前，cat_diary 的 `<task>` 可以先由后台 CLI 打印到日志/文件，
不真发朋友圈；等 action 实现后，同一个 prompt 不用改，只需工具层支持执行。

---

## 十、Runtime 配置

### 10.1 runtime.json 新增字段

```json
{
  "special_prompts": {
    "cat_diary": {
      "enabled": true,
      "rate_per_day": 1.0,
      "active_hours": [9, 23]
    },
    "memory_consolidation": {
      "enabled": true,
      "rate_per_day": 0.5,
      "active_hours": [0, 24]
    }
  }
}
```

注意：prompt 正文仍放在 `config/prompts/special/`，runtime.json 只放调度开关。

### 10.2 网关 schema

`src/gateway/app.py` 中 `_RUNTIME_FIELDS` 需要支持嵌套校验，或新增一个独立的
`SPECIAL_PROMPT_SCHEMA`，由 `config.py` 的 `_validate_runtime` 调用。

### 10.3 热重读

当前 `RuntimeConfig.check()` 全代码库无人调用，配置改动不会热生效。
本设计必须在 scheduler 的 `tick()` 开头调用 `self._runtime.check()`，
这样网关改 `runtime.json` 后下一分钟就能生效。

---

## 十一、关键设计原则

1. **特殊 prompt 不阻塞正常聊天**：使用伪 session + 较低优先级。
2. **输出必须严格受限**：每份 special prompt 的 system 里写死允许输出的块类型，
   handler 解析时只认白名单内的块。
3. **先保存再发布**：日记先生成文件/记忆，再发朋友圈；发朋友圈失败不丢失内容。
4. **可观测**：每次 special run 写入 `proxy_events.jsonl`（start/end/output/executed）。
5. **成本可控**：默认 rate 低（1/天、0.5/天），避免频繁 LLM 调用。
6. **不靠 LLM 自觉**：记忆整合由 scheduler 强制触发，而不是等 agent 想起。

---

## 十二、实现阶段

| 阶段 | 内容 | 主要改动文件 |
|---|---|---|
| P1 | Special prompt 框架：事件类型、handler、scheduler、配置读取 | `events.py`, `handlers/special_run.py`, `proxy.py`, `prompt/builder.py`, `prompt/library.py`, `gateway/app.py` |
| P2 | 记忆整合 special prompt | `config/prompts/special/memory_consolidation.md`, `ContextBuilder.build_special()` 上下文收集 |
| P3 | 猫猫日记 special prompt + 日记持久化 | `config/prompts/special/cat_diary.md`, 文件/memory 写入 |
| P4 | 自动发朋友圈动作层 | ✅ 已实现：`src/interaction/ports/android/action/moments_poster.py`（任意起点、RandomTouch、真机验证通过，测试朋友圈已清理） |
| P5 | 状态/页面识别扩展（可选） | 如需要，可在 `page_detector.py` / `state_builder.py` 增加朋友圈编辑页类型 |

---

## 十三、风险与待决策事项

1. **日记是否带图？** 带图需要从相册/网络选图，任务简报和 UI 动作都会复杂很多。
2. **朋友圈发文字还是图文？** 微信“长按相机”发文字，“点相机”发图文，入口不同。
3. **日记是否私发给主人备份？** 可在 prompt 里让 LLM 输出两个 `<task>`：一个发朋友圈，一个私发。
4. **context 大小控制**：cat_diary 需要“大量信息”，但 prompt 不能无限膨胀，需要摘要/筛选。
5. **失败重试策略**：发朋友圈失败是否重试？重试间隔多少？

---

## 十四、参考

- 现有专用 prompt 先例：`src/decision/prompt/builder.py::build_warmup()`
- 事件处理器注册：`src/decision/proxy/handlers/__init__.py`
- 网关配置 API：`src/gateway/api/config.py`
- 运行配置加载：`src/shared/runtime.py`
- 朋友圈动作实现：`src/interaction/ports/android/action/moments_poster.py`
- 朋友圈动作单测：`tests/test_moments_poster.py`
