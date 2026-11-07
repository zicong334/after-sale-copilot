"""W2-A 政策检索测试:条款解析数对账、BM25 top1 命中、未覆盖 no_hit、
threshold 定标行为、load 幂等、VectorBackend 扩展点、8 定标问句。

不写任何临时文件(load 幂等走 session fixture),不违反沙箱护栏。
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import KnowledgeItem
from app.retrieval.base import RetrievedChunk, SearchResult, VectorBackend
from app.retrieval.bm25_backend import BM25Backend, DEFAULT_THRESHOLD, tokenize
from app.retrieval.chunker import PolicyChunk, PolicyChunker

ROOT = Path(__file__).resolve().parents[2]
POLICIES_DIR = ROOT / "data" / "policies"
LOAD_SCRIPT = ROOT / "backend" / "scripts" / "load_policies.py"

# 6 个政策覆盖问句 → 期望 top1 clause_id(定标实测,数字见 bm25_backend docstring)
COVERED_QUERIES = {
    "7天无理由能退吗": "RP-01",
    "衣服尺码不合适能换货吗": "RP-05",
    "质量问题退货运费谁出": "RP-02",
    "退款多久到账": "RP-06",
    "偏远地区包邮吗": "LP-02",
    "物流破损怎么处理": "LP-04",
}
# 2 个政策未覆盖问句(仅字面弱重叠,应 no_hit)
UNCOVERED_QUERIES = ["会员积分怎么兑换", "店铺有没有会员折扣活动"]


def _load_module(name: str, path: Path):
    """按文件路径加载一个脚本模块(不污染 sys.modules 通用命名)。"""
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_all() -> list[PolicyChunk]:
    chunker = PolicyChunker()
    chunks: list[PolicyChunk] = []
    for path in sorted(POLICIES_DIR.glob("*.md")):
        chunks.extend(chunker.parse(path.read_text(encoding="utf-8")))
    return chunks


def _build_backend(chunks: list[PolicyChunk]) -> BM25Backend:
    backend = BM25Backend()
    backend.build(chunks)
    return backend


# ---------------------------------------------------------------------------
# 1) 条款解析数对账:18 = 12(RP) + 6(LP),clause_id 与 md 结构一致
# ---------------------------------------------------------------------------
def test_parse_returns_18_clauses_matching_md_headers():
    chunks = _parse_all()
    assert len(chunks) == 18, f"条款解析数应为 18,实际 {len(chunks)}"

    # clause_id 从 md `## ` 结构解析:与 md 文件里的标题数逐一对账
    md_ids: list[str] = []
    for path in sorted(POLICIES_DIR.glob("*.md")):
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("## "):
                md_ids.append(s[3:].split(None, 1)[0])
    parsed_ids = [c.clause_id for c in chunks]
    assert parsed_ids == md_ids, "解析出的 clause_id 必须与 md `##` 标题顺序一致"


def test_clause_ids_no_handwritten_mapping():
    chunks = _parse_all()
    rp = sorted(c.clause_id for c in chunks if c.clause_id.startswith("RP-"))
    lp = sorted(c.clause_id for c in chunks if c.clause_id.startswith("LP-"))
    assert rp == [f"RP-{i:02d}" for i in range(1, 13)], "RP 应为 12 条连续编号"
    assert lp == [f"LP-{i:02d}" for i in range(1, 7)], "LP 应为 6 条连续编号"


def test_chunk_has_version_and_effective_date():
    chunks = _parse_all()
    for c in chunks:
        assert c.version == "v1", f"{c.clause_id}.version 应为 v1"
        assert c.effective_date == "2026-01-01", (
            f"{c.clause_id}.effective_date 应为 2026-01-01"
        )
        assert c.text.strip(), f"{c.clause_id} 正文不应为空"


# ---------------------------------------------------------------------------
# 2) tokenize:ASCII 词 + CJK unigram + bigram(零依赖)
# ---------------------------------------------------------------------------
def test_tokenize_ascii_and_cjk():
    toks = tokenize("7天无理由")
    assert "7" in toks, "ASCII 数字串应整体成词"
    for ch in ("天", "无", "理", "由"):
        assert ch in toks, f"CJK unigram 缺 {ch}"
    assert "无理由" not in toks and ("无理" in toks and "理由" in toks), (
        "应有 bigram(无理/理由),不应有三字整体"
    )


def test_tokenize_ascii_lowercase():
    toks = tokenize("V1 尺码")
    assert "v1" in toks and "V1" not in toks, "ASCII 串应小写化"


# ---------------------------------------------------------------------------
# 3) BM25 检索:top1 命中正确条款 / 未覆盖 no_hit / 阈值行为
# ---------------------------------------------------------------------------
def test_search_7day_return_hits_rp01():
    backend = _build_backend(_parse_all())
    result = backend.search("7天无理由能退吗", top_k=3)
    assert result.results, "应命中条款"
    assert result.results[0].clause_id == "RP-01", (
        f"top1 应为 RP-01,实际 {result.results[0].clause_id}"
    )
    assert not result.no_hit


def test_search_uncovered_returns_no_hit():
    backend = _build_backend(_parse_all())
    for q in UNCOVERED_QUERIES:
        result = backend.search(q, top_k=3)
        assert result.no_hit, f"未覆盖问句 {q!r} 应 no_hit=True"
        assert result.results == [], f"未覆盖问句 {q!r} 应返回空列表"


def test_search_threshold_default_is_20():
    assert DEFAULT_THRESHOLD == 20.0, "默认阈值应为定标值 20.0"
    backend = _build_backend(_parse_all())
    # 弱相关问句(仅"天"字重叠)在阈值下应 no_hit
    weak = backend.search("今天天气怎么样", top_k=3)
    assert weak.no_hit, "弱相关问句应 no_hit"


def test_search_empty_query_and_no_index():
    backend = BM25Backend()  # 未 build
    assert backend.search("随便").no_hit, "空索引应 no_hit"
    built = _build_backend(_parse_all())
    assert built.search("   ").no_hit, "空查询应 no_hit"
    assert built.search("").no_hit, "空字符串应 no_hit"


def test_search_result_shape_and_version():
    backend = _build_backend(_parse_all())
    result = backend.search("退款多久到账", top_k=1)
    assert isinstance(result, SearchResult)
    top = result.results[0]
    assert isinstance(top, RetrievedChunk)
    assert top.clause_id == "RP-06"
    assert top.version == "v1"
    assert top.score >= DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# 4) load 幂等:upsert 两遍 knowledge_item 行数不变(仍为 18)
# ---------------------------------------------------------------------------
def test_load_upsert_idempotent(session):
    load_mod = _load_module("load_policies", LOAD_SCRIPT)
    chunks = load_mod.parse_policies(POLICIES_DIR)
    assert len(chunks) == 18

    first = load_mod.upsert_policies(session, chunks)
    second = load_mod.upsert_policies(session, chunks)

    assert first["inserted"] == 18 and first["updated"] == 0
    assert second["inserted"] == 0 and second["updated"] == 18
    assert first["total"] == 18 and second["total"] == 18, (
        "两遍装载后 knowledge_item 行数必须都是 18"
    )

    # 直接查库佐证(而非只信 upsert 返回值)
    count = session.scalar(
        select(func.count()).select_from(KnowledgeItem).where(
            KnowledgeItem.deleted_at.is_(None)
        )
    )
    assert count == 18


# ---------------------------------------------------------------------------
# 5) VectorBackend 是声明式扩展点:抽象 + 抛 NotImplementedError
# ---------------------------------------------------------------------------
def test_vector_backend_is_abstract():
    assert inspect.isabstract(VectorBackend), "VectorBackend 应为抽象类"
    assert {"index", "search"} <= VectorBackend.__abstractmethods__, (
        "index/search 应为抽象方法"
    )


def test_vector_backend_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        VectorBackend.index(None, [])
    with pytest.raises(NotImplementedError):
        VectorBackend.search(None, "query")


# ---------------------------------------------------------------------------
# 6) 8 个定标问句整体断言(6 覆盖 top1 + 2 未覆盖 no_hit)
# ---------------------------------------------------------------------------
def test_calibration_queries_full_matrix():
    backend = _build_backend(_parse_all())
    for q, want_clause in COVERED_QUERIES.items():
        result = backend.search(q, top_k=3)
        assert not result.no_hit, f"覆盖问句 {q!r} 不应 no_hit"
        assert result.results[0].clause_id == want_clause, (
            f"{q!r} top1 应为 {want_clause},实际 {result.results[0].clause_id}"
        )
    for q in UNCOVERED_QUERIES:
        result = backend.search(q, top_k=3)
        assert result.no_hit, f"未覆盖问句 {q!r} 应 no_hit"
