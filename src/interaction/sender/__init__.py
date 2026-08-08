# -*- coding: utf-8 -*-
"""sender — bundle 解释执行：拆句、引用、发图、发文件。

接收决策层的 XML 动作包（<reply>/<silent/> 块），
翻译成端口操作并执行。内部处理：
- 执行顺序与屏幕互斥
- 拟人拆句连发（随机延迟/打字节奏）
- 引用回复（长按气泡→菜单→引用→输入→发送）
- 发图片/文件（加号面板流程）
"""

from .bundle_sender import BundleSender
