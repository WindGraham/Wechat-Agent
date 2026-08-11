# 消息漏处理与时序竞态问题排查报告（2026-08-10）

> 本文档记录一次真实故障的完整排查过程与根因分析：
> **消息已收到、LLM 已生成回复，但消息最终没发出去 / 没进决策**。
> 涉及：journey（同步）与 proxy（异步）的时序脱节、水位盲区、搜索进入的过渡帧问题。
> 结论先行：这是**架构级时序缺陷**，不是单点 bug。

---

## 一、故障现象（用户可感知）

1. **交流一下？的 @我 消息不回复**：「Sifer/霓鸿: @陈曦 猫猫回应一下这条喜欢」多次收到通知、agent 进入会话，但始终不回；
2. **怨憎会的对联回复没发出去**：LLM 已生成「那我对一个：倚门喊人..."妈呀。" / 对门秒应："爹滴！"」并 route 标记成功，但群里实际没收到；
3. **群友已察觉**：「霓鸿」在群里说「过水位了」——群友发现 agent 的水位机制漏消息。

---

## 二、关键时间线（实测日志还原）

### 2.1 怨憎会：journey 先退出，LLM 后输出（时序竞态铁证）

```
19:29:18  journey start: 怨憎会 (kind=notify)
19:29:22  '怨憎会...' not in list → fallback to search     ← 搜索进入
19:29:35  sync complete: new=3（读到"对对联"等）→ 发 LogUpdated → journey 继续
19:29:47  proxy: 媒体转换完成（new 里有多媒体，转换花了 12 秒）
19:29:55  journey end: 怨憎会 sent=False                     ← journey 先退出了!
19:30:02  proxy: LLM 输出对联回复 → route ok
          → submit_bundle 被调用，但此时屏幕已离开该会话
          → 只有 queue push action: 怨憎会（补救入队）
(之后)    该补救 action 从未被执行（队列后续被清空，无怨憎会 journey 处理它）
```

**核心矛盾**：journey 同步执行、只在"进会话→同步→处理行动"期间持有屏幕；proxy 异步决策（媒体转换 + LLM 调用耗时 10~30 秒），**LLM 输出时 journey 早已退出，发送动作没有执行者**。

### 2.2 交流一下？：搜索进入的过渡帧让 sync 读到空

```
19:46:17  notify: '霓鸿: @陈曦 猫猫回应一下这条喜欢' (mention=True) → 入队
19:47:09  journey start: 交流一下？
19:47:12  '交流一下？' not in list → fallback to search       ← 首页列表没有它
19:47:21  enter_session -> [OK] wechat_chat | 交流一下？       ← _verify_entered 通过
19:47:24  sync complete: new=0                                 ← 读到 0 条!
19:47:26  sync complete: new=0（退出前 final sync）
19:47:26  decision.proxy: [交流一下？] 无新消息，跳过           ← 不进决策
```

**而手动读屏**（页面稳定后）能读到 7 条新消息（quote「y7nieSEL5：陈曦我好喜欢你呀」+「啧」+「过水位了」+「没收到信息」+ 2 条 @陈曦 + 多媒体）。

**对照实验**：YOUSAOBI（在首页列表、直接进入）sync new=2 正常；交流一下？（搜索进入）sync new=0。**差异 = 进入方式**。

---

## 三、根因分析（三层）

### 3.1 直接原因：`_slice_entries` 对非聊天页帧静默返回空

```python
# reader.py _slice_entries
if state.get("page", {}).get("type") != "wechat_chat":
    return None        # ← 静默返回，无任何日志!
```

搜索进入聊天页后，`frame_bus.capture()` 可能截到**搜索结果→聊天页的过渡帧**，页面判定不是 `wechat_chat`：
- `_slice_entries` 静默 `return None`（**无 warning 日志**）；
- 降级 `_state_to_entries` 对过渡帧也解析不出消息 → 空 entries；
- sync 得到 `new=0` → 水位不推进 → **消息永久丢失且无任何痕迹**。

