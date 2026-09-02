# Proxy 改造设计：多平台输入 + 输出格式插件化

> 目标：将 Proxy 从"微信专用决策中枢"升级为"通用多平台 Agent 决策中枢"，
> 核心思想：**输入抽象化、输出格式插件化、平台能力声明化**。

---

## 一、当前架构问题诊断

### 1.1 输入侧：隐含微信假设

当前 `notify_log_updated()` 只接收 `LogUpdated`（微信专用概念，version = 同步批次号）。
QQ/Discord 等其他平台没有"版本号"概念。

### 1.2 输出侧：硬编码微信 XML 格式

`output_protocol.md` 里全是 `<reply>` / `<image>` / `<sticker>` 等微信概念，
LLM 学到的输出格式与微信强绑定。

### 1.3 路由侧：无平台概念

`submit_bundle` 直接发给微信交互层，没有平台选择。

### 1.4 数据侧：session 无平台前缀

`session = "交流一下？"` 没有平台信息，无法区分 `qq:交流一下？`。

---

## 二、改造目标

将 Proxy 升级为**通用 Agent 决策中枢**：
- 任何平台的消息 → 统一事件 → Proxy 决策 → 按平台格式输出 → 对应平台发送

---

## 三、核心改造点（三个抽象）

### 3.1 输入抽象：MessageEvent

```python
@dataclass
class MessageEvent:
    platform: str              # "wechat" / "qq" / "discord"
    session: str               # 会话标识
    sender: str
    content: str
    content_type: str
    is_group: bool
    is_mine: bool
    mentions: list
    ts: float
    platform_meta: dict        # 平台特有扩展
```

Proxy 新增统一入口 `notify_message(ev: MessageEvent)`，
旧 `notify_log_updated()` 转为兼容层。

### 3.2 输出抽象：OutputFormat

每个平台一个 `OutputFormat` 实现，定义：
- `system_prompt()` → 该平台专用的输出格式说明
- `validate_block()` → 验证块是否符合平台格式
- `route_block()` → 解析路由目标

```python
class WechatXMLOutput(OutputFormat):
    platform = "wechat"
    supports_quote = True
    supports_cross_session = True
    max_reply_per_round = 3
    
class QQXMLOutput(OutputFormat):
    platform = "qq"
    supports_quote = False      # QQ 不支持引用
    supports_cross_session = False
    max_reply_per_round = 5
    max_text_length = 5000
```

### 3.3 路由抽象：SessionID + submit_bundle_map

```python
# 会话标识：platform:session_name
"wechat:交流一下？"
"qq:某QQ群"
"discord:general"

# 多平台出口映射
submit_bundle_map = {
    "wechat": wechat_submit_bundle,
    "qq": qq_submit_bundle,
}
```

---

## 四、Prompt 构建改造

### 4.1 order.txt 支持平台条件

```
[all]
system/persona.md

[platform:wechat]
system/formats/wechat.md

[platform:qq]
system/formats/qq.md

[all]
system/tools.md
```

### 4.2 ContextBuilder 平台感知

```python
def build(self, ..., platform: str = "wechat"):
    fmt = self._formats.get(platform)
    # system prompt = 人格 + 平台格式 + 工具
    system = "\n\n".join([
        persona_text,
        fmt.system_prompt(),
        tools_prompt,
    ])
```

---

## 五、实施步骤

| Phase | 内容 | 验证 |
|-------|------|------|
| 1 | 输出格式插件化 | 微信功能不变，架构可扩展 |
| 2 | 统一事件接口 | 微信走旧接口，内部走新流程 |
| 3 | 加 QQ 入口 | QQ 消息触发决策，输出 QQ 格式 |
| 4 | 会话 ID 平台前缀 | 微信和 QQ 会话不冲突 |

---

## 六、关键决策

1. **保持 XML**：LLM 已学会 XML，平台差异用不同 XML 子集表达
2. **格式代码化**：`OutputFormat.system_prompt()` 返回字符串，可动态计算、可测试
3. **支持跨平台转发**：`<reply session="qq:某群">` 可把微信消息转发到 QQ
4. **人格默认共享**：陈曦在微信和 QQ 是同一个人，支持按平台覆盖

---

## 七、改造后架构

```
Proxy
├── 输入层：MessageEvent（平台无关）
├── 核心层：Decider（平台感知）
│   ├── ContextBuilder（按平台选格式块）
│   ├── Policy（按平台判定策略）
│   └── MemoryService（session 带平台前缀）
└── 输出层：OutputFormat（平台专用）
    ├── WechatXMLOutput
    ├── QQXMLOutput
    └── DiscordJSONOutput（未来）
```

Proxy 成为**通用 Agent 决策中枢**，微信只是其中一个平台实现。
