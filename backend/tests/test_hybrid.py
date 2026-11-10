"""W9-2 混合检索测试:双路召回 + RRF 融合 + 停用词 + 重叠度重排 + 定标。

覆盖任务书 W9-2 验收点:
- BM25 top-20 与向量 top-20 双路召回,RRF(k=60) 融合,重叠度 rerank 取 top-5;
- 停用词查询侧/索引侧同用(BM25 默认行为不变,零回归);
- 融合分阈值 0.005 的定标依据可复算(sqrt(覆盖句下界 × 未覆盖上界));
- ≥10 句定标表,含已知局限句"我想退钱怎么弄"与"退差价怎么算";
- 只检索 active 条款;``rebuild_index`` 落 ``kb_vector``;改版本后热更新可见。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import KbVector, KnowledgeItem, Merchant
from app.retrieval.bm25_backend import (
    BM25Backend,
    DEFAULT_THRESHOLD,
    STOPWORDS,
    strip_stopwords,
    tokenize,
)
from app.retrieval.chunker import PolicyChunk, PolicyChunker
from app.retrieval.hybrid import (
    BM25_ROUTE_TOP_K,
    FUSION_THRESHOLD,
    JACCARD_WEIGHT,
    K_RRF,
    VECTOR_ROUTE_TOP_K,
    HybridRetriever,
    get_retriever,
    invalidate_cache,
    rebuild_index,
)

ROOT = Path(__file__).resolve().parents[2]
POLICIES_DIR = ROOT / "data" / "policies"
MERCHANT = "m-hybrid"


def _parse_all() -> list[PolicyChunk]:
    chunker = PolicyChunker()
    chunks: list[PolicyChunk] = []
    for path in sorted(POLICIES_DIR.glob("*.md")):
        chunks.extend(chunker.parse(path.read_text(encoding="utf-8")))
    return chunks


def _build(chunks: list[PolicyChunk] | None = None) -> HybridRetriever:
    retriever = HybridRetriever()
    retriever.build(_parse_all() if chunks is None else chunks)
    return retriever


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


def _ensure_merchant(session) -> None:
    if session.get(Merchant, MERCHANT) is None:
        session.add(Merchant(id=MERCHANT, name="混合检索测试店"))
        session.commit()


def _add_item(
    session,
    clause_id: str,
    content: str,
    *,
    status: str = "active",
    version: str = "1",
    title: str | None = None,
) -> KnowledgeItem:
    _ensure_merchant(session)
    row = KnowledgeItem(
        merchant_id=MERCHANT,
        clause_id=clause_id,
        title=title or clause_id,
        content=content,
        status=status,
        version=version,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# 1) 常量契约 + 双路就绪
# ---------------------------------------------------------------------------
def test_route_constants_and_build_state():
    assert K_RRF == 60
    assert BM25_ROUTE_TOP_K == 20 and VECTOR_ROUTE_TOP_K == 20
    assert JACCARD_WEIGHT > 0
    retriever = _build()
    assert retriever.is_ready
    assert retriever.clause_count == 18
    assert retriever.vector_dim == 17
    assert HybridRetriever().search("退款").no_hit is True, "空索引应 no_hit"


# ---------------------------------------------------------------------------
# 2) 10 句定标表(8 原句 + 2 已知局限句)
# ---------------------------------------------------------------------------
CALIBRATION: dict[str, str | None] = {
    "7天无理由能退吗": "RP-01",
    "衣服尺码不合适能换货吗": "RP-05",
    "质量问题退货运费谁出": "RP-02",
    "退款多久到账": "RP-06",
    "偏远地区包邮吗": "LP-02",
    "物流破损怎么处理": "LP-04",
    # W9 已知局限句(原 BM25 下 no_hit / 弱命中)
    "我想退钱怎么弄": "RP-07",
    "退差价怎么算": "RP-07",
    # 政策未覆盖:必须 no_hit(不硬凑)
    "会员积分怎么兑换": None,
    "店铺有没有会员折扣活动": None,
}


def test_calibration_table_expectations():
    retriever = _build()
    for query, want in CALIBRATION.items():
        result = retriever.search(query, top_k=3)
        if want is None:
            assert result.no_hit, f"{query!r} 应 no_hit,实际 {result.results[:1]}"
            continue
        assert not result.no_hit, f"{query!r} 不应 no_hit"
        assert result.results[0].clause_id == want, (
            f"{query!r} top1 应为 {want},实际 {result.results[0].clause_id}"
        )


def test_known_limitation_queries_are_fixed():
    """两条已知局限句:修复前 BM25 分数 4.4157(no_hit)/26.4139,修复后均命中 RP-07。"""
    retriever = _build()
    bm25 = BM25Backend()
    bm25.build(_parse_all())

    before = bm25.search("我想退钱怎么弄", threshold=0.0).results
    assert before[0].clause_id == "RP-07" and before[0].score < DEFAULT_THRESHOLD
    assert bm25.search("我想退钱怎么弄").no_hit is True, "修复前应为 no_hit"

    fixed = retriever.search("我想退钱怎么弄")
    assert not fixed.no_hit and fixed.results[0].clause_id == "RP-07"
    assert retriever.search("退差价怎么算").results[0].clause_id == "RP-07"


# ---------------------------------------------------------------------------
# 3) 融合分可复算 + 阈值定标依据
# ---------------------------------------------------------------------------
def test_fused_score_recomputable_from_details():
    retriever = _build()
    result = retriever.search("我想退钱怎么弄", top_k=3)
    assert result.results
    top = retriever.details[0]
    s_bm25 = min(1.0, max(0.0, top.bm25_raw) / 40.0)
    s_vec = max(0.0, top.vector_cos)
    rrf = 0.0
    if top.bm25_rank:
        rrf += s_bm25 / (K_RRF + top.bm25_rank)
    if top.vector_rank:
        rrf += s_vec / (K_RRF + top.vector_rank)
    expected_fused = rrf * (0.2 + 0.8 * top.coverage)
    expected_rerank = expected_fused + JACCARD_WEIGHT * top.jaccard
    assert top.rrf == pytest.approx(rrf, abs=1e-12)
    assert top.fused == pytest.approx(expected_fused, abs=1e-12)
    assert top.rerank == pytest.approx(expected_rerank, abs=1e-12)
    assert result.results[0].score == pytest.approx(top.rerank, abs=1e-12)


def test_fusion_threshold_is_geometric_midpoint():
    """0.005 = sqrt(0.008135 × 0.003070):覆盖下界与未覆盖上界的几何中点。"""
    covered_floor, uncovered_ceil = 0.008135, 0.003070
    midpoint = math.sqrt(covered_floor * uncovered_ceil)
    assert FUSION_THRESHOLD == pytest.approx(midpoint, abs=1e-4)
    assert covered_floor / FUSION_THRESHOLD > 1.3, "覆盖侧余量约 39%"
    assert FUSION_THRESHOLD / uncovered_ceil > 1.3, "未覆盖侧余量约 63%"


# ---------------------------------------------------------------------------
# 4) RRF vs 单路 BM25 召回对比
# ---------------------------------------------------------------------------
def test_hybrid_recall_is_superset_of_bm25_single_route():
    retriever = _build()
    bm25 = BM25Backend()
    bm25.build(_parse_all())
    bm25_hits = {q for q in CALIBRATION if not bm25.search(q).no_hit}
    hybrid_hits = {q for q in CALIBRATION if not retriever.search(q).no_hit}
    assert bm25_hits <= hybrid_hits, "混合不应丢掉单路 BM25 的召回"
    assert hybrid_hits - bm25_hits == {"我想退钱怎么弄"}, (
        f"混合应额外召回已知局限句,实际差值 {hybrid_hits - bm25_hits}"
    )
    assert len(hybrid_hits) == 8 and len(bm25_hits) == 7


# ---------------------------------------------------------------------------
# 5) 停用词:两侧同用,但 BM25 默认行为零回归
# ---------------------------------------------------------------------------
def test_stopwords_used_by_hybrid_but_bm25_default_unchanged():
    chunks = _parse_all()
    backend = BM25Backend()
    backend.build(chunks)  # clean=False
    assert backend._tokenized == [tokenize(c.text) for c in chunks]  # noqa: SLF001

    # 官方条款正文零口语停用词(实测 grep 0 命中):索引侧清洗对真实语料是恒等变换,
    # 真正起作用的是查询侧清洗。用构造文本证明两侧确实共用同一清洗函数。
    probe = "我想退钱怎么弄"
    assert strip_stopwords(probe) == "退钱"
    assert tokenize(probe) != tokenize(strip_stopwords(probe))
    mixed = [PolicyChunk(
        clause_id="X-1", version="v1", title="", text=probe, effective_date=""
    )]
    raw, cleaned = BM25Backend(), BM25Backend()
    raw.build(mixed)
    cleaned.build(mixed, clean=True)
    assert raw._tokenized != cleaned._tokenized  # noqa: SLF001

    # 查询侧:默认口径仍 no_hit(W2-A 行为不变),clean=True 时词项过滤才命中
    assert backend.search(probe).no_hit is True
    scored = backend.search(probe, threshold=0.0, clean=True)
    assert scored.results and scored.results[0].clause_id == "RP-07"
    assert len(STOPWORDS) >= 30


# ---------------------------------------------------------------------------
# 6) DB 侧:只索引 active、落 kb_vector、版本热更新、缓存失效
# ---------------------------------------------------------------------------
def test_only_active_items_are_indexed(session):
    _add_item(session, "RP-01", "七天无理由退货,商品不影响二次销售。")
    _add_item(session, "RP-02", "质量问题退货运费由商家承担。", status="draft")
    _add_item(session, "RP-03", "退款金额按实付计算。", status="retired")

    retriever = get_retriever(session, MERCHANT)
    assert retriever.clause_count == 1, "draft/retired 不参与索引"
    assert retriever.search("无理由").results[0].clause_id == "RP-01"
    assert retriever.search("运费").no_hit is True


def test_rebuild_index_persists_kb_vector(session):
    _add_item(session, "RP-01", "七天无理由退货,商品不影响二次销售。")
    _add_item(session, "RP-02", "质量问题退货运费由商家承担。")

    retriever = rebuild_index(session, MERCHANT, persist=True)
    session.commit()
    rows = session.execute(select(KbVector)).scalars().all()
    assert len(rows) == 2
    assert {row.dim for row in rows} == {retriever.vector_dim}
    assert {row.model for row in rows} == {"tfidf-svd/char_wb2-3+word1-2/v1"}
    sizes = [len(row.vec) for row in rows]
    assert set(sizes) == {retriever.vector_dim * 4}

    # 再 reload 一次:按 merchant 先清后写,不累积重复行
    rebuild_index(session, MERCHANT, persist=True)
    session.commit()
    assert len(session.execute(select(KbVector)).scalars().all()) == 2


def test_hot_update_sees_new_version_and_hides_retired(session):
    row = _add_item(session, "RP-06", "退款 48 小时内原路退回。", version="1")
    _add_item(session, "RP-07", "退款金额按实付计算。")

    first = rebuild_index(session, MERCHANT)
    hit = first.search("原路退回")
    assert hit.results[0].clause_id == "RP-06" and hit.results[0].version == "1"

    # 模拟 PATCH:改正文 + version 自增;RP-07 退役
    row.content = "退款 24 小时内原路退回(新版本)。"
    row.version = "2"
    row.updated_at = row.updated_at  # 保持列非空
    other = session.execute(
        select(KnowledgeItem).where(KnowledgeItem.clause_id == "RP-07")
    ).scalar_one()
    other.status = "retired"
    session.commit()

    # 签名变化 → get_retriever 自动重建(等价于 POST /api/kb/reload)
    second = get_retriever(session, MERCHANT)
    hit2 = second.search("原路退回", top_k=5)
    assert hit2.results[0].clause_id == "RP-06"
    assert hit2.results[0].version == "2"
    assert "24 小时" in hit2.results[0].text
    assert all(r.clause_id != "RP-07" for r in hit2.results), "退役条款不可见"


def test_cache_reuse_and_invalidate(session):
    _add_item(session, "RP-01", "七天无理由退货。")
    a = get_retriever(session, MERCHANT)
    b = get_retriever(session, MERCHANT)
    assert a is b, "数据签名不变应命中缓存"
    invalidate_cache(MERCHANT)
    assert get_retriever(session, MERCHANT) is not a
