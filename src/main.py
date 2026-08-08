# -*- coding: utf-8 -*-
"""main.py — 仓库根入口：装配三层并启动交互层主循环。

用法：
    python -m src.main              # 正常启动（设备不可用则进入"等待设备"状态）
    python -m src.main --once       # 只跑一轮主循环（冒烟用）
    python -m src.main --dry-run    # 只装配不启动：打印装配清单后退出（不触设备）

装配顺序（见 docs/WORKSPACE.md / docs/INTERACTION_LAYER.md）：
    1. workspace 目录结构（chatlogs/media/tasks/runtime/locks）
    2. runtime.json 加载（src/shared/runtime.py，mtime 热重读）
    3. 消息日志（msglog，SQLite：workspace/chatlogs/chatlog.db）
    4. 统一时间序队列（interaction/loop/unified_queue.py）
    5. Android 端口（device/action/perception，adb 初始化包 try/except）
    6. 交互层组件：reader / sender / journey / scanner / watcher / loop
    7. 决策层（TODO：proxy/prompt/policy/provider 尚未实现，见下方接线点）

严格约定：本文件只做装配与依赖注入，不含业务逻辑。
"""

import argparse
import logging
import os
import sys
import threading
import time

from .shared.runtime import RuntimeConfig
from .interaction.msglog import message_log
from .interaction.loop.unified_queue import UnifiedQueue

log = logging.getLogger("main")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 设备初始化重试间隔（秒）：adb 连不上时入口进入"等待设备"状态而不是崩溃
DEVICE_RETRY_INTERVAL = 10.0


# ------------------------------------------------------------------ workspace
def ensure_workspace(root):
    """按 WORKSPACE.md §二 创建运行时目录结构。返回各目录路径 dict。"""
    dirs = {
        "chatlogs": os.path.join(root, "chatlogs"),
        "media": os.path.join(root, "media"),
        "tasks": os.path.join(root, "tasks"),
        "runtime": os.path.join(root, "runtime"),
        "locks": os.path.join(root, "runtime", "locks"),
    }
    for path in dirs.values():
        os.makedirs(path, exist_ok=True)
    return dirs


# ------------------------------------------------------------------ 设备装配
def init_device_with_retry(retry_interval=DEVICE_RETRY_INTERVAL,
                           max_retries=None):
    """初始化 Android 端口（WeChatTools → DeviceCtl → adb 检查）。

    设备不可用（adb 连不上）时不崩溃：进入"等待设备"状态，周期性重试，
    直到设备上线或用户 Ctrl+C。max_retries 供测试/冒烟限制重试次数。
    """
    # 延迟 import：--dry-run 路径不加载端口模块（避免 jieba/cv2 等重依赖）
    from .interaction.ports.android.action.wechat_tools import WeChatTools

    attempt = 0
    while True:
        attempt += 1
        try:
            tools = WeChatTools()
            log.info("设备就绪（第 %d 次尝试）", attempt)
            return tools
        except Exception as e:
            if max_retries is not None and attempt >= max_retries:
                raise
            log.warning("设备不可用：%s；%.0fs 后重试（等待设备中，Ctrl+C 退出）",
                        e, retry_interval)
            print(f"[main] 等待设备：{e}（{retry_interval:.0f}s 后第 "
                  f"{attempt + 1} 次尝试，Ctrl+C 退出）", flush=True)
            try:
                time.sleep(retry_interval)
            except KeyboardInterrupt:
                print("[main] 等待设备被用户中断", flush=True)
                sys.exit(2)


# ------------------------------------------------------------------ 装配
def assemble(workspace_root, config_path, with_device=True):
    """装配全部组件。with_device=False 时跳过端口/设备（dry-run 用）。

    返回 (components: dict, manifest: list[str])；manifest 为装配清单行。
    """
    manifest = []
    comp = {}

    # 1. workspace 目录结构
    dirs = ensure_workspace(workspace_root)
    comp["workspace"] = dirs
    manifest.append(f"workspace: {workspace_root} "
                    f"({'/'.join(sorted(dirs))} 已就绪)")

    # 2. runtime.json（mtime 热重读 + 默认值兜底）
    runtime = RuntimeConfig(config_path)
    comp["runtime"] = runtime
    manifest.append(f"runtime: {config_path} "
                    f"(paused={runtime.get('paused')}, "
                    f"sweep_interval={runtime.get('sweep_interval')}, "
                    f"notify_interval={runtime.get('notify_interval')})")

    # 3. 消息日志（SQLite 主库）
    db_path = os.path.join(dirs["chatlogs"], "chatlog.db")
    conn = message_log.connect(db_path)
    comp["msglog_conn"] = conn
    comp["msglog_path"] = db_path
    manifest.append(f"msglog: {db_path}")

    # 4. 统一时间序队列
    queue = UnifiedQueue(max_attempts=runtime.get("action_max_attempts", 2))
    comp["queue"] = queue
    manifest.append(f"queue: UnifiedQueue "
                    f"(max_attempts={runtime.get('action_max_attempts', 2)})")

    # 5-6. 端口 + 交互层（依赖设备）
    if with_device:
        tools = init_device_with_retry()
        _assemble_interaction(comp, manifest, tools, dirs, runtime, queue,
                              conn)
    else:
        manifest.append("device: [dry-run 跳过] Android 端口 "
                        "(WeChatTools/DeviceCtl/FrameBus/Reader/Scanner/"
                        "NotifyWatcher) 不初始化")
        manifest.append("interaction: [dry-run 跳过] SessionReader/"
                        "BundleSender/JourneyManager/InteractionLoop 不装配")

    # 7. 决策层接线点（TODO）
    # TODO(decision): src/decision/{proxy,prompt,policy,provider} 目前只有空
    #   __init__。决策层落地后在此接线：
    #   - Proxy(on_log_updated=comp['on_log_updated'])：
    #     LogUpdated → 五队列分流 → ContextBuilder 装配 prompt → provider
    #   - 决策产物 ActionBundle → comp['queue'].push_action(session, blocks_xml)
    #   - task_receipt 回调 → prompt 块替换 new_messages（见 prompts/order.txt）
    manifest.append("decision: TODO 未接线（proxy/prompt/policy/provider "
                    "为空包；接线点见 main.py 注释）")
    return comp, manifest


