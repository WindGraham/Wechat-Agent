# 新旧逻辑交替·未清理状态 盘查报告（2026-08-13）

> 一次严格的全仓库代码考古，目标是找出「希望更新但新旧逻辑交替、新的没写好、
> 旧的没删净」的半途迁移/未清理状态。覆盖 7 个分片：装配层（main.py/shared/tools）、
> 决策层（src/decision）、网关层（src/gateway）、交互层核心（loop/reader/sender/msglog）、
> Android 端口 action+device、Android 端口 perception、config↔代码接线一致性。
>
> 方法：grep 信号词 → read 上下文 → 在**整个 src/（含 scripts/、tests/）**核实调用方与
> 生产者 → 定类别与严重度。所有「死代码/未接线/双路径」结论均经调用方核实，非 grep
> 命中臆测。已对 3 处最高危结论做独立复核。
>
> 结论总量：**高危 8、中危 16、低危约 25，合计约 50 处**（已跨分片去重）。

---

## 〇、判级类别定义

| 类别 | 含义 |
|---|---|
| A | 过时注释/文档：写「尚未实现/暂不/TODO」但实际已实现（或相反） |
| B | 新旧双路径并存：旧逻辑和新逻辑都还在跑（分支/开关/切换），新的没覆盖全旧行为 |
| C | 新代码写好但没接线（死新代码），或旧 stub/占位实现没删净（死旧代码） |
| D | 「兼容/旧签名/向后兼容/legacy」包装：无调用方=死代码；有调用方=真实双路径 |
| E | 运行时可达的占位/空实现/未标定硬编码兜底（危险：真会被执行到） |

严重度：**高**（会静默出错/丢功能/污染数据，必须决策）/ **中**（功能降级或双重维护）/
**低**（死代码/过时注释，可随手清）。

---

## 一、五大模式总览

| 模式 | 数量级 | 一句话 |
|---|---|---|
| ① 新链路写好了没接线 | ~10 | chat_slicer 半接 / moments_reader / roster_matcher / realtime_scan / scroll_stitch / home_scan / TTS-clone |
| ② 旧实现没删净 | ~15 | parse_chat 旧气泡路径 / message_log backfill / 旧签名 / 死方法 / 死模板 / 死别名 |
| ③ 「单一事实来源」收敛只做了一半 | ~6 | DEFAULT_MODELS / layout_consts / AVAILABLE_MODELS / monitored / DEFAULTS / 提取清单 |
| ④ 文档·prompt·注释与代码脱节 | ~10 | main.py docstring / chat_history 幽灵工具 / order.txt 死段 / 过时注释 |
| ⑤ 危险占位 / 未标定 | ~4 | get_clipboard / 向量库删库 / BG_PINNED / 小程序面板 |

---

## 二、高危（8 处，必须决策：接线 or 删除）

### H1　chat_parser ↔ chat_slicer 双解析器并存（类别 B）— 高

- **位置**：`state_builder.py:202-226`；`chat_slicer.py:22,375,471,604,770,792`；`reader.py:57-58,96-138,275-279`；`quote_reply.py:492`
- **旧逻辑**：`parse_chat` 气泡驱动，聊天页完整解析（~430 行消息构建）。
- **新逻辑**：`slice_chat` 头像顶切段（WP2），声明「平行实现，不改动 chat_parser」。
- **接线状态**：聊天页每帧 `parse_chat` 完整跑完 → 再 `slice_chat` → `state_builder:219-221`
  把 parse_chat 的 message_bubble/time_divider 全部丢弃、用 slice_chat 结果替换；
  parse_chat 只保留 title/is_group/input_area/actions/按钮/胶囊/pinned_bar。`quote_reply`
  仍直接调 parse_chat 拿气泡元素（旧消费者仍活）。`reader._slice_entries` 又对同一帧
  重跑 run_ocr+slice_chat（与 state_builder 里的 slice_chat 重复）。
- **问题**：两套解析器、两份从 chat_parser「平移」来的引用配对/胶囊/居中小字逻辑
  （chat_slicer 内 5 处标注「自 chat_parser L396-410 平移」等），维护双份 + 每帧双 OCR/双 slice。
- **建议**：把 parse_chat 拆成「头部/输入栏/按钮/胶囊」与「消息构建」两层；消息构建层
  只留给 quote_reply 用，或让 quote_reply 也切到 slice_chat；reader 复用 state 的 slice
  结果而非重跑 slice_chat。

