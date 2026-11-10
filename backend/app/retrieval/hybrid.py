"""W9-2 混合检索:BM25 + 稠密向量 → RRF 融合 → 重叠度 rerank。

本机形态(与 W8-W12 规划 §2 一致)
--------------------------------
- 稀疏路:``BM25Backend``(rank-bm25 BM25Plus),**开启停用词清洗**
  (``build(..., clean=True)`` / ``search(..., clean=True)``),取 top-20。
- 稠密路:``TfidfSvdVectorBackend``(sklearn TF-IDF + TruncatedSVD),取 top-20。
- 融合:加权 RRF(Reciprocal Rank Fusion,``k=60``)。
- 重排:query 与条款关键词的 Jaccard + clause_id 先验,取 top-5。

融合分公式(定标口径,数字可复算)
--------------------------------
对候选条款 d::

    rrf(d)    = Σ_r  w_r · s_r(d) / (K_RRF + rank_r(d))        # w_r = 1.0, K_RRF = 60
    s_bm25(d) = min(1.0, bm25_raw(d) / BM25_SCALE)              # BM25_SCALE = 40.0
    s_vec(d)  = max(0.0, cosine(d))                             # 余弦已 L2 归一化
    cover(d)  = |Q ∩ D| / |Q|          # Q/D = 清洗后长度≥2 的 token 集合
    fused(d)  = rrf(d) · (COVERAGE_BASE + (1-COVERAGE_BASE) · cover(d))
                                                                 # COVERAGE_BASE = 0.2
    rerank(d) = fused(d) + JACCARD_WEIGHT · jaccard(d) + clause_id_prior(d)
                                                                 # JACCARD_WEIGHT = 0.15

- ``s_bm25`` 把 BM25 原始分压到 [0,1](``BM25_SCALE=40`` 取自 18 条语料上
  覆盖句最高分 47.86 与未覆盖句上界 15.11 之间的量级);``s_vec`` 直接用余弦。
- 加权 RRF 保留"名次共识"的本质,又让"分数很低的单路命中"贡献很小 ——
  纯 1/(k+rank) 在 18 条小语料上无法区分"排名第 1 但只有 4.4 分"与
  "排名第 1 且 47 分"。
- ``cover`` 是相关性闸:未覆盖问句(如"会员积分怎么兑换")可能在向量路拿到
  较高余弦(RP-03 实测 0.7868),但它的关键词集合与任何条款都没有长度≥2 的
  交集,``cover=0`` 时融合分只剩 20%,被阈值挡下。
- ``rerank`` 只做**排序**,不参与 no_hit 判定:``jaccard`` 用 query 关键词
  集合(含别名扩展)与条款关键词集合,权重 0.15 与融合分同量级,
  足以在两条候选接近时按"字面重叠更多"排前;``clause_id_prior`` 仅在
  query 里直接写了条款号时加 0.05。

``FUSION_THRESHOLD`` 语义:**融合分阈值**(作用于 ``fused``),低于它 →
``no_hit=True``。返回的 ``RetrievedChunk.score`` 是 ``rerank`` 分(排序分)。
10 句定标表见 ``backend/tests/test_hybrid.py`` 的 ``CALIBRATION_CASES``
(每句给出 bm25 top1 分、vec top1 余弦、fused,可手算复现)。

重建时机
--------
与 BM25 **同生命周期**:``build`` 全量重建;``get_retriever`` 按
``(id, version, updated_at)`` 签名缓存,数据变化自动失效;
``POST /api/kb/reload`` 显式调用 ``rebuild_index`` 并落 ``kb_vector`` 表。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.kb import KB_STATUS_ACTIVE, KbVector, KnowledgeItem
from app.retrieval.base import RetrievedChunk, SearchResult
from app.retrieval.bm25_backend import BM25Backend, strip_stopwords, tokenize, tokenize_query
from app.retrieval.chunker import PolicyChunk
from app.retrieval.vector_backend import TfidfSvdVectorBackend

K_RRF = 60
"""RRF 名次平滑常数(工业界默认 60)。"""

BM25_ROUTE_TOP_K = 20
VECTOR_ROUTE_TOP_K = 20
"""两条召回路的候选数;融合只在 top-20 内做(与任务书一致)。"""

BM25_SCALE = 40.0
"""BM25 原始分 → [0,1] 强度的量级基准(见模块 docstring 定标说明)。"""

COVERAGE_BASE = 0.2
"""无关键词重叠时的融合分保留比例(0.2 = 最多保留两成)。"""

JACCARD_WEIGHT = 0.15
"""重叠度重排权重;jaccard 量级 0.01~0.11 → 贡献 0.0015~0.017,与融合分
(0.002~0.03)同量级,足以在接近候选间按字面重叠翻转(定标样本:
"偏远地区包邮吗" LP-02/LP-03 融合分差 0.0028,jaccard 差 0.024 → 翻转)。"""

FUSION_THRESHOLD = 0.005
"""融合分阈值(定标见 tests/test_hybrid.py 与模块 docstring):

