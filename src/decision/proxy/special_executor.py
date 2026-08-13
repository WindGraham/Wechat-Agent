# -*- coding: utf-8 -*-
"""proxy/special_executor.py — SpecialExecutor：特殊 prompt 的加载与执行。

从 Proxy 抽出的"特殊任务"职责（cat_diary / memory_consolidation 等）：
  加载 prompt 文件 → 收集上下文 → 调便宜模型 → 按 output_mode 分流执行
  （memory / task / tool / text）。
"""

import logging
import os
import time

from ...shared.xml_blocks import extract_blocks, parse_attrs

log = logging.getLogger("decision.proxy.special_executor")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


class SpecialExecutor:
    """特殊 prompt 执行器（由 SpecialRunHandler 调用）。"""

    def __init__(self, builder, extract_provider_fn, memory_svc,
                 tool_executor, start_task_fn, clock, journal_fn, clip_fn):
        self._builder = builder                # ContextBuilder（build_special）
        self._extract_provider_fn = extract_provider_fn  # callable -> provider
        self._memory_svc = memory_svc          # MemoryService
        self._tools = tool_executor            # ToolExecutor
        self._start_task = start_task_fn       # callable(session, block)
        self._clock = clock
        self._journal = journal_fn
        self._clip = clip_fn

    # ---------------------------------------------------------------- 主流程
    def run(self, prompt_name: str, session: str):
        """执行一个特殊 prompt。根据 output_mode 分流：
        memory / task / tool / text。"""
        spec = self._builder._lib.load_special(prompt_name)
        if spec is None:
            log.warning("[%s] special prompt 加载失败", prompt_name)
            return

        meta = spec["meta"]
        mode = meta.get("output_mode", "memory")

        ctx = self._collect_special_context(prompt_name, meta)
        ctx["time"] = time.strftime("%Y-%m-%d %H:%M %A",
                                    time.localtime(self._clock()))

        messages = self._builder.build_special(prompt_name, ctx)
        if not messages:
            return

        try:
            provider = self._extract_provider_fn()  # 特殊 prompt 走便宜模型
            if hasattr(provider, "chat_full"):
                out, thinking = provider.chat_full(messages)
            else:
                out, thinking = provider.chat(messages), ""
        except Exception as e:
            log.warning("[%s] special LLM 调用失败: %s", prompt_name, e)
            return

        self._journal("special_run", prompt=prompt_name, mode=mode,
                      output=self._clip(out))

        if mode == "memory":
            self._exec_special_memory(out, session)
        elif mode == "task":
            self._exec_special_task(out, session, meta)
        elif mode == "tool":
            self._exec_special_tool(out, session)
        elif mode == "text":
            self._exec_special_text(out, prompt_name, meta)

    # ---------------------------------------------------------------- 上下文
    def _collect_special_context(self, prompt_name: str, meta: dict) -> dict:
        """收集特殊 prompt 需要的上下文数据。"""
        ctx = {}
        mems = self._memory_svc.store().list_scope("all", limit=30)
        if mems:
            ctx["memories"] = [
                {"source": m.get("_file", "?"),
                 "content": m.get("content", "")[:120]}
                for m in mems[:20]
            ]
        return ctx

    # ---------------------------------------------------------------- 各模式
    def _exec_special_memory(self, llm_output: str, session: str):
        """memory 模式：只执行记忆工具块。"""
        extractor = self._memory_svc.extractor()
        n = extractor._execute_memory_blocks(llm_output, session)
        log.info("[special] memory 模式: 执行 %d 个操作", n)

    def _exec_special_tool(self, llm_output: str, session: str):
        """tool 模式：执行 LLM 输出里的 <tool> 块（如 moments 发圈）。"""
        for b in extract_blocks(llm_output):
            if b.valid and b.tag == "tool":
                result = self._tools.exec(b, session)
                log.info("[special] tool 模式: %s → %s",
                         parse_attrs(b.attrs).get("name"), result)

    def _exec_special_task(self, llm_output: str, session: str,
                           meta: dict):
        """task 模式：把 LLM 生成的 <task> 块委派给 CLI backend。"""
        blocks = [b for b in extract_blocks(llm_output)
                  if b.valid and b.tag == "task"]
        if not blocks:
            log.warning("[special] task 模式: 无 <task> 块")
            return
        for b in blocks:
            self._start_task(session, b)

    def _exec_special_text(self, llm_output: str, prompt_name: str,
                           meta: dict):
        """text 模式：保存文本到文件。"""
        save_dir = os.path.join(PROJECT_ROOT, "workspace", "memory",
                                prompt_name)
        os.makedirs(save_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
        path = os.path.join(save_dir, f"{ts}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(llm_output.strip())
        log.info("[special] text 模式: 已保存 %s", path)