### H2　moments_reader.py 读 feed 链整条未接线（类别 C）— 高

- **位置**：`moments_reader.py:79-318`（FeedStitcher / MomentsReader.read_feed / 水位续读）；`moments_parser.py:24,707-709`
- **接线状态**：`src/` 内无任何 import/call（仅 `tests/test_moments_reader.py` 测了纯
  FeedStitcher）。决策层只有 `post_text_moments`（发朋友圈），没有「读朋友圈」入口。
- **附加问题**：`moments_parser.py` 把「兼容旧契约」的 `parse_moments`（实为 state_builder
  唯一活入口）与「新契约」`parse_moments_entries`（唯一外部消费者是死链 MomentsReader）
  **反向标注**——把活的叫「旧」、把只有死链用的叫「新」。
- **建议**：要么接到某个 journey/handler（真正的读朋友圈入口），要么明确标为「未完成」
  删除/归档；同时反转 parse_moments 的命名/注释。

### H3　vector_store 每次启动删库重建、无 reindex（类别 E/B）— 高

- **位置**：`vector_store.py:98-121`
- **问题**：`_ensure()` 首用时无条件 `delete_collection("memory_facts")` 再 `create_collection`
  （注释「防 embedding 函数不匹配」），且全仓库无任何 reindex/backfill（`store.py` 只在
  add/update 时 upsert，`proxy._memory_store()` 只是构造）。
- **后果（可达）**：chromadb 已安装（v1.5.9），`_ensure` 成功执行 → 每次进程启动删除并重建
  空 collection → **重启后旧 fact 全无向量，search 返回空 → 永远回退子串匹配**；只有本次
  进程内新写入的 fact 才有向量。「语义检索」这个新功能在重启后等于没接线。
- **建议**：`_ensure` 不要无条件删 collection；改用稳定 embedding（固定 n-gram 384 维，
  别优先 sentence-transformers 造成维度漂移），并在 MemoryStore 构造后对已有 fact 做一次性
  upsert 回灌（或懒加载时全量重建索引）。

### H4　factory DEFAULT_MODELS 导入未用 + deepseek 默认漂移（类别 B）— 高

- **位置**：`factory.py:14,69,73`；`model_catalog.py:33-35`
- **旧逻辑**：`create_provider` 硬编码默认模型：kimi→`"k3"`、deepseek→`"deepseek-chat"`。
- **新逻辑**：`factory.py:13-14` 已加注释「收敛到 shared 单一事实来源」并
  `from src.shared.model_catalog import DEFAULT_MODELS`；`model_catalog.py:35` 规定
  deepseek 默认 = `deepseek-v4-pro`。
- **接线状态（已复核）**：`DEFAULT_MODELS` import 后**从未被使用**，硬编码 `"deepseek-chat"`
  仍在生效。这正是 `model_catalog.py:11-15` docstring 明说的「历史教训：deepseek 出现
  deepseek-chat 与 deepseek-v4-pro 两套默认」——迁移到一半，老默认值还在跑。调用路径可达
  （`main.py:156-158` model 缺省时命中）。
- **建议**：`create_provider` 改用 `DEFAULT_MODELS[prefer]`（kimi/deepseek 两条分支），删掉
  `"k3"`/`"deepseek-chat"` 字面量；或至少把 deepseek 兜底改为 `deepseek-v4-pro` 并真正消费
  已 import 的 `DEFAULT_MODELS`。

### H5　realtime_scan + roster_matcher 无生产接线（类别 C）— 高

- **位置**：`loop/realtime_scan.py:111`；`perception/roster_matcher.py:35-172`
- **接线状态**：`realtime_scan()` 零调用（未接 run_loop/session_reader，用户「实时处理」诉求
  写了没接）；花名册双因子匹配唯一生产消费者是 realtime_scan（`:114/129`），而 realtime_scan
  本身无 src 调用方（仅 scripts/diag_clipped.py 复用其 do_swipe/scroll_to_latest）。
  `state_builder:218` 与 `reader.py:122` 调 slice_chat 都未传 roster_matcher（默认 None）。
- **建议**：若群成员识别要进主读取路径，需在 reader/state_builder 注入 roster_matcher；
  否则整条 realtime_scan+roster_matcher 属 scripts 级实验代码，应删除或降级归档。

