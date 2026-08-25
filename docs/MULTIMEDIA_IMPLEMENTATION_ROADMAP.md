# 微信多模态消息处理实现路线图

> 状态：截至 2026-08-25，分类器与单类型处置已真机验证；**链接读取已找到 root+setuid 直读剪贴板的方案（见 §2.2.1，已真机验证返回 URL）**；统一接入交互层尚未完成。  
> 目标：把多媒体消息（图片、视频、表情包、链接、聊天记录转发卡、文件卡、红包）完整接入日常 journey 流水，所有中间文件落盘，测试期不自动删除。

---

## 1. 当前已验证的能力（不要再重做，只需封装）

### 1.1 分类器 `media_classifier.py`

- 位置：`src/interaction/ports/android/perception/media_classifier.py`
- 已验证样本：473 个组件，0 unknown。
- 输出类型：
  - `text`：普通文本气泡
  - `quote`：引用回复（气泡下方带卡片）
  - `media`：图片/视频（大图块，非调色板，area≥20000）
  - `card`：聊天记录转发卡 / 文件卡（OCR 正则 `^.+的聊天记录$` 或 `^\d+(\.\d+)?\s?(KB|MB|GB)$`）
  - `red_packet`：微信红包
- 背景色：固定灰 `(92,92,92)`，所有群聊无自定义背景（已确认）。
- 自己气泡绿：`BGR(116,180,60)`，他人气泡灰：`BGR(44,44,44)`，容差 24。

### 1.2 表情包处置

- 操作：长按/点击 → 进入详情页
- 页面签名：底部出现「更多表情」「添加」字样
- 提取：上半屏最大非背景连通块
- 落盘：`workspace/media/stickers/{group}_{ts}_{idx}.png`
- 已验证：存出 519×486 PNG 一张。

### 1.3 图片处置

- 正确链路（用户纠正）：
  1. 点图片 → 全屏查看器
  2. **长按图片** → action sheet
  3. 点「保存图片」（native 坐标约 111,1884）
  4. 保存到 `/sdcard/Pictures/WeiXin/mmexport*.jpg`
  5. `adb pull` 到 `workspace/media/images/`
  6. 删除手机源文件
  7. **只按一次 back** 回会话（按两次会退到首页）
- 已验证：成功 pull 到 2275×1279 原图。

### 1.4 聊天记录转发卡处置

- 操作：点击卡片
- 页面签名：顶部标题「xxx 的聊天记录」+ 日期区间 + 每条带精确时间戳
- 提取：页面可滚动 OCR 全量抓取
- 已验证：页面可进入并识别顶部标题。

### 1.5 链接处置（部分验证）

- 操作：点击链接 → webview → 右上角 ⋯ → 底部面板点「复制链接」
- 已验证：复制成功，toast「已复制到剪贴板」。
- **未解决**：读取剪贴板内容。

---

## 2. 当前唯一硬卡点：链接读取

### 2.1 已排除的方案

| 方案 | 结果 | 原因 |
|------|------|------|
| `service call clipboard 1` | ❌ | 微信复制的是富 ClipData（含缩略图），Parcel 720KB+，触发 `Allocation of size ... above allowed limit of 1MB` |
| Appium Settings clipboard broadcast | ❌ | Android 10+ 剪贴板权限收紧，返回空 |
| `trash/clipper.apk` | ❌ | 实际包名是 `io.appium.settings` 的改名，无真实 clipper 功能 |
| Termux:API `termux-clipboard-get` | ❌ | Termux:API 未安装 |
| 粘贴到输入框 + uiautomator 读 | ❌ | 粘贴成功，但微信输入框对无障碍服务不可见，`uiautomator dump` 返回空 hierarchy |

### 2.2 推荐实现路线

**路线 A（已实装并真机验证，首选）：root + setuid(聚焦 App) 直读剪贴板。**

这是「剪贴板直读」的可行实现，绕开了 Android 15 的读门控与 Parcel 1MB 上限。见 §2.2.1。

