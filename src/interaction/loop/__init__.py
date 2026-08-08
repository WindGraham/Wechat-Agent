# -*- coding: utf-8 -*-
"""loop — 运行循环组：统一时间序队列、旅程规则、屏幕互斥。

主循环（红点驱动）：
  回首页 → 解析未读标记 → 有标记的会话逐个进入
  → 滚动读取 → 更新日志 → 回传 LogUpdated
  → 无标记：本轮结束（不进任何会话）

统一时间序队列合并通知条目与行动条目：
- 通知条目：系统通知 + 红点识别
- 行动条目：决策层 submit_bundle
- 去重不挪动 / 行动吞并通知 / @我插队
"""

from .unified_queue import UnifiedQueue, QueueEntry
from .journey import JourneyManager
from .run_loop import InteractionLoop
