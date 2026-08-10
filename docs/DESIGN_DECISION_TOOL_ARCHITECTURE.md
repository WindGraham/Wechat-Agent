# 决策层工具架构定稿（2026-08-10）

> 决策层主动工具收敛为 **2 个**：`memory` + `websearch`。
> 其余能力（群聊天记录 / 文件内容 / 自动记忆 / 时间 / 任务状态）全部走
> prompt 注入，不做工具。本文是实现的蓝图，配套设计见
> DESIGN_DECISION_FEATURES_MEMORY.md。

## 一、总览：2 个主动工具

| 工具 | 作用 | 延迟 | 异步 | 数据源 | 状态 |
|---|---|---|---|---|---|
| **memory** | 读写长期记忆（偏好/关系/承诺） | 毫秒（本地） | 同步 | memory 存储 | 待实现 |
| **websearch** | 本地记录 + 网络搜索 | 秒级 | **异步（子线程）** | 网络 + 消息库/memory | 待实现 |

## 二、不做工具的（走 prompt 注入）

| 能力 | 注入方式 | 状态 |
|---|---|---|
| 各群聊天记录 | history 聚合（get_context） | ✅ 已有 |
| 时间/日期 | session_info 块预置 | ✅ 已有 |
| 任务状态 | running_tasks 块预置 | ✅ 已有 |
| 文件内容 | 转换写回 content → history | 规划中（感知层） |
| 自动记忆检索 | 决策前系统注入【相关记忆】块 | 规划中（系统层） |

## 三、memory 工具设计

### 3.1 prompt 措辞（tools.md 新增）

```markdown
## memory — 读写长期记忆

你有一个长期记忆库，记录值得记住的事（用户偏好/关系/重复话题/承诺）。
什么时候用：
- 用户说了值得记的事 → op=add
- 需要回忆"之前说过的事" → op=read / search
怎么调：
<tool name="memory" op="add" key="风图" value="不喜欢表情包"/>
<tool name="memory" op="read" key="风图"/>
<tool name="memory" op="delete" id="f1"/>
边界：
- 宁可少记，不记垃圾；拿不准就不记
- 读到的记忆用于回应，不主动复述来源会话
```

### 3.2 存储结构（memory 文件）

```json
{
  "facts": [
    {
      "id": "f1",
      "key": "风图",
      "content": "不喜欢表情包",
      "scope": "global | session",
      "session": "私聊",
      "source": "私聊",
      "ts": 1786000000,
      "updated_at": 1786000000,
      "confidence": 0.9
    }
  ]
}
```

### 3.3 受限操作（agent 不直接编辑文件）

```
op=add      → 追加条目（系统去重/上限/置信度）
op=read     → 按 key 查 → 返回条目
op=search   → 按关键词检索 memory
op=delete   → 按 id 删（id 是稳定句柄，不按内容匹配）
维护（系统负责）：去重、上限（每用户 N 条）、淘汰（旧+低置信）、来源标注
```

### 3.4 proxy 实现点

```
proxy.py _exec_tool 恢复 → 按 name 分发
memory → add/read/search/delete 实现
结果回灌 tool_feedback（现有机制）
```

## 四、websearch 工具设计

### 4.1 prompt 措辞（tools.md 新增）

```markdown
## websearch — 查资料/搜索

需要查资料、核实信息、找东西时用。
怎么调：
<tool name="websearch" query="山城老灶 火锅 评价" scope="web"/>
scope: web(网络) | local(本地记录) | all(默认，都查)
返回：本地记录 + 网络结果，分两段标注
边界：
- 搜索是异步的，结果回来后你会收到；可以先回复"我去查一下"
- 查不到就如实说，不编造
- 深度任务（下载/分析）→ 委派 <task>
```

### 4.2 执行流程（本地同步 + 网络异步）

```
LLM 输出 <tool name="websearch" query="..." scope="..."/>
  ↓
proxy 执行：
  ① local 段：查消息库 + memory（毫秒，同步）→ 立即可回灌
  ② web 段：起子线程搜网络（秒级，异步）→ 不阻塞事件循环
  ↓
合并回灌：
  【本地记录】[特高课 8/5] 风图: 那家店叫山城老灶
  【网络结果】山城老灶火锅, 评分 4.5 (来源: 大众点评)
  ↓
超时降级：web 段超时 → 只给本地结果 + "网络搜索超时，可委派"
```

### 4.3 异步机制（复用现有 task 线程模式）

```
proxy.py 已有先例：_start_task 起 threading.Thread 跑 CLI 任务
websearch web 段复用同一模式：
  threading.Thread(target=search_web, daemon=True).start()
  完成 → _push_event(SEARCH_DONE, 结果) → 回灌该会话续生成

关键：
  - 搜索期间主循环继续处理其他会话（proxy 不干等）
  - 搜索期间同会话不触发新决策（等待标记，防混乱）
  - 搜索限并发（如同时 2 个）
```

### 4.4 proxy 实现点

```
新增 SEARCH_DONE 事件类型（或复用 TASK_DONE 通道）
新增 web 搜索后端（可插拔：轻量搜索 API / CLI）
_exec_tool 分发 websearch → local 段同步 + web 段异步
```

## 五、prompt 架构调整（与 2 工具配套）

```
system/tools.md:
  # 可用工具
  ## 行动准则（委派纪律，已有）
  ## delegate_task（已有，不动）
  ## memory — 读写长期记忆（新增）
  ## websearch — 查资料/搜索（新增）

system/output_protocol.md:
  <tool> 块的完整定义（参数/转义/回灌说明，已有协议补全）

system 新增【信息边界】：
  memory/websearch 涉及跨会话 → 防泄漏规则（措辞强硬，放 system）

user 新增【相关记忆】块模板：
  自动注入（非工具），与 memory/websearch 配合
```

## 六、实现顺序

```
第 1 步: memory 工具（prompt + proxy 实现）—— 独立闭环，最快见效
第 2 步: websearch 工具（prompt + proxy 实现 + 异步线程）—— 复用 task 模式
第 3 步: 自动检索注入【相关记忆】（系统层，与 memory 共用检索）
第 4 步: 文件内容转换（感知层，进 history）—— 非工具
第 5 步: prompt caching（全部定稿后）
```

## 七、关键设计原则（回顾）

1. **越少越好**：能拼 prompt 注入的绝不做工具
2. **感知转换不进决策**：文件内容/图片描述在传入前转好
3. **不阻塞决策**：慢操作（web 搜索）子线程异步，主循环继续处理其他会话
4. **不靠 LLM 自觉**：自动记忆注入是系统层，不依赖模型主动调
5. **防泄漏**：memory/websearch 结果带来源，system 级【信息边界】规则

## 八、参考

- 能力边界原则与封顶判据：DESIGN_DECISION_FEATURES_MEMORY.md §一
- 记忆系统设计：DESIGN_DECISION_FEATURES_MEMORY.md §五
- 搜索机制：DESIGN_DECISION_FEATURES_MEMORY.md §四
- 业界对照（Letta/Mem0/RAG）：DESIGN_DECISION_FEATURES_MEMORY.md §六