**路线 B（回退方案）：粘贴到输入框 + 截图 OCR + 清空输入框**（原首选，保留为 `read_clipboard` 失败时的 fallback）。

理由（B 仍可用，已实测 rapidocr 置信度 0.99+）：不依赖剪贴板权限、Parcel 大小、无障碍服务；微信输入框区域固定，可精确裁剪。

### 2.2.1 剪贴板直读（已解决，2026-08-25 真机验证）

| 项 | 值 |
|----|----|
| 设备 | OnePlus 6T / Android 15 (API 35) / Magisk root |
| 门控 | Android 15 剪贴板读权限 = 读取者必须是「默认 IME」**或**「聚焦 App」；root/shell 一律返回 null |
| 命中方案 | `app_process` 以 root 跑 → `setuid(聚焦 App 的 uid)` → `android.content.IClipboard.getPrimaryClip(pkg,null,0,0)` 取 `ClipData` item 文本 |
| 关键点 | 读前必须 `setuid` 到**当前聚焦 App** 的 uid（微信=10195，动态从 `dumpsys window mCurrentFocus` + `dumpsys package` 的 `appId=` 解析）；SetPrimaryClip 无需门控，root 直接可写 |
| ≥1MB 富剪贴板 | 无论客户端，透过 binder 传富 ClipData 都会被 `Allocation ... above allowed limit of 1MB` 拦截（事务上限）；URL 是纯文本小 clip 不受影响。大图剪贴板直接读不可行，需回落 B |

实现产物：
- `tools/clipboard/ClipIO.java`（一次性）+ `tools/clipboard/ClipIOServer.java`（常驻，根因见下）+ `tools/clipboard/build.sh`（javac + d8/r8）+ `tools/clipboard/dex/clip.dex`（预编译，可直接 push）。
- `DeviceCtl.read_clipboard()` / `set_clipboard()`（`device_ctl.py`）：优先走**常驻服务**（见下），服务不可用才回退一次性 app_process。
- `MediaHandler._read_link_from_webview()`：复制链接后**先** `read_clipboard()`，失败回退 paste→OCR。

**常驻（daemon）方案 —— 读 ~0ms，不干扰 ADB 输入法：**
- 一次性 `app_process` 每次起 zygote + 框架，读一次 ~1.6s。为满足「复制后快速读」，改成**常驻服务**：`ClipIOServer` 以 root 启动，绑定 tcp 7001，**先 bind 再 setuid 到聚焦 App（微信 10195）**，之后每次读只是 client socket 往返。
- 不影响 `ADBKeyboard` 输入法：不改 `default_input_method`、不 `ime set`、不发 `ADB_INPUT_TEXT/ADB_CLEAR_TEXT`、不抢焦点；只是后台读剪贴板这一只读 binder 调用。
- `DeviceCtl._clip_ping()` 用完整的 `P` 心跳往返判断服务健在（避免 adb forward 的「能连上但服务已死」假阳性）；服务死后 `read_clipboard()` 自动重启。

真机结果：`read_clipboard()` 对 `https://mp.weixin.qq.com/s/s9FIHjp5gCXTqFThk1KbSQ` 返回原样 URL，**常驻后读稳定 ~10~20ms**（一次性回退时 ~1.6s）；读写前后 `default_input_method` 均为 `com.android.adbkeyboard/.AdbIME`。


### 2.3 路线 B 详细步骤

1. 链接 webview 中完成「复制链接」。
2. 返回会话（按 back）。
3. 点击输入框（坐标约 400,2080），获取焦点。
4. 长按输入框，弹出「粘贴」菜单。
5. 点击「粘贴」（坐标约 110,1970），URL 进入输入框。
6. **截图当前屏幕**。
7. **裁剪输入框区域**：底部固定条，y 范围建议 `[0.86*h, 0.98*h]`，全宽。
8. **OCR 识别**：得到 URL 文本。
9. **清空输入框**：长按 → 全选 → 删除（或点输入框右侧删除图标），确保不残留、不触发发送。
10. 返回 URL 字符串给上层。

