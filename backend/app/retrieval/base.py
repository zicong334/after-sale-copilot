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
    """向量检索后端抽象接口 —— 声明的扩展点,不是假实现。

    W2-A 不实现向量检索(D-02 禁 faiss/chromadb)。下面两个方法是将来
    FAISS / chromadb 后端的契约,当前项目里没有实现类,调用即抛
    ``NotImplementedError``。这是任务书明确允许的"扩展点",回执亦单独标注。

    契约(与 BM25Backend 对齐,保证未来可无缝替换):
    - ``index(chunks)``:把分块写入向量索引,并落 ``embedding_model`` / ``dim``。
    - ``search(query, top_k, threshold)``:向量检索,返回 ``SearchResult``。
    """

    @abstractmethod
    def index(self, chunks: list) -> None:
        """构建向量索引。扩展点,未实现。"""
        raise NotImplementedError(
            "VectorBackend 是扩展点:W2-A 只做 BM25,未实现向量检索;请用 BM25Backend"
        )

    @abstractmethod
    def search(
        self, query: str, top_k: int = 5, threshold: float = 0.0
    ) -> SearchResult:
        """向量检索。扩展点,未实现。"""
        raise NotImplementedError(
            "VectorBackend 是扩展点:W2-A 只做 BM25,未实现向量检索;请用 BM25Backend"
        )
