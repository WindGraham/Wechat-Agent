# 交互层（Interaction Layer）

> 三端适配器：把 Android / Windows / macOS 三端微信的消息转换成一样的接口
> 暴露给决策层，把决策层的动作翻译成端上操作。
> 本层不调用 LLM，不做回复决策。

## 一、核心职责

### 1. 统一接口（本层存在的唯一理由）

```python
# 上行：三端消息 → 同一个事件结构
MessageEvent(
    session: str,            # 会话名
    is_group: bool,
    sender: str,             # 发送人昵称（群聊逐条识别）
    content: str,            # 文本内容；多媒体为识别后的描述文本
    content_type: str,       # text / image / sticker / quote / system ...
    mention_me: bool,
    source: str,             # notify / sweep / heartbeat / passive
)

# 下行：决策层动作 → 端上操作
ActionRequest(kind="text|image|quote|file", session=..., payload=...) -> ActionResult

# 上下文：决策层随时可读
SessionContext(session) -> list[Message]
```

决策层看到的 Android 私聊和 macOS 群聊没有任何结构差异。

### 2. 端口实现（ports/，每端一份）

| 端口 | 消息来源 | 操作方式 |
|---|---|---|
| `android/` | 截图 + 本地 OCR + 区域切段识别 | ADB/root shell（随机化点击/滑动/广播输入） |
| `windows/` | 桌面微信 UI 树（UIA），截图兜底 | 原生输入事件 |
| `macos/` | Accessibility 树，截图兜底 | 原生输入事件 |

端口内部允许硬编码布局标定（一机一份）。输出必须归一化为 `MessageEvent` /
`ScreenState`——坐标、像素、控件树不出本层。

### 3. 消息发现与读取

- 发现：平台通知监听（近实时）+ 首页未读主动扫描（数字红圈/免打扰红点/
  红色@前缀）+ 首页帧被动识别 + 心跳兜底；统一入通知队列按会话合并
- 读取：聊天页逐条切段（发送人昵称与内容分离），增量续写 + 断档回填
- 多媒体：图片/表情消息在交付决策层前完成多模态标注，content 直接是
  "图片内容：…"的文本
- @我：mentions 提取 + 标记

### 4. 行为伪装

- 点击落点/滑动轨迹/等待时长/发送延迟全部随机化
- 长文本拟人分段发送、模拟打字节奏
- 目的：行为统计上像人，规避风控

### 5. 消息日志

每会话独立持久化（SQLite + 文本导出），幂等续写。这是全系统的记忆底座，
交互层负责写，决策层通过 `SessionContext` 读。

### 6. 运行循环组（整体流程的发动机）

主循环在交互层，因为**循环机制是平台耦合的，每端不一样**：

| 端 | 循环机制 |
|---|---|
| Android | 无推送通道，必须主动轮询：定时 sweep（双击 Tab 扫未读 + 红点补扫）+ 通知轮询 + 心跳兜底 + 空闲乱逛 |
| Windows / macOS | 可订阅 UI 事件/系统通知，事件驱动为主，轻量轮询兜底 |

循环组的职责：

- 驱动发现通道，把"有动静"变成 `MessageEvent` 上报决策层
- 通知队列管理：合并去重、mention 优先、处理中防重入
- 节奏控制：单会话驻留上限、follow-up 补读、暂停/静默状态执行
- 空闲行为：乱逛/退避（行为画像像人）
- 决策层只收到事件，不知道也不关心事件来自轮询还是订阅

## 二、明确不做

- 不调用 LLM（多媒体标注的视觉 API 调用视为感知环节，例外）
- 不决定回不回、回什么
- 不包含人格文案
