# 交互层（Interaction Layer）

> 微信的一切进出 + "像人"的全部学问。对上（决策层）只暴露语义化接口，
> 坐标、像素、平台细节全部封在本层。

## 一、职责清单

### 1. 平台端口（ports/）——三端差异的唯一藏身地

每个端口实现同一组抽象：

```python
class Port:
    def observe(self) -> ScreenState: ...   # 当前屏幕 → 标准化状态（页面/元素/消息）
    def act(self, action: UiAction) -> ActionResult: ...  # tap/long_press/swipe/type/back
    def notify_events(self) -> list[Event]: ...           # 平台侧新消息信号（通知栏等）
```

- `android/`：截图 + 本地 OCR + 区域标定 + ADB/root shell 操控
  （感知采用区域切段方案：首页分割线切段 + 聊天页头像顶切段，
  操控全部带随机化扰动，布局常量两态标定）
- `windows/`：桌面微信 UI 树（UIA）优先，截图兜底——桌面端无节点混淆，比安卓好做
- `macos/`：Accessibility API 优先，截图兜底

端口内部允许硬编码标定（一机一份），但输出必须是标准化 `ScreenState`。

### 2. 消息发现（什么时候"有动静"）

四通道互补，产物统一进通知队列（按会话粘滞合并）：

| 通道 | 说明 |
|---|---|
| 通知监听 | 平台通知栏（Android dumpsys / 桌面端通知 API），近实时，@我 预判 |
| 主动扫描 | 定时回首页解析未读：数字红圈、免打扰红点、红色 @ 前缀；含沉底补扫 |
| 被动识别 | 任何一次首页帧都自动收编红点/未读入队 |
| 心跳兜底 | 低频全量拍帧，防主通道失效 |

### 3. 消息读取与标注

- 聊天页切段读取：头像顶切段 → 发送人昵称（限高不限宽条带）/内容/归属/时间
- 增量续写 + gap 回填（msg_log 幂等）
- **多媒体即时标注**：图片/表情等消息在交付决策层之前裁图送多模态模型，
  描述写回消息日志——决策层看到的永远是"图片内容：…"而非 [图片]
- @我 消息标记（mentions 提取 + [@我] 标注）

### 4. 人格与伪装

- 人格卡加载与合并（底座 + 会话卡 + 群类型卡），说话风格约束
- 行为随机化：点击落点、滑动轨迹、等待时长、回复延迟全部带扰动
- 回复切片发送（长文本拟人分段）

### 5. 消息日志（记忆底座）

- 每会话独立持久化（SQLite + 文本导出），msg_uid 幂等
- 供决策层读取上下文，供多媒体标注写回

### 6. 动作出口

接收决策层的 `ActionRequest`（发送文本/图片/引用/文件等），转成端口动作序列。
**剧本优先**：已验证的确定性流程（如发图）直接执行；失败或场景未覆盖 → 生成
`TaskBrief` 移交决策层任务模式，不硬撑。

## 二、对上接口（本层唯一的出口）

```python
# 产出
MessageEvent(session, sender, content, content_type, mentions, media_desc, source)
SessionContext(session) -> list[Message]      # 含 [@我] / 多媒体标注
# 接收
ActionRequest(kind="text|image|quote|file", session, payload) -> ActionResult
```

## 三、明确不做

- 不调用 LLM（唯一的例外通道：多媒体标注用多模态 API，视为感知的一部分）
- 不做回复决策（只提供事件和上下文）
- 不包含任何平台无关业务逻辑（那是决策层的事）
