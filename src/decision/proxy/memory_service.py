# -*- coding: utf-8 -*-
"""proxy/memory_service.py — MemoryService：记忆注入/提取/工具的统一入口。

从 Proxy 抽出的"记忆"职责：
  - MemoryStore 单例（injector/tool/extractor 共用，避免向量存储不一致）
  - 决策前记忆块注入（MemoryInjector）
  - 后置记忆提取（MemoryExtractor，异步事件）
  - 记忆工具（MemoryTool）
"""

import logging

log = logging.getLogger("decision.proxy.memory_service")


class MemoryService:
    """记忆子系统：store / injector / tool / extractor 的懒加载与编排。"""

    def __init__(self, extract_provider_fn, push_fn, clock=None):
        self._extract_provider_fn = extract_provider_fn  # callable -> provider
        self._push = push_fn
        self._clock = clock
        self._store_inst = None
        self._tool = None
        self._injector = None
        self._extractor = None

    # ---------------------------------------------------------------- store
    def store(self):
        """懒加载 MemoryStore（共享实例，带向量存储）。"""
        if self._store_inst is None:
            from ..memory import MemoryStore, VectorStore
            from ..memory.store import DEFAULT_MEMORY_ROOT, DEFAULT_VECTOR_ROOT
            vs = VectorStore(DEFAULT_VECTOR_ROOT)
            self._store_inst = MemoryStore(
                root=DEFAULT_MEMORY_ROOT, vector_store=vs)
        return self._store_inst

    def tool(self):
        """懒加载 MemoryTool（避免 import 开销，仅首次调用时构造）。"""
        if self._tool is None:
            from ..memory import MemoryTool
            self._tool = MemoryTool(store=self.store())
        return self._tool

    def block(self, session, is_group, history, new_msgs) -> str:
        """决策前自动拼接【记忆】块：L0 全局 + L2 当前会话 + L1 在场人。
        任何异常降级为空串（记忆是增强，不是决策的硬依赖）。"""
        try:
            if self._injector is None:
                from ..memory.injector import MemoryInjector
                self._injector = MemoryInjector(self.store())
            return self._injector.build_memory_block(
                session, is_group, history, new_msgs)
        except Exception:  # noqa: BLE001
            log.exception("memory 注入失败（降级空块）")
        return ""

    # ---------------------------------------------------------------- 提取
    def schedule_extraction(self, session: str, history, new_msgs: list):
        """调度后置记忆提取事件（不阻塞当前决策）。"""
        lines = []
        context = (list(history[-10:]) if history else []) + list(new_msgs)
        for m in context:
            sender = getattr(m, "sender", "?")
            content = (getattr(m, "content", "") or "").replace("\n", " ")[:300]
            if getattr(m, "is_mine", False):
                sender = "我"
            lines.append(f"{sender}: {content}")

        user_names = list(set(
            getattr(m, "sender", "") for m in context
            if getattr(m, "sender", "") and getattr(m, "sender", "") != "我"
            and not getattr(m, "is_mine", False)
        ))

        from .events import EV_MEMORY_EXTRACT
        self._push({
            "type": EV_MEMORY_EXTRACT,
            "session": session,
            "conversation_text": "\n".join(lines),
            "user_names": user_names,
            "ts": self._clock() if self._clock else 0,
        })

    def extractor(self):
        """懒加载 MemoryExtractor（用提取专用/主 provider）。"""
        if self._extractor is None:
            from ..memory import MemoryExtractor
            self._extractor = MemoryExtractor(
                store=self.store(),
                provider=self._extract_provider_fn())
        return self._extractor

    def reset_extractor(self):
        """切换提取 provider 后重置 extractor（下次懒加载用新 provider）。"""
        self._extractor = None

    def extract(self, session: str, conversation_text: str, user_names: list):
        """执行后置记忆提取（由 handler 调用）。同时触发定期整合检查。"""
        extractor = self.extractor()
        if extractor is None:
            return
        try:
            n = extractor.extract_from_conversation(
                session, conversation_text, user_names)
            if n:
                log.info("[%s] 后置记忆提取: %d 条", session, n)
        except Exception:
            log.exception("[%s] 后置记忆提取失败", session)

        try:
            extractor.maybe_consolidate(self._clock() if self._clock else 0)
        except Exception:
            log.exception("记忆整合检查失败")