**为什么 `_verify_entered` 能通过但 sync 读不到？**
`_verify_entered` 用 `_snap()` 截图确认了聊天页；但 journey 立即调 sync 时 `frame_bus.capture()` **重新截图**，恰好截在微信搜索→聊天页的过渡动画中间（微信搜索进入有高亮→转场动画，约 1~2 秒）。

### 3.2 结构性原因：journey（同步）与 proxy（异步）双线程抢屏幕

```
【交互线程 run_loop】                      【决策线程 decision-proxy】
  journey: 进会话 → sync → 行动 → 退出      异步消费 LogUpdated → 媒体转换 → LLM → submit_bundle
  同步、独占屏幕操作                          完成后直接操作屏幕发送（也要 enter_session!）
```

| 时序 bug | 后果 |
|---|---|
| journey 不等 proxy 决策完成就退出 | LLM 输出时屏幕已离开 → 发送落空（怨憎会实锤） |
| proxy 异步发送与交互线程无仲裁 | bundle_sender 的 `_mutex` 屏幕锁可能挡住，但 proxy 只看返回 |
| 补救 action 入队后被后续流程吞掉 | 19:30:02 的 push action 从未被执行 |

### 3.3 水位（watermark）盲区

```
水位语义 = "已入库且已决策的最新 seq"
若消息没入库（sync new=0）→ 水位不推进 → 下次仍查 seq>last_seq → 仍为空 → 永远漏
```

水位设计本身合理（保证一批消息只决策一次的幂等性），但它**只认"已入库"**，对"该入库但没读到"的消息无感知——这正是群友说的「过水位了」。

---

## 四、证据链汇总

| # | 证据 | 说明 |
|---|---|---|
| 1 | journal: 19:30:02 怨憎会 llm_output 对联回复 + route ok | LLM 生成了、route 标记成功 |
| 2 | 轮转日志: 19:29:55 journey end sent=False | journey 已退出，无发送 |
| 3 | 轮转日志: 19:30:02 仅 queue push action（补救） | 补救入队但无执行 |
| 4 | journal: 交流一下？ 19:47 无 decision_start | sync new=0 → 未进决策 |
| 5 | 手动读屏: 交流一下？ 屏上有 7 条新消息 | 消息真实存在，DB 却没有 |
| 6 | YOUSAOBI new=2 vs 交流一下？ new=0 | 直接进 vs 搜索进，结果不同 |
| 7 | 水位停在 2032（交流一下？） | 与"消息未入库"一致 |
| 8 | 怨憎会 19:51:03 LLM 输出 `<silent/>` | 那是**正常沉默**（话题无关），非故障 |

---

## 五、修复方向

### 5.1 治本：发送动作统一走队列（架构级）

```
现在: proxy 决策完成 → 直接 submit_bundle（与交互线程抢屏）
改后: proxy 决策完成 → queue.push_action(session, xml) → run_loop 派发 journey 执行
```

- **发送永远有执行者**（journey），不再依赖"决策时恰好有 journey 在目标会话"；
- **屏幕永远单线程访问**（run_loop 独占），消除双线程竞态；
- 现有 `queue.push_action` + journey `_execute_actions` 机制已具备，只需把 proxy 的 `submit_bundle` 调用改为入队。

涉及改动：
1. `proxy.py` `_llm_loop` / `_handle_task_done`：`submit_bundle` → `queue.push_action`；
2. journey action 执行路径保持（已是队列驱动）；
3. 补测试：异步决策 → action 入队 → journey 执行发送的完整链路。

### 5.2 过渡帧兜底：sync 前页面稳定 + 重试

1. `_slice_entries` 非聊天页时**打 warning 日志**（至少可观测）；
2. `sync_session` 里 capture 判非聊天页 → 等待 0.5~1s 重试（最多 2 次）；
3. 搜索 fallback 进入成功后加 `wait_random` 让动画完成。

### 5.3 水位兜底（治标）

- 若通知提示"有新东西"（mention/notify）但水位差分空 → 强制上翻 1~2 屏重同步一次再判断。

---

## 六、附带发现

