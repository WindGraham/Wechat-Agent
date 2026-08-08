# 工作区与目录规划

> 仓库目录与运行时工作区的完整规划。原则：代码进 git，数据进 workspace/（gitignore）。

## 一、仓库目录

```
Wechat-Agent/
├── docs/                        # 架构文档（总览 + 三层分册 + 本文）
├── config/
│   ├── personas/                # 人格卡（底座 + 会话卡）
│   ├── prompts/                 # prompt 块文件 + order.txt 装配清单
│   └── runtime.json             # 运行时配置（并发上限、间隔、开关）
├── src/
│   ├── shared/                  # 层间契约类型（dataclass 定义）——三层唯一共享物
│   ├── interaction/             # 交互层
│   │   ├── ports/               # 端口：android/（perception/action/device）windows/ macos/
│   │   ├── loop/                # 循环组：统一时间序队列、旅程、屏幕互斥
│   │   ├── reader/              # 读取与同步：切段、增量、回填、打标
│   │   ├── sender/              # bundle 解释执行（拆句/引用/发图/发文件）
│   │   └── msglog/              # 消息日志：写入、版本号、水位差分
│   ├── decision/                # 决策层
│   │   ├── proxy/               # Proxy：五队列、分流、进程监听、台账
│   │   ├── prompt/              # ContextBuilder：块文件装配 + 占位填充
│   │   ├── policy/              # 必回规则、@逐条、兜底话术
│   │   └── provider/            # LLM provider 抽象（k3/DeepSeek/本地）
│   └── tools/                   # 工具层（无代码，仅使用说明 README.md）
├── tests/                       # 分层单测（假层/回放，不连真机真模型）
└── workspace/                   # 运行时数据（gitignore，首次启动自动建立）
```

## 二、workspace/（运行时数据）

```
workspace/
├── chatlogs/                    # 消息日志
│   ├── chatlog.db               # 全量 SQLite（msg_uid 幂等）
│   └── <会话名>.txt             # 每会话文本导出（人读/调试用）
├── media/                       # 多媒体裁图归档
│   └── <会话名>/                # 段条带裁图 + media_id sidecar
├── tasks/                       # subprocess 工作区（一任务一目录）
│   └── 2026-08-08/              # 按日期分目录
│       └── t0007_特高课_m3+m5_整理周报/
│           ├── task.json        # 台账：task_id/session/refs/desc/deliver/
│           │                    #      status/started_at/finished_at
│           ├── brief.md         # 任务简报全文（喂给 kimi -p 的输入）
│           ├── trace.jsonl      # stream-json 执行轨迹（审计）
│           ├── result.txt       # 最终输出（最后一句话总结）
│           └── files/           # 产出文件（发图/发文件的真实路径来源）
└── runtime/                     # 运行状态
    ├── queue.json               # 统一时间序队列持久化（崩溃恢复）
    ├── watermarks.json          # 各会话版本水位指针
    ├── replied_mentions.json    # 已回复 @ 登记
    └── locks/                   # 会话锁/屏幕锁状态（调试用）
```

## 三、规则

1. **一任务一目录**：`tasks/<日期>/<task_id>_<会话>_<消息编号>_<描述>/`，
   命名即归属（哪个会话、哪条/哪些消息、什么事）
2. **一个 subprocess 可合并完成多条消息的任务**：`ref` 支持多个
   （`m3+m5`），命名和 task.json 里都要写全
3. task.json 是唯一台账事实源；Proxy 内存台账崩溃后从目录重建
4. `tasks/` 按日期归档，超过 N 天（默认 14）的旧任务目录可整体清理
5. workspace 路径由入口在首次启动时创建；端口/层之间只传相对路径
