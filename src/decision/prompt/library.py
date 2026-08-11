# -*- coding: utf-8 -*-
"""prompt/library.py — prompt 块文件库：按 order.txt 装配，mtime 热重读。"""

import logging
import os
import re

log = logging.getLogger("decision.prompt.library")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
PROMPTS_DIR = os.path.join(PROJECT_ROOT, "config", "prompts")
SPECIAL_DIR = os.path.join(PROMPTS_DIR, "special")

# 简易 YAML frontmatter 解析（不依赖 pyyaml：够用即可）
_FRONT_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)


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

    # ---------------------------------------------------------------- special
    def load_special(self, name: str) -> dict:
        """加载 config/prompts/special/<name>.md。

        返回 {"meta": {...}, "system": "..."}。
        meta 从 YAML frontmatter 解析，system 是 body 部分。
        文件不存在或格式错误时返回 None。
        """
        path = os.path.join(SPECIAL_DIR, f"{name}.md")
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            log.warning("special prompt 文件缺失: %s", path)
            return None

        # 解析 frontmatter
        m = _FRONT_RE.match(raw)
        if not m:
            log.warning("special prompt 缺少 YAML frontmatter: %s", path)
            return None

        meta = self._parse_frontmatter(m.group(1))
        system = raw[m.end():].strip()
        return {"meta": meta, "system": system}

    @staticmethod
    def _parse_frontmatter(yaml_text: str) -> dict:
        """最简 YAML 解析：只支持 key: value（字符串/数字/布尔）。"""
        meta = {}
        for line in yaml_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # 布尔
            if val.lower() in ("true", "yes"):
                val = True
            elif val.lower() in ("false", "no"):
                val = False
            # 数字
            else:
                try:
                    val = float(val)
                    if val == int(val):
                        val = int(val)
                except ValueError:
                    pass
            meta[key] = val
        return meta

    def list_specials(self) -> list:
        """列出所有可用的 special prompt 名称。"""
        names = []
        if os.path.isdir(SPECIAL_DIR):
            for fn in sorted(os.listdir(SPECIAL_DIR)):
                if fn.endswith(".md"):
                    names.append(fn[:-3])
        return names