### 2.4 路线 B 的 OCR 选型

当前主 venv（`~/.venvs/wechat-agent/bin/python`）无 easyocr / pytesseract。需要先确认项目 OCR 入口：

- `src/interaction/ports/android/perception/ocr_engine.py`
- 若项目 OCR 使用 Gemini / 本地 cv2 / 其他模型，直接复用该入口。
- 若 OCR 引擎无法识别固定区域 URL，可临时安装 `easyocr` 或 `pytesseract`，但优先复用现有依赖。

### 2.5 路线 B 的落盘要求

- 截图：`workspace/collect_debug/links/{ts}/screen_after_paste.png`
- 裁剪：`workspace/collect_debug/links/{ts}/input_box_crop.png`
- OCR 原始结果：`workspace/collect_debug/links/{ts}/ocr_raw.json`
- 最终 URL：`workspace/collect_debug/links/{ts}/url.txt`

---

## 3. 统一多媒体处理模块

### 3.1 目标模块

- 新建：`src/interaction/ports/android/perception/media_handler.py`
- 职责：接收分类结果 → 执行点击/长按/打开 → 按页面签名分流 → 取回内容 → 清理现场 → 返回统一结构。

### 3.2 输入结构

```python
@dataclass
class MediaTask:
    msg_id: str           # 消息唯一 id
    msg_type: str         # classifier 输出：text/quote/media/card/red_packet/...
    bbox: Tuple[int,int,int,int]  # 单条消息在屏幕上的包围框 (x,y,w,h)
    screen_path: str      # 当前整屏截图路径
    group_name: str
    sender_hint: str      # 发送者昵称/头像提示
```

### 3.3 输出结构

```python
@dataclass
class MediaResult:
    msg_id: str
    msg_type: str
    content: Any          # 文本 / 图片路径 / 视频路径 / 表情包路径 / 链接 URL / 聊天记录 dict
    raw_files: List[str]  # 所有中间文件路径
    success: bool
    error: Optional[str]
```

### 3.4 各类型处置流程详细设计

#### 3.4.1 文本 `text`

- 无需点击，直接 OCR 气泡内文字。
- 落盘：单条消息裁切图、OCR 结果。

#### 3.4.2 引用 `quote`

- 分类器已识别为引用：气泡下方带卡片。
- 处理：点击打开引用详情页（或直接在气泡 OCR）→ 提取原文 + 被引用内容。
- 需验证：点击引用气泡后的页面签名与返回逻辑。

#### 3.4.3 图片 / 视频 `media`

- 判断子类型：
  - 若气泡区域有大图块且非调色板 → 图片/视频
  - 若存在 `视频` 字样或时长文本 → 视频
  - 否则默认图片
- 处理：
  1. 点击 media 区域 → 全屏查看器
  2. 截图查看器（落盘）
  3. **长按图片/视频** → 保存
  4. 等待文件出现在 `/sdcard/Pictures/WeiXin/` 或 `/sdcard/Movies/WeiXin/`
  5. `adb pull` 到 `workspace/media/images/` 或 `workspace/media/videos/`
  6. 删除手机源文件
  7. 按一次 back 回会话
- 视频需额外处理：
  - 视频查看器可能有播放控件，保存选项可能为「保存视频」。
  - 保存路径可能在 `/sdcard/Movies/WeiXin/`。
  - 大视频需等待下载完成（通过轮询文件大小稳定判断）。

#### 3.4.4 聊天记录转发卡 `card`

- 处理：
  1. 点击卡片 → 进入聊天记录详情页
  2. 截图页面顶部验证标题「xxx 的聊天记录」
  3. 滚动页面，逐屏 OCR
  4. 每条消息按现有 `cutline_segment` / `chat_parser` 流程解析
  5. 返回结构化聊天记录（含发送者、时间、内容）
  6. 按 back 回会话
- 落盘：
  - 详情页截图：`workspace/media/chat_records/{group}_{ts}/screens/`
  - 解析结果：`workspace/media/chat_records/{group}_{ts}/record.json`

