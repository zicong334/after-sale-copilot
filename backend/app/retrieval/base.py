"""检索层公共数据结构 + 向量检索后端抽象接口。

D-02 决策:W2-A 只做 BM25 + 条款号引用;向量检索(FAISS/chromadb)留扩展点,
不实现。``knowledge_item.embedding_model / dim`` 两列即向量落点预留。

本模块只含:
1. ``RetrievedChunk`` —— 检索命中块(带 clause_id + version,上层引用用)。
2. ``SearchResult`` —— 检索结果(results + no_hit 元信息)。
3. ``VectorBackend`` —— 向量后端抽象接口(声明式扩展点,见类 docstring)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetrievedChunk:
    """一条检索命中的条款块(任务书要求:永远带 clause_id + version)。"""

    clause_id: str
    version: str
    text: str
    score: float


@dataclass
class SearchResult:
    """检索结果。

    ``results`` 为空且 ``no_hit=True`` 表示"政策未覆盖":上层据此明说
    "政策未覆盖",禁止硬凑、禁止编造条款号。
    """

    results: list[RetrievedChunk] = field(default_factory=list)
    no_hit: bool = False


class VectorBackend(ABC):
    """向量检索后端抽象接口。

    W9-1 起由 :class:`app.retrieval.vector_backend.TfidfSvdVectorBackend`
    实现(本机形态:sklearn TF-IDF + TruncatedSVD,无需 faiss/chromadb)。
    本 ABC 保留,作为生产缝:pgvector / Milvus 后端实现同一契约即可替换,
    上层 ``HybridRetriever`` 无需改动(见 W9 任务书 §2 与
    ``vector_backend.py`` 模块 docstring)。

    W2-A 的"扩展点"语义已被真实实现取代:下面两个方法不再自带
    ``NotImplementedError`` 桩体,由具体子类强制实现。

    契约(与 BM25Backend 对齐):
    - ``index(chunks)``:把分块写入向量索引,并记录 ``model`` / ``dim``。
    - ``search(query, top_k, threshold)``:向量检索,返回 ``SearchResult``。
    """

    @abstractmethod
    def index(self, chunks: list) -> None:
        """构建向量索引(全量重建;空列表 = 清空)。"""
        ...

    @abstractmethod
    def search(
        self, query: str, top_k: int = 5, threshold: float = 0.0
    ) -> SearchResult:
        """向量检索,按相似度降序返回 ``SearchResult``。"""
        ...
