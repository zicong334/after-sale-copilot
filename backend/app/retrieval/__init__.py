"""检索层:政策语料分块(chunker)+ BM25 检索(bm25_backend)+ 向量扩展点(base)。

W2-A 只做 BM25;向量检索留接口(VectorBackend),不实现(D-02)。
"""
from __future__ import annotations

from app.retrieval.base import RetrievedChunk, SearchResult, VectorBackend
from app.retrieval.bm25_backend import BM25Backend, tokenize
from app.retrieval.chunker import PolicyChunk, PolicyChunker

__all__ = [
    "RetrievedChunk",
    "SearchResult",
    "VectorBackend",
    "PolicyChunk",
    "PolicyChunker",
    "BM25Backend",
    "tokenize",
]