#### 3.4.5 文件卡 `card`

- 识别：`^\d+(\.\d+)?\s?(KB|MB|GB)$` 文件大小行。
- 处理：
  1. 点击文件卡 → 进入文件预览或下载
  2. 尝试「用其他应用打开」或「保存到手机」
  3. 下载完成后 pull 到 `workspace/media/files/`
  4. 删除手机源文件
  5. 返回会话
- **注意**：文件类型未在当前语料中大量出现，需后续补充测试。

#### 3.4.6 红包 `red_packet`

- 识别：OCR 含「微信红包」。
- 当前策略：记录为红包事件，不自动点开（避免资金/礼仪风险）。
- 输出：`{"type": "red_packet", "text": "[微信红包]"}`。

#### 3.4.7 链接 `card/link`

- 处理见第 2 节路线 B。

#### 3.4.8 表情包 `media/sticker`

- 处理见 1.2。
- 需要判断：media 类型中，若点开后页面签名含「更多表情」「添加」，则按表情包流程；否则按图片流程。

### 3.5 页面签名库

`media_handler.py` 必须维护一个页面签名判断模块：

```python
PAGE_SIGNATURES = {
    "sticker_detail": ["更多表情", "添加"],
    "photo_viewer": ["转发", "收藏", "编辑", "删除"],  # 长按菜单关键词
    "photo_viewer_fullscreen": ["下载", "更多"],  # 底部 4 图标
    "webview": ["复制链接", "刷新", "在浏览器打开"],
    "chat_record": ["的聊天记录"],
    "file_card": ["文件大小", "KB", "MB", "GB"],
}
```

判断方式：截图 + OCR 顶部/底部固定区域，匹配关键词。

### 3.6 异常回退

每种处置类型都必须有超时与回退：

- 若点击后 5 秒内未识别到预期页面签名，截图保存到 `workspace/collect_debug/errors/` 并 back 回会话。
- 连续失败 3 次，标记该消息为 `unprocessed`，记录截图路径，不阻塞后续消息。
- 任何异常必须保留最后一张屏幕截图供人工复盘。

---

## 4. 接入交互层

### 4.1 接入点

需要修改以下文件，把 `text-only` 假设替换为 `media_handler` 调用：

1. `src/interaction/loop/realtime_scan.py`
2. `src/interaction/loop/session_reader.py`
3. `src/interaction/loop/history_collect.py`
4. `src/interaction/loop/cutline_segment.py`（已识别单条消息 bbox）

### 4.2 接入伪代码

```python
from src.interaction.ports.android.perception.media_classifier import classify_components
from src.interaction.ports.android.perception.media_handler import MediaHandler

handler = MediaHandler(device_ctl)

for msg in messages:
    if msg.type in ("text",):
        # 现有 OCR 流程
        pass
    else:
        result = handler.handle(MediaTask(
            msg_id=msg.id,
            msg_type=msg.type,
            bbox=msg.bbox,
            screen_path=current_screen_path,
            group_name=group_name,
            sender_hint=msg.sender,
        ))
        if result.success:
            message_log.append(result.to_message_entry())
        else:
            message_log.append_error(msg.id, result.error, result.raw_files)
```

### 4.3 状态机保护

交互层必须保证：

- 处理多媒体消息前，记录当前会话位置书签（截图 + 滚动偏移）。
- 处理完成后必须回到原会话、原滚动位置（或至少回到最新记录顶部）。
- 如果 back 操作导致退出会话（按两次 back），必须能从首页重新进入该群聊并恢复位置。

### 4.4 与花名册系统的联动

- 多媒体消息同样需要发送者识别。
- 图片/视频/表情包类消息：发送者头像在气泡左侧，调用 `roster_matcher` 匹配。
- 链接/聊天记录卡：发送者头像同样在左侧，正常匹配。
- 若匹配失败，触发 `roster_update.reconcile_on_mismatch(auto_back=True)`：
  1. tap 头像 → 资料页
  2. 提取昵称/头像
  3. back 回聊天页
  4. 继续处理

