# 运行时架构详图（2026-08-09 实测版）

> 本文描述**当前代码实际运行时**的组件与数据流（Android 端口），
> 是 docs/ARCHITECTURE.md 总纲的落地详图。以 mermaid 绘制。
> 交互层是重点，按"发现 → 队列 → 旅程 → 端口"四段理解。

## 一、全局三层 + 网关

```mermaid
flowchart TB
    subgraph PHONE["一加6T · 微信 8.0.76（深色模式）"]
        UI[微信 UI]
        NOTIF[系统通知栏]
    end

    subgraph IL["交互层 src/interaction/"]
        DISC["发现组<br/>盯屏/扫描/通知监听"]
        QUEUE["统一时间序队列<br/>UnifiedQueue"]
        JOUR["旅程管理器<br/>JourneyManager"]
        PORT["Android 端口<br/>perception + action + device"]
        READER["SessionReader<br/>+ 消息日志 SQLite"]
        BS["BundleSender<br/>XML 动作包解释器"]
    end

    subgraph DL["决策层 src/decision/"]
        PROXY["Proxy<br/>LLM 唯一对话对象"]
        PROMPT["ContextBuilder<br/>prompt 装配"]
        LLM[("k3 API<br/>api.kimi.com")]
    end

    subgraph TL["工具层（不自研）"]
        KIMI["kimi CLI 子进程<br/>无头 stream-json"]
    end

    GW["网关 Flask :13014<br/>队列/红点/流水/prompt热改"]

    UI -- "adb screencap" --> DISC
    NOTIF -- "dumpsys notification" --> DISC
    DISC --> QUEUE --> JOUR
    JOUR --> READER
    JOUR --> BS
    READER -- "LogUpdated" --> PROXY
    PROXY --> PROMPT --> LLM
    PROXY -- "reply→XML bundle" --> QUEUE
    PROXY -- "task→简报" --> KIMI
    KIMI -- "task_done 回执" --> PROXY
    BS --> PORT --> UI
    GW -.只读快照/流水.- QUEUE
    GW -.只读.- PROXY
```

层间铁律：

- **交互层不组装 prompt、不调 LLM**（多媒体视觉标注例外，视为感知）
- **决策层不碰设备**：出向只有 `submit_bundle(session, xml)` 和 CLI 简报
- **工具层不碰手机**：只做电脑侧任务，交付物落盘回传路径
- 好友申请流程**全程在交互层**，决策层不参与（2026-08-09 用户定）

## 二、交互层 · 发现组（三条通道 + 收口）

```mermaid
flowchart LR
    subgraph W1["通道1 · 持续盯屏 ScreenWatcher（2~4s/帧）"]
        F1["adb 截一帧"]
        F2["detect_page<br/>掩膜快路径 <5ms"]
        F3{"在首页?"}
        F4["build_state 全量解析<br/>会话红点/数字/@我"]
        F5["contacts_tab_has_dot<br/>通讯录tab红点掩膜"]
    end

    subgraph W2["通道2 · 周期扫描 Scanner.sweep（45~90s，队列空才扫）"]
        S1["双击微信tab回顶<br/>解析未读标记"]
    end

    subgraph W3["通道3 · 系统通知 NotifyWatcher（3~6s轮询）"]
        N1["dumpsys notification<br/>解析微信通知"]
        N2["NotificationQueue<br/>已见去重 notify_seen.json"]
        N3["桥接线程 1s"]
    end

    REDIR{"标签 == 新的朋友 ?"}
    PN["push_notify<br/>kind=notify"]
    PF["push_friend<br/>kind=friend"]

    F1 --> F2 --> F3
    F3 -- "否（旅程占用屏幕）<br/>本帧跳过" --> X1(( ))
    F3 -- 是 --> F4 --> REDIR
    F3 -- 是 --> F5 -- "有红点" --> PF
    S1 --> REDIR
    N1 --> N2 --> N3 --> REDIR
    REDIR -- 否 --> PN
    REDIR -- 是 --> PF
```

要点：

- 盯屏与旅程**天然互斥**：屏幕不在首页就跳过，不用锁
- 好友申请的红点生命周期（用户实测）：tab 红点开一次通讯录就消；
  入口行红点开到详情才消；详情点开未通过 → 红点全消但申请还在。
  所以 tab 红点是**即时信号**，另加 30min **兜底巡检**抓残留态
