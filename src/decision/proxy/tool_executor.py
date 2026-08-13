# -*- coding: utf-8 -*-
"""proxy/tool_executor.py — ToolExecutor：决策层内联工具的分发与执行。

从 Proxy 抽出的"工具执行"职责：
  - memory / websearch / emoji / moments 四种工具的分发
  - emoji 检索（模糊+词义匹配）
  - websearch 分流（本地段同步 + 网络段异步回灌）
  - moments 发圈（存文案 → 写 memory → submit_bundle 投交互层）

依赖全部构造注入（journal_fn / clip_fn / push_fn 等），不依赖 Proxy 实例。
"""

import logging
import os
import threading
import time

from ...shared.xml_blocks import parse_attrs

log = logging.getLogger("decision.proxy.tool_executor")


class ToolExecutor:
    """执行 <tool> 块（decision 层内联工具）。"""

    def __init__(self, memory_svc, reader, submit_bundle, push_fn, clock,
                 journal_fn, clip_fn, emoji_index=None):
        self._memory_svc = memory_svc          # MemoryService（tool + store）
        self._reader = reader                  # is_group / 本地段检索
        self._submit_bundle = submit_bundle    # moments 动作出口
        self._push = push_fn                   # 事件推送（websearch 异步回灌）
        self._clock = clock
        self._journal = journal_fn             # callable(event_type, **data)
        self._clip = clip_fn                   # callable(text, limit) -> str
        self._emoji_idx = emoji_index          # 可注入测试实例，None 懒加载
        self._websearch = None                 # 懒加载 WebSearchTool

    # ---------------------------------------------------------------- 分发
    def exec(self, block, current_session: str = "") -> str:
        """执行 <tool> 块（决策层内联工具分发）。

        当前工具：memory / websearch / emoji / moments。
        current_session: 当前决策会话（memory scope 缺省时按此推断）。
        """
        attrs = parse_attrs(block.attrs)
        name = attrs.get("name", "")
        op = attrs.get("op", attrs.get("query", ""))
        t0 = time.time()
        result = ""
        if name == "memory":
            is_group = None
            try:
                if current_session:
                    is_group = self._reader.last_is_group(current_session)
            except Exception:  # noqa: BLE001
                is_group = None
            result = self._memory_svc.tool().run(
                attrs, current_session=current_session, is_group=is_group)
        elif name == "websearch":
            result = self.exec_websearch(attrs, current_session)
        elif name == "emoji":
            result = self.exec_emoji_search(attrs)
        elif name == "moments":
            result = self.exec_moments_tool(attrs)
        else:
            result = f"未知工具: {name}"
        self._journal("tool_call", session=current_session,
                      tool=name, op=op[:60],
                      attrs={k: str(v)[:120] for k, v in attrs.items()},
                      result=self._clip(str(result)),
                      elapsed_ms=int((time.time() - t0) * 1000))
        return result

    # ---------------------------------------------------------------- emoji
    def emoji_index(self):
        """懒加载表情索引（shared 层，与交互层发送共用同一数据源）。"""
        if self._emoji_idx is None:
            from ...shared.emoji_index import EmojiIndex
            self._emoji_idx = EmojiIndex()
        return self._emoji_idx

    def exec_emoji_search(self, attrs: dict) -> str:
        """emoji 工具：检索表情库候选，同步返回候选文本（LLM 按 seq 精确发）。"""
        query = (attrs.get("query") or "").strip()
        if not query:
            return "emoji 缺 query 属性"
        try:
            from ...shared.emoji_index import format_candidates
            index = self.emoji_index()
            emojis = index.search_semantic(query, exclude_real=True, limit=10)
            return format_candidates(emojis, query)
        except Exception as e:  # noqa: BLE001
            log.exception("emoji 检索失败: %s", query)
            return f"emoji 检索失败: {type(e).__name__}: {e}"

    # ---------------------------------------------------------------- websearch
    def websearch_tool(self):
        """懒加载 WebSearchTool（注入 memory_store/reader 供本地段）。"""
        if self._websearch is None:
            from ..memory import MemoryStore
            from ..search import SearchService, WebSearchTool
            self._websearch = WebSearchTool(
                search_service=SearchService(memory_store=MemoryStore(),
                                             reader=self._reader))
        return self._websearch

    def exec_websearch(self, attrs: dict, session: str = "") -> str:
        """websearch 分流：local 段同步回灌；web 段起子线程异步。"""
        query = attrs.get("query", "")
        if not query:
            return "websearch 缺 query 属性"
        scope = attrs.get("scope", "all")
        tool = self.websearch_tool()

        local_text = ""
        if scope in ("local", "all"):
            local_text = tool.run_local(attrs, session=session)

        if scope in ("web", "all"):
            from .events import EV_SEARCH_DONE

            def _work():
                try:
                    results = tool.run_web(attrs)
                    self._push({
                        "type": EV_SEARCH_DONE, "session": session,
                        "query": query, "results": results,
                        "ts": self._clock()})
                except Exception as e:  # noqa: BLE001
                    log.warning("websearch '%s' 失败: %s", query, e)
                    self._push({
                        "type": EV_SEARCH_DONE, "session": session,
                        "query": query, "error": f"{type(e).__name__}: {e}",
                        "ts": self._clock()})
            threading.Thread(target=_work, daemon=True,
                             name=f"websearch-{query[:10]}").start()
            tail = "\n[网络搜索进行中，结果回来后会通知你]"
        else:
            tail = ""

        return (local_text + tail).strip() or "（无本地记录）"

    # ---------------------------------------------------------------- moments
    def exec_moments_tool(self, attrs: dict) -> str:
        """moments 工具：发纯文字朋友圈（走契约通道，不直接操作设备）。

        三步：存文案文件 → 写 memory → submit_bundle 投 <moments-post>
        进交互层统一队列（实际发布由 BundleSender 持屏幕锁执行）。
        """
        text = (attrs.get("text") or "").strip()
        if not text:
            return "moments 缺 text 属性"

        # 1. 保存文案文件（先存：即使发圈失败也不丢文案）
        from ..memory.store import DEFAULT_MEMORY_ROOT
        diary_dir = os.path.join(DEFAULT_MEMORY_ROOT, "cat_diary")
        os.makedirs(diary_dir, exist_ok=True)
        date_str = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
        diary_path = os.path.join(diary_dir, f"{date_str}.md")
        with open(diary_path, "a", encoding="utf-8") as f:
            f.write(f"\n---\n{text}\n")
        log.info("[moments] 文案已保存: %s", diary_path)

        # 2. 写 memory（先于发圈：即使发圈失败也不丢日记）
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(self._clock()))
        try:
            self._memory_svc.store().add(
                content=f"[{ts_str}] 猫娘日记：{text}",
                key="猫娘日记", scope="global",
                source="cat_diary", confidence=1.0)
        except Exception:
            log.debug("日记 memory 写入失败", exc_info=True)

        # 3. 投递交互层（契约通道：XML bundle 进统一队列，BundleSender 执行）
        esc = (text.replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;").replace('"', "&quot;"))
        xml = f'<moments-post text="{esc}"/>'
        result = self._submit_bundle("__moments__", xml)
        if result.ok:
            log.info("[moments] 朋友圈发布已投递")
            return "朋友圈发布已投递（实际发布由交互层执行）"
        return f"朋友圈发布投递失败: {result.error}"
