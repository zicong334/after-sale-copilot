"""W9-1 向量后端测试(sklearn TF-IDF + TruncatedSVD + L2 归一化)。

覆盖任务书 W9-1 验收点:
- ``TfidfSvdVectorBackend`` 真正 fulfill ``VectorBackend`` 抽象契约(不再是扩展点桩);
- 拟合语料 = 传入条款(W9 起由调用方只传 active 条款);
- 输出维度 = ``min(dim=64, n_samples-1, n_features-1)``(18 条 → 17),向量 L2 归一;
- 余弦 top-k + 阈值语义;``random_state`` 固定 → 可复算;
- 停用词在向量化前清洗(与 BM25 路同用,见 ``bm25_backend.strip_stopwords``)。
"""
from __future__ import annotations

import inspect
import math
from pathlib import Path

import numpy as np
import pytest

from app.retrieval.base import VectorBackend
from app.retrieval.bm25_backend import STOPWORDS, strip_stopwords
from app.retrieval.chunker import PolicyChunk, PolicyChunker
from app.retrieval.vector_backend import (
    RANDOM_STATE,
    VECTOR_DIM,
    VECTOR_MODEL_NAME,
    TfidfSvdVectorBackend,
    decode_vector,
)

ROOT = Path(__file__).resolve().parents[2]
POLICIES_DIR = ROOT / "data" / "policies"


def _parse_all() -> list[PolicyChunk]:
    chunker = PolicyChunker()
    chunks: list[PolicyChunk] = []
    for path in sorted(POLICIES_DIR.glob("*.md")):
        chunks.extend(chunker.parse(path.read_text(encoding="utf-8")))
    return chunks


def _build(chunks: list[PolicyChunk] | None = None) -> TfidfSvdVectorBackend:
    backend = TfidfSvdVectorBackend()
    backend.index(_parse_all() if chunks is None else chunks)
    return backend


# ---------------------------------------------------------------------------
# 1) 契约:真正实现,不再是 NotImplementedError 桩
# ---------------------------------------------------------------------------
def test_vector_backend_implements_abstract_contract():
    assert issubclass(TfidfSvdVectorBackend, VectorBackend)
    assert not inspect.isabstract(TfidfSvdVectorBackend), "实现类必须可实例化"
    assert {"index", "search"} <= VectorBackend.__abstractmethods__

    backend = TfidfSvdVectorBackend()
    backend.index([])  # 不再抛 NotImplementedError
    assert backend.search("退款", top_k=3).no_hit is True


# ---------------------------------------------------------------------------
# 2) 维度与 L2 归一化(18 条 → dim=17)
# ---------------------------------------------------------------------------
def test_index_reports_reduced_dim_and_unit_norm_vectors():
    backend = _build()
    chunks = _parse_all()
    assert backend.dim == min(VECTOR_DIM, len(chunks) - 1) == 17, (
        f"18 条语料应降到 min(64,17) 维,实际 {backend.dim}"
    )
    rows = backend.export_vectors()
    assert len(rows) == len(chunks) == 18
    for clause_id, version, blob in rows:
        assert clause_id and version == "v1"
        assert len(blob) == backend.dim * 4, "float32 × dim 字节"
        vec = decode_vector(blob)
        assert vec.shape == (backend.dim,)
        assert math.isclose(float(np.linalg.norm(vec)), 1.0, abs_tol=1e-5), (
            f"{clause_id} 向量应 L2 归一"
        )


def test_export_vectors_shape_and_roundtrip():
    backend = _build()
    first = backend.export_vectors()[0]
    vec = decode_vector(first[2])
    assert vec.dtype == np.float32
    assert float(vec.dot(vec)) == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# 3) 余弦检索:已知局限句在向量路也能召回 RP-07
# ---------------------------------------------------------------------------
def test_search_hits_known_limitation_query():
    backend = _build()
    result = backend.search("我想退钱怎么弄", top_k=3)
    assert not result.no_hit
    assert result.results[0].clause_id == "RP-07", "退款金额计算条款"
    assert 0.0 < result.results[0].score <= 1.0
    scores = [r.score for r in result.results]
    assert scores == sorted(scores, reverse=True), "结果按余弦降序"


def test_search_threshold_and_empty_inputs():
    backend = _build()
    assert backend.search("会员积分怎么兑换", threshold=0.99).no_hit is True
    assert backend.search("", top_k=3).no_hit is True
    assert backend.search("   ").no_hit is True
    empty = TfidfSvdVectorBackend()
    empty.index([])
    assert empty.dim == 0 and empty.export_vectors() == []
    assert empty.search("退款").no_hit is True


# ---------------------------------------------------------------------------
# 4) random_state 固定 → 两次拟合字节一致(可复算)
# ---------------------------------------------------------------------------
def test_vectors_are_deterministic_for_fixed_random_state():
    a = _build()
    b = _build()
    assert RANDOM_STATE == 42
    assert a.export_vectors() == b.export_vectors(), "同语料同 random_state 必须可复算"
    assert a.model_name == b.model_name == VECTOR_MODEL_NAME


# ---------------------------------------------------------------------------
# 5) 停用词在向量化前清洗(两侧同用;白盒断言)
# ---------------------------------------------------------------------------
def test_stopwords_removed_before_vectorizing():
    backend = TfidfSvdVectorBackend(clean=True)
    prepared = backend._prepare("我想退钱怎么弄")  # noqa: SLF001 - 白盒断言
    assert "怎么" not in prepared and "我" not in prepared and "弄" not in prepared
    assert strip_stopwords("我想退钱怎么弄") == "退钱"
    assert len(STOPWORDS) >= 30, "任务书要求 ≥30 个电商停用词"

    raw = TfidfSvdVectorBackend(clean=False)
    assert raw._prepare("我想退钱怎么弄") == "我想退钱怎么弄"  # noqa: SLF001


# ---------------------------------------------------------------------------
# 6) 生产缝注释:ABC 保留给 pgvector / Milvus
# ---------------------------------------------------------------------------
def test_model_name_documents_local_impl_and_production_seam():
    assert VECTOR_MODEL_NAME.startswith("tfidf-svd/")
    assert VECTOR_DIM == 64
    doc = TfidfSvdVectorBackend.__doc__ or ""
    assert "pgvector" in doc or "Milvus" in doc, "docstring 需标明生产缝"