### H6　scroll_stitch 未接主流程（类别 C）— 高

- **位置**：`loop/scroll_stitch.py:73,98`（stitch_sequence / scroll_scan）
- **接线状态**：`scroll_scan()/stitch_sequence()` 零调用；`scripts/run_scroll_scan.py` 只借
  `find_overlap_dy` 自己 inline 重写循环；旧 `scripts/scroll_scan.py`（指纹去重）仍在，
  `SCROLL_STITCH_PLAN.md:133` 替换勾选项未打。
- **建议**：接主流程或删（明确迁移决策）；连带 `msglog` 的 `complete==2`（`:472`）无生产者。

### H7　device_ctl.get_clipboard 返回伪造占位串（类别 E）— 高

- **位置**：`device_ctl.py:263-275`
- **问题**：`get_clipboard()` 返回伪造字面量 `"wxid_or_nickname_from_clipboard"`，注释自认
  「此处暂时返回占位符或解析逻辑」。
- **接线状态**：全仓库无调用方（当前不可达），但属「一旦接线就静默污染数据」的类型。
- **建议**：优先删除，或补真实的剪贴板解析（ADBKeyBoard 广播方案见同文件 :269 注释）。

### H8　default.yaml 引用不存在的 chat_history 工具（类别 B）— 高

- **位置**：`config/personas/default.yaml:105,119`
- **问题（已复核）**：prompt few-shot 写 `<tool name="chat_history" .../>`，而
  `proxy._exec_tool`（`proxy.py:702-717`）只认 `memory`/`websearch`/`emoji`，其余回「未知工具」。
- **后果**：诱导模型输出死工具块，且该工具实为「记忆查历史」的旧命名（现为
  `memory op=search` / `websearch scope=local`）。
- **建议**：把两处 `chat_history` 示例改成 `memory op=search` 或 `websearch scope=local`。

---

## 三、中危（16 处）

### 决策层

- **M1**　`proxy.py:753-759` — websearch 本地段用无向量存储的第二个 `MemoryStore()`（类别 B）。
  旧逻辑：`SearchService(memory_store=MemoryStore(), ...)` 未注入 vector_store，走子串匹配。
  新逻辑：`_memory_store()`（`:818-830`）已建立**共享**带 VectorStore 的 store，注释明确
  「injector/tool/extractor 共用同一个 store」。接线：websearch `scope=local` 绕过共享 store，
  语义检索在该路径永不生效。**建议**：改调 `self._memory_store()`。
- **M2**　`main.py:253-263` + `main.py:16` — on_log_updated 日志 stub 是死代码 + docstring 过时
  （类别 C+A，跨分片与装配层同源）。stub（打印 "decision layer not wired"）被传给
  JourneyManager 并存进 `comp["on_log_updated"]`，但 `main.py:189`
  `set_on_log_updated(proxy.notify_log_updated)` 已用真实 Proxy 处理器覆盖；`comp["on_log_updated"]`
  全仓库无读取点。docstring 第 16 行「决策层（TODO：尚未实现）」也已过时。**建议**：删 stub
  + 过时 TODO 注释。

### 交互层核心

- **M3**　`message_log.py` — backfill 老架构整套死代码 + docstring 过时（类别 C+A）。
  `find_overlap`（`:376`）、`merge_stack` 的 anchor/REBASE/回填分支（`:502-582`）、
  `backfill_runs`（`:70`）/`frames`（`:79`）两表全仓无读写；merge_stack 唯一调用方
  `append_incremental`（`:642`）仅在空日志时调它 → find_overlap 永不执行。顶层 docstring 仍把
  死架构当现行；`frame_align.Entry` 引用不存在的模块。**建议**：删 backfill 死代码 + 重写 docstring。
- **M4**　`session_reader.py:199-215` — `_tag_media` 运行时可达但只计数+日志、不写库（空壳），
  `media_dir`（`:35/39`）、`sid`（`:200`）死参数（归档已搬到 `media_archive`）（类别 E+C）。
  其 docstring 与模块 docstring 仍宣称「写占位符」。**建议**：删空壳与死参数，改 docstring。
- **M5**　`unified_queue.py:327-362` — `requeue_action`「向后兼容接口」生产零调用，仅
  `tests/test_unified_queue.py:115` 在养它，docstring 自认「重试计数失效」（类别 D）。
  **建议**：删接口与对应测试（连带 `describe`（`:386`）死函数）。