- 覆盖句(8 句原定标 + "我想退钱怎么弄"/"退差价怎么算")最低融合分 0.008135;
- 未覆盖句(2 句)最高融合分 0.003072;
- 0.005 = sqrt(0.008135 × 0.003072) ≈ 0.004999,取整到 0.005(两侧余量
  分别约 39% / 63%,均可复算)。
"""

FINAL_TOP_K = 5
"""最终返回条数上限。"""

RERANK_TOP_N = 20
"""参与重叠度重排的候选数。"""

CLAUSE_ID_PRIOR = 0.05
"""查询里直接写了条款号(如"RP-01 是什么")时的先验加分。"""


def _keyword_set(text: str, *, expand: bool = False) -> set[str]:
    """关键词集合 = 清洗后 token 中长度 ≥2 的部分(空则退回全部 token)。

    ``expand=True`` 用于查询侧:带上 W7-3 别名扩展("退钱"→"退款"/"金额"),
    否则"我想退钱怎么弄"与条款里的"退款金额"没有共同关键词,覆盖率会误判 0。
    """
    tokens = (
        tokenize_query(strip_stopwords(text))
        if expand
        else tokenize(strip_stopwords(text))
    )
    keys = {t for t in tokens if len(t) >= 2}
    return keys or set(tokens)


@dataclass
class _FusionDetail:
    """单条候选的融合明细(测试/回执用它复算数字)。"""

    clause_id: str
    version: str
    bm25_raw: float = 0.0
    bm25_rank: int = 0
    vector_cos: float = 0.0
    vector_rank: int = 0
    rrf: float = 0.0
    coverage: float = 0.0
    fused: float = 0.0
    jaccard: float = 0.0
    rerank: float = 0.0
    extra: dict = field(default_factory=dict)


class HybridRetriever:
    """BM25 + 向量双路召回、RRF 融合、重叠度重排。

    用法::

        r = HybridRetriever()
        r.build(chunks)                       # 内存重建(不落库)
        result = r.search("我想退钱怎么弄")    # SearchResult

    需要写 ``kb_vector`` 时用 :func:`rebuild_index` / :func:`get_retriever`。
    """

    def __init__(
        self,
        *,
        k_rrf: int = K_RRF,
        threshold: float = FUSION_THRESHOLD,
        final_top_k: int = FINAL_TOP_K,
    ) -> None:
        self._k_rrf = int(k_rrf)
        self._threshold = float(threshold)
        self._final_top_k = int(final_top_k)
        self._bm25 = BM25Backend()
        self._vector = TfidfSvdVectorBackend(clean=True)
        self._chunks: list[PolicyChunk] = []
        self._by_key: dict[tuple[str, str], int] = {}
        self._keyword_sets: list[set[str]] = []
        self.details: list[_FusionDetail] = []
        #: 最近一次 ``persist_vectors`` 实际写入 ``kb_vector`` 的行数
        #: (0 = 本次未落库或没有可写条款);``/api/kb/reload`` 用它报真实写入数。
        self.persisted_rows: int = 0

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self, chunks: list[PolicyChunk]) -> None:
        """全量重建双路索引(空列表清空;停用词清洗在两侧同用)。"""
        self._chunks = list(chunks)
        self._by_key = {
            (c.clause_id, c.version): i for i, c in enumerate(self._chunks)
        }
        self._keyword_sets = [_keyword_set(c.text) for c in self._chunks]
        self._bm25.build(self._chunks, clean=True)
        self._vector.index(self._chunks)

    @property
    def is_ready(self) -> bool:
        return bool(self._chunks)

    @property
    def vector_dim(self) -> int:
        return self._vector.dim

    @property
    def clause_count(self) -> int:
        return len(self._chunks)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        top_k: int = FINAL_TOP_K,
        threshold: float | None = None,
    ) -> SearchResult:
        """混合检索。``threshold`` 覆盖默认融合分阈值(``None`` = 用实例默认)。"""
        self.details = []
        if not self._chunks or not query.strip():
            return SearchResult(results=[], no_hit=True)

        thr = self._threshold if threshold is None else float(threshold)
        q_set = _keyword_set(query, expand=True)

        bm25_res = self._bm25.search(
            query, top_k=BM25_ROUTE_TOP_K, threshold=0.0, clean=True
        )
        vec_res = self._vector.search(
            query, top_k=VECTOR_ROUTE_TOP_K, threshold=0.0
        )

        # ---- RRF 融合(按 (clause_id, version) 聚合) ----
        fused: dict[tuple[str, str], float] = {}
        detail: dict[tuple[str, str], _FusionDetail] = {}

        def _slot(clause_id: str, version: str) -> _FusionDetail:
            key = (clause_id, version)
            if key not in detail:
                detail[key] = _FusionDetail(clause_id=clause_id, version=version)
            return detail[key]

        for rank, item in enumerate(bm25_res.results, start=1):
            slot = _slot(item.clause_id, item.version)
            strength = min(1.0, max(0.0, item.score) / BM25_SCALE)
            slot.bm25_raw = float(item.score)
            slot.bm25_rank = rank
            fused[(item.clause_id, item.version)] = (
                fused.get((item.clause_id, item.version), 0.0)
                + strength / (self._k_rrf + rank)
            )

        for rank, item in enumerate(vec_res.results, start=1):
            slot = _slot(item.clause_id, item.version)
            strength = max(0.0, float(item.score))
            slot.vector_cos = float(item.score)
            slot.vector_rank = rank
            fused[(item.clause_id, item.version)] = (
                fused.get((item.clause_id, item.version), 0.0)
                + strength / (self._k_rrf + rank)
            )

        if not fused:
            return SearchResult(results=[], no_hit=True)

        # ---- 覆盖率乘子 + 阈值判定 ----
        max_fused = 0.0
        for key, rrf_score in fused.items():
            idx = self._by_key.get(key)
            doc_keys = self._keyword_sets[idx] if idx is not None else set()
            inter = len(q_set & doc_keys)
            union = len(q_set | doc_keys)
            cover = (inter / len(q_set)) if q_set else 0.0
            slot = detail[key]
            slot.rrf = float(rrf_score)
            slot.coverage = cover
            slot.fused = float(
                rrf_score * (COVERAGE_BASE + (1.0 - COVERAGE_BASE) * cover)
            )
            slot.jaccard = (inter / union) if union else 0.0
            slot.rerank = slot.fused + JACCARD_WEIGHT * slot.jaccard + (
                CLAUSE_ID_PRIOR if key[0] and key[0] in query else 0.0
            )
            max_fused = max(max_fused, slot.fused)

        if max_fused < thr:
            self.details = sorted(
                detail.values(), key=lambda d: d.fused, reverse=True
            )
            return SearchResult(results=[], no_hit=True)

        # ---- 重叠度 rerank → top-k(先按融合分取前 N,再做 jaccard 重排) ----
        candidates = sorted(
            (d for d in detail.values() if d.fused >= thr),
            key=lambda d: d.fused,
            reverse=True,
        )[: max(0, int(RERANK_TOP_N))]
        ranked = sorted(
            candidates,
            key=lambda d: (d.rerank, d.fused),
            reverse=True,
        )[: max(0, int(top_k))]

        results: list[RetrievedChunk] = []
        for slot in ranked:
            idx = self._by_key.get((slot.clause_id, slot.version))
            if idx is None:
                continue
            chunk = self._chunks[idx]
            results.append(
                RetrievedChunk(
                    clause_id=chunk.clause_id,
                    version=chunk.version,
                    text=chunk.text,
                    score=slot.rerank,
                )
            )
        self.details = ranked
        return SearchResult(results=results, no_hit=len(results) == 0)

    # ------------------------------------------------------------------
    # 持久化(写 kb_vector)
    # ------------------------------------------------------------------
    def persist_vectors(
        self, session: Session, merchant_id: str, rows: list[KnowledgeItem]
    ) -> int:
        """把当前向量按条款 upsert 进 ``kb_vector``;返回写入行数。"""
        item_ids = (
            session.execute(
                select(KnowledgeItem.id).where(
                    KnowledgeItem.merchant_id == merchant_id
                )
            )
            .scalars()
            .all()
        )
        if item_ids:
            session.execute(
                delete(KbVector).where(KbVector.knowledge_item_id.in_(item_ids))
            )

        id_by_key = {
            (row.clause_id or "", row.version): row.id for row in rows
        }
        written = 0
        for clause_id, version, blob in self._vector.export_vectors():
            item_pk = id_by_key.get((clause_id, version))
            if item_pk is None:
                continue
            session.add(
                KbVector(
                    knowledge_item_id=item_pk,
                    vec=blob,
                    model=self._vector.model_name,
                    dim=self._vector.dim,
                )
            )
            written += 1
        session.flush()
        self.persisted_rows = written
        return written


# ---------------------------------------------------------------------------
# 进程内缓存 + 从 DB 重建(检索链路与 /api/kb/reload 共用)
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[tuple, HybridRetriever]] = {}


def _active_rows(session: Session, merchant_id: str) -> list[KnowledgeItem]:
    """检索只取 active 条款;draft 不生效、retired 不可见。"""
    return list(
        session.execute(
            select(KnowledgeItem)
            .where(
                KnowledgeItem.merchant_id == merchant_id,
                KnowledgeItem.deleted_at.is_(None),
                KnowledgeItem.clause_id.is_not(None),
                KnowledgeItem.status == KB_STATUS_ACTIVE,
            )
            .order_by(KnowledgeItem.clause_id, KnowledgeItem.version)
        )
        .scalars()
        .all()
    )


def _signature(rows: list[KnowledgeItem]) -> tuple:
    return tuple(
        (
            row.id,
            row.version,
            row.updated_at.isoformat() if row.updated_at else "",
        )
        for row in rows
    )


def _chunks_of(rows: list[KnowledgeItem]) -> list[PolicyChunk]:
    return [
        PolicyChunk(
            clause_id=row.clause_id or "",
            version=row.version,
            title=row.title or "",
            text=row.content,
            effective_date="",
        )
        for row in rows
    ]


def rebuild_index(
    session: Session, merchant_id: str, *, persist: bool = True
) -> HybridRetriever:
    """从 DB 全量重建混合索引(热更新入口);``persist`` 时写 kb_vector。"""
    rows = _active_rows(session, merchant_id)
    retriever = HybridRetriever()
    retriever.build(_chunks_of(rows))
    if persist:
        retriever.persist_vectors(session, merchant_id, rows)
    _CACHE[merchant_id] = (_signature(rows), retriever)
    return retriever


def get_retriever(session: Session, merchant_id: str) -> HybridRetriever:
    """取(必要时重建)该店铺的混合检索器;按数据签名自动失效。"""
    rows = _active_rows(session, merchant_id)
    signature = _signature(rows)
    cached = _CACHE.get(merchant_id)
    if cached is not None and cached[0] == signature:
        return cached[1]
    retriever = HybridRetriever()
    retriever.build(_chunks_of(rows))
    _CACHE[merchant_id] = (signature, retriever)
    return retriever


def invalidate_cache(merchant_id: str | None = None) -> None:
    """清缓存(``None`` 清全部);测试与 reload 用。"""
    if merchant_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(merchant_id, None)


__all__ = [
    "BM25_ROUTE_TOP_K",
    "BM25_SCALE",
    "CLAUSE_ID_PRIOR",
    "COVERAGE_BASE",
    "FINAL_TOP_K",
    "FUSION_THRESHOLD",
    "HybridRetriever",
    "JACCARD_WEIGHT",
    "K_RRF",
    "RERANK_TOP_N",
    "VECTOR_ROUTE_TOP_K",
    "get_retriever",
    "invalidate_cache",
    "rebuild_index",
]