---

## 5. 中间文件落盘规范

### 5.1 必须保留的中间文件

测试阶段全部保留，不自动删除：

1. 每屏原始截图：`workspace/collect_debug/{group}/{run_id}/screens/raw_{screen_idx}.png`
2. 一次裁切（去掉状态栏/输入栏）：`workspace/collect_debug/{group}/{run_id}/screens/cropped_{screen_idx}.png`
3. 二次裁切/拼接结果：`workspace/collect_debug/{group}/{run_id}/stitched/`
4. 单条消息裁切：`workspace/collect_debug/{group}/{run_id}/messages/{msg_id}/crop.png`
5. 单条消息识别结果：`workspace/collect_debug/{group}/{run_id}/messages/{msg_id}/ocr.json`
6. 多媒体原始文件：`workspace/media/{images|videos|stickers|files|chat_records}/{group}_{ts}/`
7. 链接处置截图：`workspace/collect_debug/links/{ts}/`
8. 错误现场截图：`workspace/collect_debug/errors/{ts}.png`

### 5.2 文件命名规范

- 时间戳统一使用 `YYYYMMDD_HHMMSS_mmm` 或 `YYYY-MM-DD_HH-MM-SS`（与现有代码保持一致）。
- 群聊名称中的特殊字符替换为下划线。
- 每个消息必须有一个稳定 `msg_id`，建议：`{group}_{screen_idx}_{msg_idx}_{hash}`。

### 5.3 Manifest 规范

每个 run 目录必须包含 `manifest.json`：

```json
{
  "group": "陈曦猫猫群",
  "run_id": "deep_20260825_180943",
  "screens_count": 47,
  "messages": [
    {
      "msg_id": "...",
      "type": "media",
      "screen_idx": 3,
      "bbox": [156, 400, 800, 200],
      "files": ["..."],
      "result": "..."
    }
  ]
}
```

---

## 6. 测试计划

### 6.1 单元测试

- 更新/新增 `tests/test_media_classifier.py`（已部分覆盖，继续补充文件卡、红包）。
- 新增 `tests/test_media_handler.py`：用录屏回放或 mock device_ctl 测试页面签名判断。

### 6.2 真机冒烟测试

| 用例 | 预期结果 |
|------|----------|
| 猫猫群链接消息 | 复制 → 粘贴 → OCR 读取 URL → 清空输入框 → 返回 URL |
| 猫猫群图片消息 | 点击 → 长按保存 → pull → 删除源文件 → 返回图片路径 |
| 猫猫群表情包 | 点击 → 详情页 → 裁最大块 → 保存 PNG |
| 猫猫群聊天记录转发 | 点击 → 滚动 OCR → 返回结构化记录 |
| 群聊红包 | 识别为红包，不点开，记录事件 |
| 错误回退 | 点击非预期页面 → 5 秒内无签名 → 截图 → back → 标记失败 |

### 6.3 长时稳定性测试

- 在陈曦猫猫群连续处理 100 条消息，不卡死、不丢失会话位置。
- 检查 `workspace/collect_debug/errors/` 是否有异常截图。

---

## 7. 文档与交付

### 7.1 需要更新的文档

1. `docs/INTERACTION_LAYER.md`
   - 第 8 节或新增「多媒体消息处理」章节。
   - 说明分类器规则、各类型处置流程、中间文件落盘路径。
2. `docs/MULTIMEDIA_IMPLEMENTATION_ROADMAP.md`（本文档）
   - 完成后在文档末尾标注「已完成」与 commit hash。
3. `README.md`（如存在且需要）
   - 简要说明已支持的多媒体类型。

### 7.2 提交要求

- 分支：`feat/gateway-rewrite`（与花名册任务同分支，或新建 `feat/multimedia-handler`）。
- commit 信息遵循项目现有风格。
- 提交前确保：
  - 所有新增代码有基本注释
  - 单元测试通过
  - 真机冒烟测试通过至少 3 个群聊