### Android 端口

- **M6**　`device_ctl.py:207-239` — `tap(x,y)/swipe` 旧签名兼容包装（类别 D）。`src/` 主运行时
  已迁到 `tap_rect/swipe_zone`；`src/` 内旧签名调用方（moments_reader.py:266、realtime_scan.py）
  本身是死模块；但 8+ 个 `scripts/`（sync_group_members、spider_group_members、
  run_scroll_scan、eval_quality、scroll_capture、scroll_scan、scan_and_publish）仍大量调用。
  **建议**：先定 scripts/ 去留，再决定删旧签名或迁移 scripts。
- **M7**　`state_builder.py:52-81` vs `generic_parser.py:182-215` — special_detector 调用逻辑双份
  （类别 B）。两者都调 detect_avatars/badges/switches/qr_regions；state_builder 永远显式传
  special_elements → generic_parser 走 `_merge_special_elements`，`_fallback_special_elements`
  只在 main() sanity check 可达（生产死）。**建议**：删 generic_parser._fallback_special_elements
  （及 _norm_ocr_items 的样本 JSON 兼容分支），detector 调用收口到 state_builder 一处。
- **M8**　`icon_detector.py:29-48` — 18 个模板注册项大部分是死的（类别 C）。chat_more/voice/
  emoji/plus/send_button 在 `_collect_perception_elements` 直接 return []（state_builder:55-56）；
  switch_on/off、avatar_placeholder、qr_code_icon、close_x、camera_icon 的匹配结果被
  generic_parser 丢弃（只取 back + 顶部搜索/加号/更多）。**建议**：注册表与真实消费对齐，
  删不接线模板或让 generic_parser 真正合并。
- **M9**　`layout_consts.py:2` vs `device/layout.py:5` — 双坐标事实源（类别 B）。layout_consts
  自称「唯一事实源」，device/layout.py 是第二份（动作层只信 device/layout.py），且其注释引用
  已失效的 `src/v2/layout_consts.py` 路径（模块已迁到 perception/）。**建议**：device/layout.py
  改为 import/派生自 layout_consts，修掉失效路径注释。
- **M10**　`layout_consts.py:22-25,105-107`；`home_parser.py:159-171,266-278`；`page_detector.py:130-166`
  — 未标定常量跑真判定（类别 E）。`BG_PINNED=None` 使 `_is_pinned` 恒 False、`first_page` 恒
  None（置顶/第一页对上层永远不可信）；小程序面板用未标定 `MINIAPP_GRID_MIN_ICONS=4` 等预判
  常量被 detect_page 真实调用，可能误判首页。**建议**：补采样本标定，或给判定加低置信兜底、
  显式暴露「未标定」而非静默 None。

### 网关层

- **M11**　`api/agent.py` `/api/decision_model` 缺 proxy 直调分支（类别 B）。`/api/task_done`、
  `/api/aside` 都有 `if proxy is not None: proxy.inject_*()` 内嵌分支，唯独决策模型切换只走
  `agent_callback_url` 转发。内嵌模式（`main.py:464 create_app(proxy=...)`，
  `agent_callback_url=None`）下 proxy 已注入、`proxy.set_provider()` 存在，热切换却只落盘
  runtime.json、静默「重启后应用」。**建议**：POST 补 proxy 直调分支，与 task_done/aside 对齐。
- **M12**　`api/live.py:75-81` `/api/home_scan` 孤端点（类别 C）。scanner 生产 home_scan.json
  （scanner.py:180「首页红点卡片」）+ 端点 + 测试（test_gateway.py:346-371）+ 文档（GATEWAY.md:61）
  齐全，但前端 index.html 及所有 pages 零调用。**建议**：补红点卡片消费，或删端点+测试。

### config 一致性

- **M13**　`order.txt` user/ 段是死文档（类别 C）。代码只用 `system_blocks()`，user 块由
  `builder.py` 硬编码；且漏列代码实际加载的 `user/goal.md`、`user/known_sessions.md`。
  **建议**：删死段或补全清单。
- **M14**　`tools.md:24` 把 `delegate_task` 错列为 `<tool>`（类别 B）。实为 `<task>` 块非
  `<tool>`，易诱导模型写未知工具块。**建议**：改分类描述。