- 乱逛（wander）**不许点通讯录 tab**——会把 tab 红点信号消掉

## 三、交互层 · 统一时间序队列

```mermaid
flowchart TB
    subgraph Q["UnifiedQueue（线程安全 RLock）"]
        E1["kind=notify<br/>会话有未读"]
        E2["kind=action<br/>决策层下发的XML"]
        E3["kind=friend<br/>好友申请"]
    end

    R1["规则1 去重不挪动：<br/>已在队列 → 位置不变，只记 sources"]
    R2["规则2 行动吞并通知：<br/>同会话 notify 升级为 action"]
    R3["规则3 @我/主人插队：<br/>pop_next 按优先级排序"]
    R4["规则4 失败重试：<br/>2次后排队尾，4次硬上限丢弃"]
    R5["快照 queue.json：<br/>每次变更落盘，重启 restore 不丢行动"]

    PN2["push_notify"] --> R1
    PA["push_action<br/>（=Proxy submit_bundle）"] --> R2
    PF2["push_friend"] --> R1
    POP["pop_next"] --> R3
    RQ["requeue_entry"] --> R4
    Q --> R5
```

## 四、交互层 · 旅程（核心状态机）

```mermaid
flowchart TD
    START["pop_next 取条目"] --> K{kind?}

    K -- "friend" --> FR1["friend_requests.accept_all<br/>通讯录→新的朋友→逐条点查看<br/>→完成/添加到通讯录→收尾弹窗"]
    FR1 --> FR2{"ok?"}
    FR2 -- 否 --> FR3["requeue 重试"]
    FR2 -- 是 --> HOME2["回首页（只落日志，<br/>决策层不参与）"]

    K -- "notify/action" --> J1["enter_session<br/>列表查找+搜索兜底"]
    J1 --> JF{"落在好友申请页?<br/>（伪装会话：新申请以<br/>昵称+打招呼出现）"}
    JF -- 是 --> FR1
    JF -- 否 --> J2["_detect_is_group<br/>标题栏(人数)实测"]
    J2 --> J3["铁律1 · 同步日志<br/>sync_session 失败重试2次<br/>再败→标dirty退出补同步"]
    J3 --> J4{"有 action payload?"}
    J4 -- 是 --> J5["BundleSender.submit_bundle"]
    J4 -- 否 --> J6
    J5 --> J6["铁律2 · 行动吸收<br/>处理期间新到的行动一并执行<br/>（保险丝20轮防死循环）"]
    J6 --> J7["铁律3 · 最后一次同步<br/>+ LogUpdated 回传 Proxy<br/>不同步完不许退出"]
    J7 --> HOME1["back_to_home"]
```

## 五、交互层 · 端口（Android 适配器）

```mermaid
flowchart TB
    subgraph ACT["action/ 语义动作"]
        WT["WeChatTools<br/>工具=动作+状态查询"]
        NAV["Navigator<br/>导航薄封装"]
        SND["Sender<br/>拟人发送（标点分段+随机延迟）"]
        QR["quote_reply<br/>长按→引用→发送 八步流程"]
        IMG["image_sender<br/>图片走相册/文件走加号面板"]
        FR["friend_requests<br/>好友申请巡检+通过"]
    end

    subgraph PER["perception/ 感知（只读屏）"]
        OE["ocr_engine<br/>RapidOCR"]
        PD["page_detector<br/>掩膜快路径判页面类型"]
        SB["state_builder<br/>build_state 全量解析"]
        HP["home_parser / chat_parser<br/>scan_parser / icon_detector"]
        RD["Reader<br/>滚动读聊天页+增量同步"]
        SC["Scanner<br/>首页扫描"]
        FB["FrameBus<br/>帧复用总线"]
    end

    subgraph DEV["device/ 设备原语"]
        DC["DeviceCtl<br/>adb 封装"]
        LAY["layout.py<br/>Rect常量表（唯一坐标来源）"]
        RT["random_touch<br/>落点/轨迹随机化"]
        NW["NotifyWatcher"]
    end

    ADB["adb → USB → 手机"]
    IME["ADBKeyBoard<br/>广播输入（不弹键盘）"]

    ACT --> PER
    ACT --> DEV
    DC --> ADB
    DC --> IME
```

