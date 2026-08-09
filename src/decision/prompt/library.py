# -*- coding: utf-8 -*-
"""prompt/library.py — prompt 块文件库：按 order.txt 装配，mtime 热重读。"""

import logging
import os

log = logging.getLogger("decision.prompt.library")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
PROMPTS_DIR = os.path.join(PROJECT_ROOT, "config", "prompts")


class PromptLibrary:
    """按 config/prompts/order.txt 装配块文件。

    - system 块、user 块分组返回（order.txt 里 system/ 前缀的进 system）
    - 每块带 mtime 缓存：文件没变不重复读盘
    - {占位符} 用 render() 填充；task_receipt.md 不进默认装配
      （任务回调时由调用方显式取用）
    """

    def __init__(self, prompts_dir=PROMPTS_DIR):
        self._dir = prompts_dir
        self._cache = {}          # relpath -> (mtime, content)

    def _read(self, rel: str) -> str:
        path = os.path.join(self._dir, rel)
        try:
            mtime = os.path.getmtime(path)
            cached = self._cache.get(rel)
            if cached and cached[0] == mtime:
                return cached[1]
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self._cache[rel] = (mtime, content)
            return content
        except OSError:
            log.warning("prompt 块缺失: %s", rel)
            return ""

    def order(self) -> list:
        """order.txt 的装配清单（去注释/空行）。"""
        raw = self._read("order.txt")
        return [ln.strip() for ln in raw.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    def render(self, rel: str, **slots) -> str:
        """读块文件并做 {占位符} 替换（缺失占位符原样保留）。"""
        content = self._read(rel)
        for k, v in slots.items():
            content = content.replace("{%s}" % k, str(v))
        return content.strip()

    def system_blocks(self, **slots) -> list:
        """装配 system 侧块（order.txt 中 system/ 前缀的）。"""
        return [self.render(rel, **slots) for rel in self.order()
                if rel.startswith("system/")]

    def user_block(self, name: str, **slots) -> str:
        """取单个 user 块（如 'session_info'/'history'/'new_messages'/
        'task_receipt'）。"""
        return self.render(f"user/{name}.md", **slots)