- **M15**　`monitored` 键三处错位（类别 A/E）。`runtime.py DEFAULTS` 有、`scanner.py:52` 在读，
  但 `runtime.json` 不写、`runtime_schema.py` 不认（PUT 会报 unknown field）。**建议**：三处对齐。
- **M16**　`config/personas/tool_group.yaml` 孤儿卡（类别 C/D）。仅 `common.py:148` 引用它做
  排除、无任何选择逻辑；且其 `identity.description` 因缺 `name` 被 `persona.py:72-80` 渲染
  逻辑丢弃。**建议**：删卡或补 name/选择逻辑。

---

## 四、低危（约 25 处，快速清理清单）

### 死方法 / 死别名 / 死函数

- `proxy.py:306-311` `notify_owner_command()` — 被 `inject_aside`（`:324-338`）取代，零调用。
- `special_scheduler.py:55-63` `update_configs()` — 零调用，改配置需重启。
- `prompt/library.py:125-132` `list_specials()` — 零调用。
- `proxy/cli_backend.py:164-165` `register_backend()` — 零调用却被 `proxy/__init__.py:7,10` 导出。
- `proxy.py:106` `EV_SPECIAL_RUN` — 仅 import 未使用（special 事件用字符串字面量推送）。
- `proxy.py:388-389` `Proxy.stop()` — `_stop` Event 只被 stop() 置位，stop() 全仓库无调用。
- `moments_poster.py:347-348` `post_moments` 别名 — 零调用（只有 post_text_moments 被 import）。
- `avatar_detector.py:246-281` `verify_chat_avatars` — 零调用。
- `icon_templates.py:71` `list_templates` — 零调用。
- `list_parser.py:244,250` `parse_contacts_list`/`parse_settings_list` — 「供 generic_parser 复用」
  但 generic_parser 实际直接调 parse_list_items。
- `wechat_tools.py:516` / `quote_reply.py:262` `_send_btn_visible` — 各有一份，均零调用。
- `device_ctl.py:122` `capture()` 落盘版 — 零调用。
- `device_ctl.py:346-348` `clear_text` — 声称「退回 KEYCODE_DEL」但无此代码，`times` 参数无人读。
- `message_log.py:677` `export_text_log` — 零调用。
- `unified_queue.py:386` `describe` — 零调用。
- `message_log.py:731` `get_sync_version` — 生产零调用（session_reader:115 import 未用）。
- `pages/static/js/replay.js:118-128` `renderK3`/`renderSample`/`renderNewSample` — 被内联调用
  绕过后遗留；`:72` 还引用后端从不产出的 `g.k3_meta`。

### 旧格式只读守卫（保留，但收紧）

- `media.py:26,46-47` `_LEGACY_MARK = "内容:"` — 旧写入方已消失只剩只读守卫；`needs_convert` 用
  裸 `"内容:"`（非 `"\n内容:"`）匹配，正文恰好含「内容:」的多媒体消息会被误判跳过转换。
  建议收紧为 `"\n内容:"`。
- `message_log.py:266-274` `_strip_annotation` — 历史库兼容，保留。

### 双副本 / 双真相源

- `prompt/builder.py:19-28` `_EXTRACTION_CHECKLIST` vs `extractor.py:25-34` `EXTRACTION_CHECKLIST`
  — 两份几乎一致的提取清单。建议合并为一份。
- special prompt frontmatter 的 `rate_per_day`/`active_hours` — 死配置，调度器只读
  runtime.json `special_prompts`（双真相源）。
- `runtime.py DEFAULTS` 缺 14 个新键默认：`screen_watch_interval`、`task_timeout_s`、
  `tool_model_mm`、`tool_model_text`、`friend_auto_accept`、`friend_check_interval`、
  `friend_max_accept`、`decision_provider`、`decision_model`、`decision_token_floor/ceiling`、
  `extract_provider`、`extract_model`、`special_prompts`。
- `muted_until` / `task_retention_days` — 三方都有但零读取（死键）。
- `tool_model` — 被 `tool_model_mm`/`tool_model_text` 取代仍保留。
- `runtime.py:63-66` `.config` 兼容属性 + `__getattr__` 双通道 — `scanner.py:52/139` 仍用旧
  `runtime.config.monitored` / `getattr(config, ...)`，未迁到新 `get()` API。

