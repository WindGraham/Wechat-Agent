# -*- coding: utf-8 -*-
"""reader — 读取与同步：切段、增量、回填、打标。

对决策层只暴露两个接口：get_context() 和 get_new_since()。
内部整合端口感知 + 消息日志。
"""

from .session_reader import SessionReader