纪律：

- **触屏只准 `tap_rect(layout.XXX)` / `swipe_zone`**，裸坐标 tap 视为违规
- 截图即读即删（capture_bytes 内存流转，不落盘）
- perception 只读不写，action 只写不判（页面判断走 perception）

## 六、出向动作执行（BundleSender）

```mermaid
flowchart LR
    XML["XML bundle<br/>（决策层输出）"] --> EX["逐块扫描<br/>坏块只丢自己"]
    EX --> R["<reply> ×≤3"]
    R --> T1["<text> ×N<br/>拆句逐条发（间隔1~3s）"]
    R --> T2["<quote><br/>第一条text带引用发<br/>日志定位方向+滚动查找"]
    R --> T3["<image/> 相册流程"]
    R --> T4["<file/> 加号面板→SAF最近列表"]
    R --> MUTEX["屏幕互斥锁<br/>发送中发现工作顺延"]
```

## 七、决策层 · Proxy 一轮决策

```mermaid
flowchart TD
    EV["事件队列（优先序：<br/>主人 > @我 > task_done > 普通）"] --> H["_decide_session<br/>信号量+会话锁"]
    H --> WM["水位差分<br/>watermarks.json"]
    WM --> FL["过滤：自己的/时间线/系统消息"]
    FL --> MC["媒体转换队列<br/>多媒体截图→k3视觉→描述写回日志"]
    MC --> PB["ContextBuilder 装配prompt<br/>persona + output_protocol + tools<br/>+ session_info/history/new_messages<br/>+ running_tasks"]
    PB --> K3[("k3")]
    K3 --> P["extract_blocks 解析"]
    P --> R1["<reply> → submit_bundle<br/>→ queue.push_action"]
    P --> R2["<task> → TaskLedger登记<br/>→ kimi CLI 子进程"]
    P --> R3["<tool chat_history><br/>同步查日志回灌续生成 ≤3次"]
    P --> R4["<silent/> 沉默"]
    R2 --> DONE["子进程完成<br/>→ task_done 事件"] --> RCPT["回执决策轮<br/>人格化交付（图/文件/转述）"]
    H --> MR["必回场景（私聊/@我/主人）<br/>未回→重试→兜底文案"]
```

## 八、好友申请 · 完整信号流（2026-08-09 定稿）

```mermaid
sequenceDiagram
    participant W as 盯屏/扫描/通知桥
    participant Q as UnifiedQueue
    participant J as JourneyManager
    participant F as friend_requests
    participant P as 手机微信

    Note over W: 即时：tab红点（每帧）<br/>兜底：30min巡检（残留态）
    W->>Q: push_friend（去重不挪动）
    Q->>J: pop_next（kind=friend）
    J->>F: accept_all(tools)
    F->>P: 通讯录→新的朋友
    loop 逐条
        F->>P: 点"查看"→完成/添加到通讯录→收尾
    end
    F->>P: 回首页
    F-->>J: {accepted, remaining, error}
    J->>Q: 失败则 requeue（≤2次）
    Note over J: 结果只落日志<br/>决策层不参与
```

## 九、运行时拓扑（进程/线程）

```mermaid
flowchart LR
    subgraph MAIN["python -m src.main（单进程）"]
        T1["主线程 · InteractionLoop<br/>sweep/分发/旅程/兜底巡检"]
        T2["screen-watch 线程 2~4s"]
        T3["notify-watcher 线程 3~6s"]
        T4["notify-bridge 线程 1s"]
        T5["decision-proxy 线程<br/>事件循环"]
        T6["gateway 线程 :13014"]
        T7["task-* 线程 ×N<br/>kimi CLI 子进程"]
        T8["media-convert 线程池 ×2"]
    end
    FS["workspace/<br/>chatlogs/chatlog.db · queue.json<br/>watermarks.json · proxy_events.jsonl<br/>media/ · tasks/"]
    MAIN --> FS
```

观测面（网关数据源全部只读）：`queue.json`（时序队列）、
`proxy_events.jsonl`（prompt/llm_output/route 流水）、
ops_journal（原子操作）、`/api/home_scan`（首页解析）、
`/api/task_done`（进程外补跑任务的结果注入）。
