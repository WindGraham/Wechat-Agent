# -*- coding: utf-8 -*-
"""decision/memory/vector_store.py — ChromaDB 向量存储，为 memory 提供语义检索。

与 MemoryStore 的 JSON 文件存储并行运行：每条 fact 在写入/更新/删除时
同步到 ChromaDB，search() 优先走向量检索，回退到子串匹配。

设计：
  - 单 collection "memory_facts"，metadata 存 id/scope/key/user/session
  - 默认使用轻量字符 n-gram 嵌入（零依赖，不需要下载模型），
    也支持外部注入 embedding_fn（如 sentence-transformers）
  - 所有操作可降级：ChromaDB 不可用时自动回退到子串匹配
"""

import logging
import threading
import numpy as np
from typing import Callable, Optional

log = logging.getLogger("decision.memory.vector_store")

# 轻量嵌入维度
_DEFAULT_DIM = 384


def _char_ngram_embed(text: str, dim: int = _DEFAULT_DIM) -> list:
    """轻量字符 n-gram 哈希嵌入（零依赖，不需要下载模型）。

    把文本切成 2-4 字 n-gram，哈希到固定维度向量。
    语义上不如 sentence-transformers，但比子串匹配强得多——
    "爬山"和"登山"、"周末去香山"会有一定的重叠。
    """
    text = (text or "").lower()
    vec = np.zeros(dim, dtype=np.float32)
    if not text.strip():
        return vec.tolist()
    # 2-4 gram
    for n in (2, 3, 4):
        for i in range(len(text) - n + 1):
            gram = text[i:i + n]
            h = hash(gram) % dim
            vec[h] += 1.0
    # L2 归一化
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec.tolist()


class LightweightEmbeddingFunction:
    """零依赖嵌入函数（字符 n-gram 哈希），供 ChromaDB 使用。"""

    def __init__(self, dim: int = _DEFAULT_DIM):
        self._dim = dim

    def __call__(self, input: list) -> list:
        return [_char_ngram_embed(t, self._dim) for t in input]

    def name(self) -> str:
        return "lightweight-ngram"


class VectorStore:
    """ChromaDB 向量存储包装，提供 upsert/search/delete。

    使用模式：
        vs = VectorStore(persist_dir="/path/to/vectors")
        vs.upsert(fact_id, content, metadata)
        results = vs.search("query text", n=10)
        vs.delete(fact_id)

    嵌入函数优先级：
      1. 外部注入的 embedding_fn
      2. sentence-transformers（如果有）
      3. 轻量 n-gram 哈希（零依赖兜底）
    """

    def __init__(self, persist_dir: str,
                 embedding_fn: Optional[Callable] = None):
        self._persist_dir = persist_dir
        self._client = None
        self._collection = None
        self._ready = False
        self._lock = threading.Lock()
        self._embedding_fn = embedding_fn  # 外部注入（最高优先）

    @staticmethod
    def _default_embedding_fn():
        """获取默认嵌入函数：sentence-transformers > n-gram。"""
        try:
            from chromadb.utils import embedding_functions
            return embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="paraphrase-multilingual-MiniLM-L12-v2")
        except Exception:
            log.debug("sentence-transformers 不可用，使用轻量 n-gram 嵌入")
            return LightweightEmbeddingFunction()

    # ---------------------------------------------------------------- 初始化
    def _ensure(self) -> bool:
        """懒初始化 ChromaDB（首次使用时才加载，避免 import 开销）。"""
        if self._ready:
            return True
        with self._lock:
            if self._ready:
                return True
            try:
                import chromadb
                self._client = chromadb.PersistentClient(
                    path=self._persist_dir,
                    settings=chromadb.Settings(
                        anonymized_telemetry=False))
                ef = self._embedding_fn or self._default_embedding_fn()
                # 删除已有 collection（防 embedding 函数不匹配）
                try:
                    self._client.delete_collection(name="memory_facts")
                except Exception:
                    pass
                self._collection = self._client.create_collection(
                    name="memory_facts",
                    embedding_function=ef,
                    metadata={"hnsw:space": "cosine"})
                self._ready = True
                log.info("ChromaDB 向量存储就绪: %s (%d 条)",
                         self._persist_dir, self._collection.count())
            except Exception:
                log.warning("ChromaDB 初始化失败，回退到子串匹配",
                            exc_info=True)
                self._ready = False
            return self._ready

    # ---------------------------------------------------------------- 操作
    def upsert(self, fact_id: str, content: str,
               metadata: Optional[dict] = None):
        """插入或更新一条 fact 的向量。"""
        if not self._ensure():
            return
        try:
            meta = dict(metadata or {})
            # ChromaDB metadata 只接受 str/int/float/bool
            meta = {k: (str(v) if not isinstance(v, (int, float, bool))
                         else v)
                    for k, v in meta.items()}
            self._collection.upsert(
                ids=[fact_id],
                documents=[content],
                metadatas=[meta],
            )
        except Exception:
            log.debug("向量 upsert 失败: %s", fact_id, exc_info=True)

    def search(self, query: str, n: int = 10,
               scope_filter: Optional[str] = None) -> list:
        """语义检索，返回 [fact_id, ...] 列表（按相似度降序）。

        scope_filter: 可选，限定 global/user/session 范围。
        ChromaDB 不可用时返回空列表（调用方回退到子串匹配）。
        """
        if not self._ensure():
            return []
        try:
            where = None
            if scope_filter:
                where = {"scope": scope_filter}
            results = self._collection.query(
                query_texts=[query],
                n_results=min(n, 100),
                where=where,
            )
            ids = results.get("ids", [[]])[0]
            return list(ids) if ids else []
        except Exception:
            log.debug("向量检索失败: %s", query[:50], exc_info=True)
            return []

    def delete(self, fact_id: str):
        """删除一条 fact 的向量。"""
        if not self._ensure():
            return
        try:
            self._collection.delete(ids=[fact_id])
        except Exception:
            log.debug("向量删除失败: %s", fact_id, exc_info=True)

    def count(self) -> int:
        """当前向量数。"""
        if not self._ensure():
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0