1. **交流一下？在首页列表"时有时无"**：19:50/19:51 直接进（在列表），19:47/19:52 fallback（不在列表）——首页会话列表 OCR 识别不稳定，加剧了搜索进入的概率；
2. **怨憎会的 `<silent/>` 是正常行为**：LLM 判断话题（"擦边"讨论）与猫无关，按人格规则「群聊中话题与自己无关 → 沉默」主动沉默，不是故障；
3. **热重载曾不生效**（本次排查期间发现）：`hot_reload.py` 只扫描 `src/gateway/*.py` 顶层、不覆盖 `api/` 子目录，且 `_reload_modules` 不含 api 模块——已修复（递归扫描 + reload api.*）。

---

## 七、待决策事项

1. 发送走队列（5.1）的落地细节：action 条目如何携带"跨会话目标"（reply 块 session 属性）？
2. 过渡帧重试（5.2）的等待时长与重试上限？
3. 是否同步修复"首页列表时有时无"（会话列表 OCR 稳定性）？

---

*排查人：Wechat-Agent 开发（2026-08-10）*
*配套设计：DESIGN_DECISION_LAYER_TIMELINE.md.docx（时序一致性）、DECISION_LAYER.md、INTERACTION_LAYER.md*

---

## 八、修复落地（2026-08-10 复盘后实施）

> 二次严格调研（日志逐行追踪 + DB 实证）修正了第五节的部分判断，按实际根因修复。

### 8.1 对第五节判断的修正

1. **5.1 的前提已过时**：`submit_bundle → queue.push_action` 在 08-08（c899bee）
   就已接线（`main.py`），proxy 从不直接碰屏幕。怨憎会 action 19:30:02
   **确实入了队**，丢失发生在**重启环节**：
   - 19:31:03 重启快照只恢复出 canglang，怨憎会 action 消失；
   - 19:32:49 重启恢复出**已执行完毕的** canglang action → 19:33:23 简历通知
     文本被**重复发送**；
   - 19:32:38 入队的交流一下？action 再次丢失。
   结构性缺口 = **没有优雅退出机制**：条目 pop 出队后在途执行，进程被杀时
   既不在内存队列也不在快照里。supervisor 的 stop() 本来就先 SIGTERM 给
   10s grace 期，但 agent 没有 SIGTERM 处理器，等于直接被打死。
2. **过渡帧不是交流一下？失明的唯一原因**：19:50:39 列表**直接进入**也
   sync new=0，且全程无 gap 日志——读到的屏幕内容全部锚在日志尾上，
   即**新消息不在屏上**（会话停在 scrolled-up 位置/懒加载未渲染/过渡帧
   皆符合）。DB 实证：max seq=2032 停在 19:32:30，之后 7+ 次旅程全部
   new=0，水位永久失明。

### 8.2 已实施的修复

| 修复 | 位置 | 内容 |
|---|---|---|
| F1 过渡帧 | `perception/reader.py` | `read_current()` 截到非聊天页帧 → 等待 0.6~1s 重截，最多 2 次；`_slice_entries` 非聊天页返回 None 改为打 warning（不再静默） |
| F2 水位兜底 | `loop/journey.py` + `reader/session_reader.py` | `SessionReader` 暴露 `last_new_count`；journey 初次同步后若**条目带 @我 证据但新增为 0** → 滚向新消息一侧 + 重同步一次，仍空则 WARNING 告警（不静默放过） |
| F3 优雅退出 | `main.py` + `loop/journey.py` | journey 登记 `current_entry`（payload 落地即解除，防重发）；`main.py` 安装 SIGTERM 处理器：在途条目 `reinsert` 回队列 + `queue.flush()` 最终落盘后才退出 |
| F4 快照可靠性 | `loop/unified_queue.py` | `_persist` 异常捕获从 `OSError` 拓宽为 `Exception`——快照写失败必须响亮（快照丢失 = 重启后行动丢失/重发） |
| F5 锚定窗口 | `msglog/message_log.py` | **交流一下？失明的真正根因**：同一发送人发过完全相同的消息时，屏底新消息锚到几十行前的旧副本 → split 推到最后 → new=0 且无 gap，静默吞消息、水位永久停摆。文本/divider 锚定限制到日志尾最近 10 行（媒体原有 last-3 窗口的同类推广）。真机帧离线复现：修复前 new=0，修复后 你好/你好呀 等 4 条全部入库 |
| F6 幻影角标 | `perception/home_parser.py` + `layout_consts.py` | **摸鱼酱死循环根因**：橙红色头像（帽子 130x81/area3261）落在角标区，数字圈分支无尺寸上限 → 幻影 unread=-1 → 盯屏每 ~14s 推一次空旅程。新增数字圈尺寸上限（w≤90/h≤60/area≤2600，标定数字圈 w49-68/area1700-2500 不受影响）。真机帧复测：幻影消除、公众号真红点仍正常检出 |