def _assemble_interaction(comp, manifest, tools, dirs, runtime, queue, conn):
    """端口就绪后的交互层装配（全部依赖注入，不碰全局状态）。"""
    from .interaction.ports.android.action.navigator import Navigator
    from .interaction.ports.android.action.sender import Sender
    from .interaction.ports.android.perception.frame_bus import FrameBus
    from .interaction.ports.android.perception.reader import Reader
    from .interaction.ports.android.perception.scanner import Scanner
    from .interaction.ports.android.device.notify_watcher import (
        NotifyWatcher, NotificationQueue, dump_wechat_notifications)
    from .interaction.reader.session_reader import SessionReader
    from .interaction.sender.bundle_sender import BundleSender
    from .interaction.loop.journey import JourneyManager
    from .interaction.loop.run_loop import InteractionLoop

    comp["tools"] = tools
    manifest.append("device: WeChatTools (DeviceCtl/adb 已连通)")

    frame_bus = FrameBus(tools)
    port_reader = Reader(tools, frame_bus, conn=conn)
    navigator = Navigator(tools)
    port_sender = Sender(tools)
    manifest.append("ports: FrameBus / Reader / Navigator / Sender")

    owner_nick = runtime.get("owner_nick", "")
    session_reader = SessionReader(port_reader, conn,
                                   media_dir=dirs["media"],
                                   owner_nick=owner_nick)
    bundle_sender = BundleSender(port_sender, navigator, tools)
    manifest.append("interaction: SessionReader / BundleSender")

    notify_queue = NotificationQueue()
    scanner = Scanner(tools, frame_bus, runtime, notify_queue)
    watcher = NotifyWatcher(
        dump_fn=lambda: dump_wechat_notifications(
            tools.dev, owner_nick=runtime.get("owner_nick", "")),
        queue=notify_queue,
        seen_path=os.path.join(dirs["runtime"], "notify_seen.json"),
        interval=tuple(runtime.get("notify_interval", (3, 6))),
        owner_nick=owner_nick,
    )
    manifest.append("discovery: Scanner + NotifyWatcher "
                    "→ NotificationQueue →(bridge)→ UnifiedQueue")

    # 通知桥：NotificationQueue(辅助信号源) → UnifiedQueue(主时间序队列)
    bridge_stop = threading.Event()
    bridge = threading.Thread(
        target=_bridge_notify_queue,
        args=(notify_queue, queue, bridge_stop),
        daemon=True, name="notify-bridge")
    comp["bridge_stop"] = bridge_stop
    comp["bridge"] = bridge

    # TODO(decision): LogUpdated 上行通知暂落日志；Proxy 落地后换成
    #   proxy.on_log_updated（决策层唯一上行入口，CONTRACTS.md §一）
    def on_log_updated(updated):
        log.info("LogUpdated: session=%s version=%d mention_hint=%s "
                 "(decision layer not wired)", updated.session,
                 updated.version, updated.mention_hint)

    comp["on_log_updated"] = on_log_updated

    journey = JourneyManager(queue, session_reader, bundle_sender,
                             navigator, on_log_updated=on_log_updated)
    # InteractionLoop 内部对 wake_and_dim / sweep 已有 try/except 健康检查，
    # 运行期设备掉线按周期容错，不额外包装。
    loop = InteractionLoop(scanner, watcher, queue, journey, tools,
                           config=runtime)
    comp["journey"] = journey
    comp["loop"] = loop
    manifest.append("loop: JourneyManager + InteractionLoop "
                    "(config 热读取自 runtime)")


def _bridge_notify_queue(notify_queue, unified_queue, stop_ev):
    """NotificationQueue → UnifiedQueue 桥接线程（1s 轮询）。"""
    while not stop_ev.is_set():
        entry = notify_queue.pop_next()
        if entry is None:
            stop_ev.wait(1.0)
            continue
        unified_queue.push_notify(session=entry.session,
                                  mention=entry.mention,
                                  source="notify")


# ------------------------------------------------------------------ 入口
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="微信自动化 agent 入口：装配三层并启动交互层主循环")
    parser.add_argument("--workspace",
                        default=os.path.join(PROJECT_ROOT, "workspace"),
                        help="运行时工作区根目录（默认 <repo>/workspace）")
    parser.add_argument("--config",
                        default=os.path.join(PROJECT_ROOT, "config",
                                             "runtime.json"),
                        help="runtime.json 路径（默认 <repo>/config/runtime.json）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只装配不启动：打印装配清单后退出（不触设备）")
    parser.add_argument("--once", action="store_true",
                        help="主循环只跑一轮（冒烟用）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    with_device = not args.dry_run
    comp, manifest = assemble(args.workspace, args.config,
                              with_device=with_device)

    print("════ 装配清单 ════")
    for line in manifest:
        print(f"  {line}")
    print("═══════════════════", flush=True)

    if args.dry_run:
        print("[main] --dry-run：装配完成，不启动循环", flush=True)
        return 0

    comp["bridge"].start()
    loop = comp["loop"]
    try:
        loop.run(once=args.once)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt，停止主循环")
        loop.stop()
    finally:
        comp["bridge_stop"].set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