---

## 8. 风险与待决策事项

### 8.1 剪贴板直读（长期）

如果后续需要真正的剪贴板直读（不经过粘贴）：
- 需要配置 Android SDK；
- 给 `android/CaptureServer/CaptureServer.java` 增加 `CLIPBOARD` 命令；
- 重新编译并部署 classes.dex；
- 注意 Android 15 的剪贴板访问限制，可能需要以特定用户/包名运行。

**当前决策：先不做，用输入框 OCR 替代。**

### 8.2 视频大文件

- 视频可能未完全下载，保存前需轮询文件大小稳定。
- 大视频 pull 耗时较长，需要设置合理超时（30s ~ 120s）。

### 8.3 文件卡

- 当前语料不足，实现时先做基础路径，遇到真实样本再细化。

### 8.4 红包

- 是否自动点开领取？**当前决策：不自动点开**，仅记录事件，避免资金与社交风险。

### 8.5 输入框 OCR 的准确性

- 输入框背景深色，URL 白色/浅色文字，OCR 应该稳定。
- 如果 OCR 失败，可 fallback 到把整个截图发给 Gemini 或其他多模态模型读 URL。

---

## 9. 任务清单（可直接勾选）

- [x] 链接读取：root+setuid 直读剪贴板 `read_clipboard()`（真机返回 URL；见 §2.2.1）；输入框 OCR 保留为回退
- [x] 链接读取：`media_handler._read_link_from_webview()` 直读剪贴板，OCR 回退；真机 e2e 验证通过（游泳馆群「学生助理招新通知」→ 复制链接 → 常驻读 → 返回 `https://mp.weixin.qq.com/s/GAV9uRX7RivF6calz-RNOw`）；webview 签名漏判已修复（unknown→链接流+OCR找复制链接）
- [x] 图片处置：`_handle_media`/`_save_photo_or_video`（真机签名探测过；照片保存流待真实照片样本）
- [x] 视频处置：`_save_photo_or_video(is_video=True)`（代码；视频样本待验证）
- [x] 表情包处置：`_handle_sticker`/`_handle_sticker_detail`（已真机验证）
- [x] 聊天记录卡：`_read_chat_record` + 改进版 `_parse_chat_record_screen`（已真机验证，解析按时间/发送者/内容）
- [x] 文件卡：`_handle_file`（基础版，OCR/CV 定位；语料不足待样本细化）
- [x] 红包：`_handle_red_packet()`（仅记录）
- [x] 页面签名库：`_detect_page_signature`（chat_record/webview/photo/video/sticker/file_card 全判，真机验证 chat_record+sticker）
- [x] 异常回退：`handle()` try/except + `_save_error_shot` + `_return_to_chat`
- [x] 接入 `realtime_scan.py`: 新增默认关闭的 `handle_media` 参数 + `classify_slice_to_task` 桥接（避免改变现有轻量扫描）
- [ ] 接入 `session_reader.py`（`src/interaction/reader/session_reader.py`，未接入）
- [ ] 接入 `history_collect.py`（未接入）
- [x] 中间文件落盘：`MediaResult.run_dir` + `_write_manifest`（每个消息写 manifest.json）
- [x] 单元测试：`tests/test_media_handler.py`（11 项，离线 mock run_ocr）
- [x] 真机冒烟测试：聊天记录卡/表情包/OCR点击/常驻剪贴板读/**链接复制→常驻读**（游泳馆群公众号文章 e2e 返回 URL）已真机验证；图片保存/文件卡待真实照片/文件消息样本
- [ ] 更新 `docs/INTERACTION_LAYER.md`
- [ ] commit push

---

## 10. 当前下一步（立即执行）

1. 验证输入框 OCR：对当前已粘贴 URL 的输入框截图、裁剪、OCR。
2. 若 OCR 成功，实现 `media_handler.handle_link()`。
3. 继续实现其余类型的 `media_handler` 方法。