### 8.4 深夜复发与二次修复（2026-08-10 22:20–23:15）

**复发确认**：F5 上线后 交流一下？仍"看见 @ 不回"。DB 实证：该群水位卡
2032（19:32），19:35–22:42 的 58 条（含 6 次 @陈曦）全部未入库；22:24/22:28
两次 mention 旅程 sync 均 new=0。根因是 F2 兜底只滚一屏——进会话落在
"最早未读"位置时，整屏旧消息全部锚中日志尾（new=0 且无 gap），新消息在
下方约 6 屏深处，单屏滚动够不到。22:44 一次行动旅程因发送跳到屏底、
follow-up 检出 gap，backlog 17 屏回补 60 条，水位恢复，@ 随后被回复。

**顺手坐实另一个群（YOUSAOBI）与 交流一下？是两个独立群**（成员集不同、
同时段各自活跃），此前排查中两者的名字曾互相干扰判断。

| 修复 | 位置 | 内容 |
|---|---|---|
| F2b 循环滚底 | `loop/journey.py` | 水位兜底从"滚一屏+重同步一次"改为**循环**滚底+重同步，读到新消息即停，上限 `SCROLL_RESYNC_ROUNDS=6` 屏（覆盖数小时积压），仍空才告警 |
| F7 路由名单 | `reader/session_reader.py` + `decision/prompt/builder.py` + `decision/proxy/proxy.py` + `config/prompts/user/known_sessions.md` | **发错群根因**：主人让"去交流一下群发言"，决策 prompt 里没有会话名单，LLM 不知道确切群名，把营业话术当本地回复发进了 陈曦猫猫群。修复：决策 prompt 注入【已知会话】块（有消息记录的会话按最近活跃排序取 15 个，OCR 噪声会话自然沉底），跨会话投递照抄名单名 |
| F8 协议补规 | `config/prompts/system/output_protocol.md` | 明确"去别的群发言"必须用 `<reply session="X">` 跨会话投递，绝不在当前会话代发（发在当前会话=发错群） |

测试：`test_journey.py` 水位兜底用例改为循环语义（滚满上限/多屏后读到），
`test_decision.py` 新增已知会话块注入用例，`test_reader.py` 新增
known_sessions 排序/过滤用例；全量测试通过。重启后 proxy journal 实证
【已知会话】块已随决策 prompt 下发（名单含 交流一下？/YOUSAOBI 等）。

### 8.3 遗留

- 快照在事故窗口内为何未落盘（微观原因）未能从日志坐实；F3/F4 从机制上
  封堵了同类丢失。exFAT 上 tmp+replace 实测 100 次零失败，排除文件系统。
- "首页列表时有时无"（会话列表 OCR 稳定性）未修，仍待决策。
- **runtime.json 热重读已死**：`RuntimeConfig.check()` 全代码库无人调用，
  暂停/配置修改对运行中的 agent 不生效（2026-08-10 调试时发现），待修。
- 交流一下？输入栏残留草稿（"主人，这个"）：19:30 进程被杀时引用回复
  流程输入到一半留下的，下次向该会话发消息时 clear_text 会清掉；也可手动删。
- 测试：`tests/test_journey.py` 新增水位兜底/在途条目两组用例，
  `tests/test_reader.py` 新增过渡帧重试用例，`tests/test_message_log.py`
  新增锚定窗口回归用例（重复消息不再吞新消息），
  `tests/test_home_parser.py` 新增幻影角标尺寸上限用例；全量 167 个测试通过。