### 过时注释 / 文档

- `app.py:5` 蓝图枚举只写 4 域（实 10 域：agent/memory/config/live/models/emojis/replay/
  sessioncfg/messages/tts）。
- `supervisor.py:280`「等价旧 restart.sh」— restart.sh 已被改用途（只重启网关），
  supervisor.restart() 重启的是 agent 子进程。
- `hot_reload.py:72`「返回 True 表示全部成功」— 函数体无 return（实返 None）。
- `icon_templates.py:7-10` TODO「模板从旧仓库迁移」— 迁移其实已完成（23 张模板齐全、旧
  samples/ 已删），TODO 是过期描述。
- `image_sender.py:26`「真实设备验证本轮未做」— 与 2026-08-13 真机标定注释矛盾。
- `reader/__init__.py:2,4` docstring「回填/打标/只暴露两个接口」— 均过时。
- `wechat_tools.py:60`「re-export 保持向后兼容」— 无任何外部消费者（都直接 import
  shared.name_match）。
- `memory/__init__.py:8-12` docstring 仍写旧 `value=` 属性名（代码已 value/content 双收，
  prompt 已统一 content）。
- `perception/layout_consts.py:2`「唯一事实源」— 与 device/layout.py 并存，陈述不成立。
- `device/layout.py:5` 引用 `src/v2/layout_consts.py` — 路径已失效（模块已迁到
  perception/，src/v2 目录已不存在）。
- `reader.py:22` / `message_log.py:16,506` 提到 `frame_align.Entry` — 模块已不存在，只剩
  duck-type 语义注释。

### 根脚本（非 src 目录，相关）

- 仓库根 `restart.sh` 与 `run.sh stop/-d` 功能重叠，且 supervisor 文档仍引用旧 restart.sh 语义。
  建议评估去重。

---

## 五、已核实为良性（明确不报，供对照）

- `Policy.fallback_bundle`（rules.py:135 + proxy.py:521）— 设计兜底（必回场景两次沉默后发
  固定「在的，刚看到」），已接线、可达、无被取代痕迹。
- `provider/gemini.py` — **不是死代码**。经 `factory.create_provider(prefer="gemini")` +
  `sessioncfg.py:5,78` + `model_catalog` + `main.py:390` 热切换全部接线。docstring 的「503」是
  「OpenAI 兼容 /v1 不支持 gemini 模型名」的解释性注释。
- provider 工厂 `factory.py:47-64` deepseek↔kimi↔gemini API key 回退 — 明确设计（缺 key 换 provider）。
- handler 注册表（handlers/__init__.py 7 个 handler 全 `@register_handler`，events.py 7 个事件
  类型全有对应 handler）— 无孤儿。
- `journey.py:15`「暂不设计」与 `ABSORB_FUSE_ROUNDS` 不矛盾（docs/INTERACTION_LAYER.md:124 一致）。
- `message_log.py:120-124` `_MIGRATIONS` ALTER TABLE 补列机制 — 完整正确。
- `image_sender.py:217` 硬编码坐标 fallback — 与 2026-08-13 真机标定一致。
- 截图 ImageReader→screencap 回退、OCR 失败降级占位、ChromaDB 不可用回退子串、模板缺失降级 —
  均为设计降级。

---

## 六、建议清理顺序

**第一批（会静默出错/丢功能，立即）**
1. H3 向量库启动删库 → 不删 collection + 回灌索引。
2. H4 factory 消费 DEFAULT_MODELS，消灭 deepseek-chat 漂移。
3. H8 default.yaml 删 chat_history 幽灵工具示例。

**第二批（写了没接的高成本死链，决策接线 or 删）**
4. H2 moments_reader 读 feed 链。
5. H5 realtime_scan + roster_matcher。
6. H6 scroll_stitch。
7. M12 /api/home_scan、M11 decision_model 内嵌热切换缺口。

**第三批（双解析器/双事实源收敛，收益大但要小心）**
8. H1 chat_parser ↔ chat_slicer 职责拆分。
9. M9/M7/M8 layout_consts / special_detector / icon_detector 收敛单一来源。
10. M15/M16 config 三处错位与孤儿卡。

**第四批（死代码/过时注释批量清理，零风险）**
11. 全部低危清单（约 25 处）+ M2/M3/M4/M5 交互层核心死代码。
