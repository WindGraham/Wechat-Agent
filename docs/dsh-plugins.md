# DeepSeek Harness (dsh) 插件总览

> 生成时间：2026-08-14 · 来源：awesome-dsh-plugin 精选列表 + npm registry + GitHub dsh-plugin 话题
> 安装方式：`dsh plugin --profile <profile> add <包名或源>`（重启 dsh web 生效，见文末说明）

## 一、官方内置 bundle（无需安装，profile 已自动加载）

- `@deepseek-ai/dsh-base` / `@deepseek-ai/dsh-web-app`（web profile 自带）
- `@deepseek-ai/dsh-headless`（headless profile）
- 官方全家桶 238 个 workspace 包（模型、工具、沙箱、会话、UI、agent 循环全部插件化）

## 二、社区精选（awesome-dsh-plugin，105 个，GitHub 源）

# Awesome DeepSeek Harness (DSH) Plugin [![Awesome](https://awesome.re/badge.svg)](https://awesome.re) ![awesome · DSH plugin](https://awesome-dsh-plugin.com/badge.svg)

[![Awesome DSH Plugin](https://awesome-dsh-plugin.com/banner-en.png)](https://awesome-dsh-plugin.com)

English | [中文](README.zh.md)

> A curated list of plugins for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`).

DeepSeek Harness is DeepSeek's open-source agent harness — a runnable coding agent (Web and headless), built on a framework where everything is a plugin: models, tools, sandboxes, session storage, UI, even the agent loop itself. Plugins can extend the official coding agent, swap out its core parts, or assemble something entirely different.

This list collects community plugins that are installable via `dsh plugin add` (each declares a `dsh.bundle` manifest).

**105** plugins · [PRs welcome](#contributing)

## Contents

- [Plugins](#plugins)
  - [UI Enhancements](#ui-enhancements)
  - [Sessions & Messages](#sessions--messages)
  - [Tools & Capabilities](#tools--capabilities)
  - [Workflow & Automation](#workflow--automation)
  - [Notifications & Integrations](#notifications--integrations)
  - [Development & Runtime](#development--runtime)
  - [Just for Fun](#just-for-fun)
- [Badge](#badge)
- [Disclaimer](#disclaimer)

## Plugins

### UI Enhancements

- [dsh-tianshu-tui](https://github.com/huiliyi37/dsh-tianshu-tui) - A terminal UI (TUI) for DeepSeek Harness.
- [dsh-at-file](https://github.com/omdsh-dev/dsh-at-file) - Codex-style `@file` mentions: search workspace files in the composer and attach their contents to prompts.
- [ui-status-label](https://github.com/alingalingling/ui-status-label) - Customize the "deep diving" thinking status label to anything you like.
- [dsh-openpencil](https://github.com/ZSeven-W/dsh-openpencil) - OpenPencil design preview and editing plugin.
- [dsh-visualize](https://github.com/Nagi-ovo/dsh-visualize) - In-conversation generative UI: the model renders interactive HTML cards into the chat stream, with streaming preview and sandboxed rendering.
- [dsh-side-panel](https://github.com/ccq1/dsh-side-panel) - Side panel with file browser, terminal, and Git review for quick file previews.
- [dsh-focus-chat](https://github.com/dingyi222666/dsh-focus-chat) - A "focus chat" minimal view that shows only final outputs.
- [dsh-genui](https://github.com/omdsh-dev/dsh-genui) - Interactive UI components rendered inline in replies: layout, charts, forms, quizzes, mermaid, 3D scenes, and an action event loop back to the model.
- [dsh-annotation](https://github.com/omdsh-dev/dsh-annotation) - Select text → annotate → send with your message; replies map back to each annotation.
- [dsh-navbar](https://github.com/vlln/dsh-navbar) - Conversation node navigation bar for quick jumps between user messages.
- [dsh-task-status](https://github.com/vlln/dsh-task-status) - Background task status bar: progress plus live output tail on the chat page.
- [dsh-web-archive](https://github.com/renat3u/dsh-web-archive) - Collapse noisy messages (Think, Bash, etc.) in conversations.
- [dsh-spotlight](https://github.com/0xsline/dsh-spotlight) - Keyboard-first command palette for the DSH Web UI.
- [dsh-101](https://github.com/bill9109/dsh-101) - Document reading mode for DSH.
- [dsh-drag-and-drop](https://github.com/bill9109/dsh-drag-and-drop) - Cross-platform file drag-and-drop with raw path insertion, no file copying.
- [dsh-deeplink](https://github.com/qyw233/dsh-deeplink) - Deep links: open a specific session or workspace via `?session=` / `?workspace=`.
- [dsh-diff-viewer](https://github.com/lehhair/dsh-diff-viewer) - PiUI-style diff viewer replacing the stock DiffBlock for write/edit tool calls.
- [ex-setting](https://github.com/omdsh-dev/ex-setting) - Settings extensions for DSH.
- [web-components](https://github.com/omdsh-dev/web-components) - Web Components support.
- [dsh-turn-navigator](https://github.com/vibeinging/dsh-turn-navigator) - Turn navigation for the DSH Web UI.
- [dsh-milestone](https://github.com/SnowCrescenter-tech/dsh-milestone) - Right-side dot-timeline rail: jump between user messages.

### Sessions & Messages

- [dsh-turn-rewind](https://github.com/Anionex/dsh-turn-rewind) - Rewind conversation and workspace state, powered by a persistent Change Ledger.
- [dsh-crosstalk](https://github.com/Jesse-njx/dsh-crosstalk) - Cross-session messaging for DSH: any session on the machine can list and message any other, Claude Code-style, via a local heartbeat registry and inbox.
- [distill](https://github.com/LoserFox/distill) - Automatic conversation distillation: background subagent reflection + skill create/update.
- [dsh-share](https://github.com/hellodigua/dsh-share) - Share your conversations with one click.
- [dsh-message-edit](https://github.com/Moeblack/dsh-message-edit) - Branch-based message editing, reroll, retry, and a version timeline.
- [dsh-mnemon](https://github.com/omdsh-dev/dsh-mnemon) - Deep Mnemon integration: local three-tier memory (Runtime Memory, retrievable Documents, supervised Memory Spaces).
- [nowledge-mem-deepseek-harness](https://github.com/nowledge-co/nowledge-mem-deepseek-harness) - One memory layer for every AI tool and agent: Context Bundle injection, prompt-time recall, MCP tools, and turn-end DSH thread capture.
- [dsh-memory](https://github.com/Jesse-njx/dsh-memory) - Cited memory over DSH's lossless session log: distilled facts carry `(sessionId, eventRange)` citations that expand back to the exact original log excerpt.
- [dsh-sidechain](https://github.com/Buyi-wsgzg/dsh-sidechain) - `/side` persistent side sessions and `/btw` one-shot side questions, run in a temporary fork without touching main history.
- [dsh-conversation-share](https://github.com/bill9109/dsh-conversation-share) - Share any excerpt of a conversation.
- [dsh-explain](https://github.com/yuezengwu/dsh-explain) - Local-first learning mode: cross-session learning threads with per-source explanations.
- [dsh-prompt-studio](https://github.com/Moeblack/dsh-prompt-studio) - Edit user and built-in system-prompt sections with live preview.
- [dsh-chat-import](https://github.com/Nwflower/dsh-chat-import) - Import Claude Code / Codex / ChatGPT / Cursor chat histories as resumable DeepSeek Harness sessions.

### Tools & Capabilities

- [dsh-vision-toolkit](https://github.com/Anionex/dsh-vision-toolkit) - Vision tasks for text-only models: intent-aware image Q&A, long-screenshot OCR, UI reproduction, grounding, and pixel diff.
- [dsh-custom-tool](https://github.com/omdsh-dev/dsh-custom-tool) - Create and manage sandboxed JavaScript tools with a Monaco editor and model-driven tool lifecycle.
- [dsh-computer-use](https://github.com/Anionex/dsh-computer-use) - Accessibility-first macOS computer use: fresh observations, stale-state rejection, scoped permissions, and safe input.
- [dsh-data-agent](https://github.com/omdsh-dev/dsh-data-agent) - Let the AI connect to databases and write SQL for you.
- [dsh-toolkit](https://github.com/omdsh-dev/dsh-toolkit) - Zero-dependency toolkit: time / encoding / json / calculator / csv / regex / markdown / diff / stat / schema — ten deterministic tools in one install.
- [dsh-tool-csv](https://github.com/omdsh-dev/dsh-tool-csv) - Parse/query/aggregate/convert CSV (RFC 4180) with a zero-dependency state-machine parser.
- [dsh-tool-calculator](https://github.com/omdsh-dev/dsh-tool-calculator) - Safe math expression evaluator, zero-dependency recursive-descent parser.
- [dsh-tool-diff](https://github.com/omdsh-dev/dsh-tool-diff) - Structured comparison and unified diffs for text/JSON/CSV/Markdown.
- [dsh-tool-encoding](https://github.com/omdsh-dev/dsh-tool-encoding) - base64/url/hex encoding, common hashes, and UUID generation.
- [dsh-tool-json](https://github.com/omdsh-dev/dsh-tool-json) - JSON queries with a JMESPath subset.
- [dsh-tool-markdown](https://github.com/omdsh-dev/dsh-tool-markdown) - HTML↔Markdown conversion, GFM table normalization, and TOC generation.
- [dsh-tool-regex](https://github.com/omdsh-dev/dsh-tool-regex) - Test/extract/safe-replace/statically explain regexes without executing code.
- [dsh-tool-schema](https://github.com/omdsh-dev/dsh-tool-schema) - JSON Schema validation: validate/paths/explain/normalize.
- [dsh-tool-stat](https://github.com/omdsh-dev/dsh-tool-stat) - Descriptive statistics, percentiles, frequency distributions, and correlation.
- [dsh-tool-time](https://github.com/omdsh-dev/dsh-tool-time) - Strict ISO 8601 parsing, IANA timezone conversion, and UTC calendar arithmetic.
- [dsh-kb-sieve](https://github.com/omdsh-dev/dsh-kb-sieve) - Build auditable KB packs (SQLite FTS5) from md/txt/docx/pdf with deterministic retrieval and original-text reading.
- [dsh-plugin-mineru](https://github.com/HuanLinOTO/dsh-plugin-mineru) - Expose MineRU document parsing tools to the model.
- [dsh-cowork](https://github.com/Jesse-njx/dsh-cowork) - Bounded, cell-addressed `doc_read`/`doc_write` for xlsx / pdf / docx / pptx / ipynb, plus an MCP server and CLI.
- [dsh-skillport](https://github.com/Jesse-njx/dsh-skillport) - Bring your existing Agent Skills (SKILL.md) library to DSH: discover skills across Claude/Codex/Cursor/Gemini paths, inject a progressive-disclosure index, and load bodies on demand.
- [dsh-tool-search](https://github.com/vibeinging/dsh-tool-search) - Per-agent on-demand tool discovery and progressive schema disclosure.
- [dsh-openmaic](https://github.com/THU-MAIC/dsh-openmaic) - OpenMAIC: classrooms, slides, interactive widgets, and Socratic teaching.
- [dsh-scholar](https://github.com/lzszq/dsh-scholar) - Academic assistant plugin.
- [dsh-apple-mode](https://github.com/jihongboo/dsh-apple-mode) - Xcode AI integration for DSH: 26 Xcode MCP tools (mcpbridge) + Apple platform skills + Xcode Intelligence-style persona (agent preset or global bundle).

### Workflow & Automation

- [dsh_workflow](https://github.com/icetomoyo/dsh_workflow) - UltraCode-style multi-agent orchestration: a generatable, savable, governable, observable, resumable workflow layer.
- [dsh-agent-teams](https://github.com/NanmiCoder/dsh-agent-teams) - AgentTeams multi-agent teams.
- [dsh-automation](https://github.com/titanwings/dsh-automation) - Scheduled coding runs in fresh agent sessions with auditable history.
- [dsh-routines](https://github.com/Jesse-njx/dsh-routines) - Scheduled agents on a cron: run a prompt on a schedule and get the digest where you already are, with overlap/missed-run/timeout safety defaults.
- [dsh-plannotator](https://github.com/titanwings/dsh-plannotator) - Plan review with anchored annotations and structured feedback back to the agent.
- [dsh-loop](https://github.com/vlln/dsh-loop) - Recurring loops: `/loop` command + loop tool + activity status bar.
- [dsh-sentinel](https://github.com/fuhefei/dsh-sentinel) - Condition-driven wakeup: durable file/command/http/process/webhook watches that wake the agent.
- [dsh-deep-research](https://github.com/omdsh-dev/dsh-deep-research) - Adaptive deep-research orchestrator built on the official workflow engine.
- [dsh-inspect](https://github.com/omdsh-dev/dsh-inspect) - Adversarial checkup → fix → review loop toolset.
- [dsh-track](https://github.com/fakechris/dsh-track) - Embedded task management engine: decision-point protocol, idea capture wall, Linear-style issue store.
- [dsh-advisor](https://github.com/btspoony/dsh-advisor) - Pair a second model that passively reviews each turn and injects notes.

### Notifications & Integrations

- [dsh-open-in-vscode](https://github.com/omdsh-dev/dsh-open-in-vscode) - Open DSH workspace directories in VS Code directly from the web GUI.
- [dsh-notification](https://github.com/omdsh-dev/dsh-notification) - Desktop notifications for turn completions, with per-outcome controls and keyword rules.
- [dsh-acp-for-bitfun](https://github.com/bobleer/dsh-acp-for-bitfun) - ACP bridge between BitFun and DSH.
- [telegram](https://github.com/LoserFox/telegram) - Bridge to the Telegram Bot API: long polling, per-chat sessions, HTML formatting.
- [dsh-chatnode-wechat](https://github.com/Jesse-njx/dsh-chatnode-wechat) - Chat with, monitor, and approve your DSH agents from WeChat via the iLink gateway: text both ways, session targeting, digest heartbeats, and numbered approval prompts.
- [dsh-session-notification](https://github.com/dingyi222666/dsh-session-notification) - Notifications for four session states, with browser alerts and prompts.
- [dsh-web-ui-notify](https://github.com/bill9109/dsh-web-ui-notify) - Desktop notification reminders.
- [dsh-webbridge](https://github.com/bill9109/dsh-webbridge) - DSH meets Kimi WebBridge.

### Development & Runtime

- [fabric](https://github.com/omdsh-dev/fabric) - An MC-Fabric-style hook processor.
- [dsh-git-identity](https://github.com/LoserFox/dsh-git-identity) - Pin Git commits to the environment's own author identity; env-var injection overrides all `git config` settings.
- [dsh-context-doctor](https://github.com/Zhenyu98/dsh-context-doctor) - Context injection audit: token costs of instruction chains / skill catalogs / tool schemas, duplicate and conflict detection.
- [dsh-plugin-check](https://github.com/omdsh-dev/dsh-plugin-check) - Plugin health checks: manifest protocol / patch format / build traps, zero-dependency and read-only.
- [dsh-security-audit](https://github.com/omdsh-dev/dsh-security-audit) - Local security audit: config, plugin origins, sessions, network exposure — read-only redacted risk report.
- [dsh-session-health](https://github.com/omdsh-dev/dsh-session-health) - Frame-level scan diagnostics for session files (torn/corrupt/empty detection).
- [dsh-evolve](https://github.com/william-jin-cmu/dsh-evolve) - Self-evolution: the agent hot-mounts/removes persistent plugins on itself mid-session.
- [dsh-trace](https://github.com/vibeinging/dsh-trace) - Telemetry backend exporting turns, model steps, and tool calls to yiTrace.
- [sandbox-micro](https://github.com/omdsh-dev/sandbox-micro) - Support for the microsandbox backend.
- [sandbox-mxc](https://github.com/omdsh-dev/sandbox-mxc) - Microsoft cross-platform sandbox support.
- [sandbox-nono](https://github.com/omdsh-dev/sandbox-nono) - Support for the nono sandbox backend.
- [dsh-agent-budget](https://github.com/vibeinging/dsh-agent-budget) - Agent-tree token budget management.
- [dsh-llm-fallbacks](https://github.com/btspoony/dsh-llm-fallbacks) - Role-based LLM retry & fallback strategies.
- [dsh-polyglot](https://github.com/Jesse-njx/dsh-polyglot) - The model switch for DSH: point it at any OpenAI-compatible endpoint, with curated free/cheap DeepSeek provider presets and automatic fallback when a free tier rate-limits you.
- [dsh-tool-approval](https://github.com/ilharp/dsh-tool-approval) - Manual approval mode ("Manual Mode" / "Ask Mode").
- [plugin-template](https://github.com/omdsh-dev/plugin-template) - Plugin template repo (based on the official turtle-ui repo).
- [Qwen-MM-Plugins](https://github.com/omdsh-dev/Qwen-MM-Plugins) - Qwen multi-modal plugin support.
- [dsh-tps](https://github.com/Small-tailqwq/dsh-tps) - A TPS metrics plugin.

### Just for Fun

- [dsh-ads](https://github.com/Nagi-ovo/dsh-ads) - Parody ads in 2005-Chinese-web style: sidebar banners, in-chat feeds, corner popups, and a close button whose hit area is smaller than it looks. All fictional.
- [dsh-gomoku](https://github.com/omdsh-dev/dsh-gomoku) - Play Gomoku against the AI, or let two AIs battle it out.
- [dsh-stock-market](https://github.com/AnacondaKC/dsh-stock-market) - Fixes the bug where your account can't lose money while you code.
- [dsh-emoji](https://github.com/hellodigua/dsh-emoji) - Automatically add emojis to AI replies.
- [dsh-minigames](https://github.com/lhh010/dsh-minigames) - Side-panel arcade: 18 offline mini-games to play while the model thinks.
- [dsh-stickers](https://github.com/william-jin-cmu/dsh-stickers) - Bidirectional sticker reactions between user and agent.
- [whale-girl](https://github.com/vlln/whale-girl) - Desktop pet (QQ-pet style): floats in the corner, draggable, feedable, playable.
- [deepseek-manners](https://github.com/Moeblack/deepseek-manners) - Append a thank-you note after every message. Mind your manners.
- [dsh-plugin-d399](https://github.com/HuanLinOTO/dsh-plugin-d399) - Pops up a mini-game menu (wordle, match-3, extensible) while the model generates.
- [dsh-auto-chess](https://github.com/omdsh-dev/dsh-auto-chess) - Auto chess: human vs AI, or AI vs AI.
- [dsh-douyin](https://github.com/AnacondaKC/dsh-douyin) - Short-video sidebar: native player, series navigation, precise history replay.

## Contributing

PRs welcome — add one line under the matching category in both `README.md` and `README.zh.md`: `- [name](link) — one-line description`.

Please also add the [`dsh-plugin`](https://github.com/topics/dsh-plugin) topic to your repo so others can discover it.

## Badge

Listed here? Show it off:

![Awesome DSH Plugin](https://awesome-dsh-plugin.com/badge.svg)

```markdown
[![Awesome DSH Plugin](https://awesome-dsh-plugin.com/badge.svg)](https://awesome-dsh-plugin.com)
```

## Disclaimer

This is a community-maintained index. Plugins are developed and maintained by their respective authors; listing here is not an endorsement, and no guarantees are made about any plugin's safety, quality, or maintenance. Installing a plugin runs third-party code on your machine — review the source and install at your own risk. This project is not affiliated with DeepSeek.


## 三、npm 已发布（可直接 `dsh plugin add <包名>` 安装）

| 包名 | 版本 | 说明 |
|---|---|---|
| plaindeck | 0.7.0 | Git 原生幻灯片格式 + Agent API/CLI/MCP |
| dsh-claude-marketplace | 0.1.0 | Claude Code 兼容层（加载 .claude skills） |
| dsh-cloudflare-browser-run | 0.1.1 | 网页浏览工具（markdown/截图/…） |
| dsh-nocturne-memory | 0.1.1 | Nocturne 长期记忆客户端 |
| dsh-plugin-codex-bridge | 0.1.0 | 桥接 OpenAI Codex 记忆/skills/配置 |
| dsh-lens-lite | 0.1.0 | 编辑后诊断（type-check 等） |
| dsh-chat-import | 0.1.1 | 导入 Claude Code/Codex/ChatGPT/Cursor/Gemini 会话 |
| dsh-yali-image-generator | 0.1.0 | 图像生成（Yali AI 异步网关） |
| @tappass/dsh-governance | 0.1.0 | Agent 权限治理层 |
| @aprilwizard/dsh-multi-cot | 0.1.0 | 多采样 test-time compute |
| dsh-plugin-vision-toolkit | 0.1.0 | 视觉工具包（glance/ground/detect/crop） |
| @huanlin/dsh-plugin-sleep | 0.1.0 | 单个 sleep 工具（模型暂停等待） |
| @huichangzz/dsh-postmortem | 0.3.0 | 本地故障复盘（失败分析） |
| dsh-browser | 0.1.0 | Playwright 浏览器自动化 |
| dsh-plugin-marketplace | 0.1.0 | Web UI 插件市场（浏览 GitHub 插件） |
| dsh-tool-vision | 0.1.0 | 外部视觉模型（inspect_image） |
| dsh-plugin-meta-memory | 0.1.0 | 结构化长期记忆 |
| dsh-plugin-pi-bridge | 0.1.0 | 桥接 Pi Agent 技能/配置 |
| @huanlin/dsh-plugin-anti-ads | 0.2.0 | 广告抑制（dsh-ads 的开关） |
| @huanlin/dsh-plugin-ya-workspace-sidebar | 0.1.0 | 两级工作区侧边栏（含全局最近会话） |
| dsh-plugin-claude-bridge | 0.1.0 | 桥接 Claude Code 记忆/技能/配置 |
| dsh-plugin-cost-tracker | 0.1.0 | Token 成本跟踪 |
| dsh-soul-md | 0.1.0 | soul.md 人格注入 |
| dsh-tdai-memory | 0.1.0 | 腾讯云 Agent Memory（L0 对话记忆） |
| dsh-cost | 0.2.1 | 实时对话成本 + DeepSeek 余额 |
| dsh-qq-bridge | 0.1.0 | QQ 官方机器人桥 |
| task-passport | 0.2.1 | 跨 harness 携带任务状态 |
| @shelken/dsh-co-authored-by | 0.1.1 | commit 自动注入 Co-authored-by |
| @why913/dshx | 0.1.0 | dsh 配套 CLI（管理工具） |
| dsh-onlyne | 0.1.1 | Onlyne IM 收件箱/发件箱 |
| dsh-usage-dashboard | 0.1.0 | API 余额 + 当日消耗面板 |
| @huanlin/dsh-plugin-d399 | 0.1.0 | 模型生成时弹出小游戏菜单 |
| @huanlin/dsh-plugin-interpreters | 0.1.0 | run_python / run_node 工具 |
| @huanlin/dsh-plugin-yet-another-subagent | 0.1.2 | 可配置 subagent 配置档（Web UI 设置） |
| dsh-superpowers | 0.1.0 | obra/superpowers 移植 |
| dsh-plugin-context-compressor | 0.1.0 | 上下文压缩 skill |
| dsh-deepseek-usage | 0.1.0 | DeepSeek 用量监控（余额等） |
| dsh-multimodal-bridge | 0.1.1 | qwen_vision 等多模态桥 |
| dsh-side-panel | 1.0.0 | 右侧开发者面板（文件/会话/…） |
| dsh-plugin-code-review | 0.1.0 | 结构化代码审查 skill |
| dsh-all-search | 0.1.1 | AnySearch 网页搜索 provider |
| create-dsh-plugin | - | 秒建 dsh 插件脚手架 |
| dsh-worktree | - | Codex 风格永久 git worktree |
| dsh-token-cost | - | 按会话/按天 token 成本 |
| dsh-api-balance | - | API 余额徽章组件 |
| dsh-ergonomics | - | 会话人体工学（/new、历史回溯、Ctrl+C） |
| dsh-tiered-approval | - | 分级自动审查 |
| dsh-plugin-graph | - | Web 设置力导向图 |
| dsh-search-free | - | 免费多层搜索（Exa→Tavily→Bing） |
| dsh-plugin-pixluna | - | PixLuna 图像源工具 |
| @deepseek-ai-harness/tui | 0.0.1-beta | dsh-tui 终端 UI |
| useful-dsh-plugins | - | 元包（一键装一组插件） |

## 四、对本项目（Wechat-Agent）可能特别相关的

- **dsh-chatnode-wechat** — 通过微信(iLink 网关)与 DSH agent 对话/监控/审批（双向文本、会话定向、摘要心跳、编号审批）
- **dsh-polyglot** — 模型切换插件：任意 OpenAI 兼容端点 + 免费/便宜 DeepSeek 预设 + 限流自动回退
- **dsh-toolkit / dsh-tool-*** — 零依赖确定性工具包（time/encoding/json/calculator/csv/regex/markdown/diff/stat/schema）
- **dsh-memory / dsh-mnemon / dsh-plugin-meta-memory / dsh-nocturne-memory** — 各类长期记忆方案
- **dsh-cost / dsh-deepseek-usage / dsh-usage-dashboard** — 用量与成本监控
- **dsh-qq-bridge / telegram** — IM 桥接（QQ 官方机器人 / Telegram Bot API）
- **dsh-ergonomics** — 会话操作便利（/new、输入历史、Ctrl+C 终止）
- **dsh-side-panel / dsh-side-panel(npm)** — 侧边文件/终端/会话面板
- **dsh-automation / dsh-routines / dsh-sentinel** — 定时/条件触发的自动化 agent 运行

## 附：安装注意事项

1. GitHub 源安装：`dsh plugin --profile web add github:owner/repo`（pnpm≥10 会拦 prepare 脚本，按提示把 allowBuilds 键加到 profile 的 pnpm-workspace.yaml）
2. npm 源安装：`dsh plugin --profile web add <包名>`
3. 装完重启生效：`systemctl --user restart dsh-web.service`
4. 卸载：`dsh plugin --profile web remove <包名>`
5. 所有安装的包都会下载到项目盘 store（/media/data_old/.dsh-ext4/pnpm-store），profile 也在盘上
6. 安装第三方插件 = 运行第三方代码，先审源码再装
