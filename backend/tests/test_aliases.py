"""W7-3 查询侧同义别名扩展测试。

定标口径(数字可复算):
- 语料 = ``data/policies/*.md`` 全 18 条条款(只读,零改动),索引侧 ``build()``
  仍只调 ``tokenize``;查询侧扩展只发生在 ``tokenize_query``。
- "修复前"= ``backend.search(q, expand=False)``(纯 ``tokenize``);
  "修复后"= ``backend.search(q, expand=True)``(默认)。
- 阈值沿用 ``DEFAULT_THRESHOLD = 20.0``:低于阈值的结果被丢弃,``no_hit=True``。
  修复前某问句虽然可能已有"最高分条款",但分数 < 20 仍算 no_hit —— 表中
  ``pre_top1/pre_score`` 用 ``threshold=0.0`` 复算,便于看清真实分差。
- 表中所有分数为 BM25Plus 原始分,取 4 位小数;断言用 ``approx(abs=1e-4)``。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.retrieval.bm25_backend import (
    DEFAULT_THRESHOLD,
    QUERY_ALIASES,
    BM25Backend,
    tokenize,
    tokenize_query,
)
from app.retrieval.chunker import PolicyChunker

ROOT = Path(__file__).resolve().parents[2]
POLICIES_DIR = ROOT / "data" / "policies"

# 口语别名 → 必须补出的 canonical 词项(覆盖 W7-3 要求的 14 组语义)。
ALIAS_PROBES: list[tuple[str, tuple[str, ...]]] = [
    ("退钱", ("退款",)),
    ("能退我差价吗", ("金额",)),
    ("赔我钱", ("退款",)),
    ("衣服坏了", ("质量", "问题", "破损", "故障")),
    ("衣服有问题", ("质量", "问题")),
    ("有瑕疵", ("质量", "问题")),
    ("不想要了", ("无理", "理由", "退货")),
    ("后悔了", ("无理", "理由")),
    ("快递破了", ("破损", "外包装")),
    ("尺码不对", ("尺码",)),
    ("穿不了", ("尺码", "换货")),
    ("发错货了", ("错发",)),
    ("少发了一件", ("发货",)),
    ("能开发票吗", ("发票",)),
    ("想换个颜色", ("换货",)),
    ("还在保修期吗", ("故障",)),
    ("帮我催一下", ("时效", "现货")),
    ("我要投诉", ("人工", "客服")),
    ("免邮吗", ("包邮", "99")),
    ("新疆发货吗", ("偏远",)),
]

# (问句, 期望 clause_id, 修复前 no_hit, 修复前最高分 clause_id, 修复前该分数, 修复后分数)
# 前 5 行为"修复前 no_hit"(分数低于 20),后 4 行为原词已覆盖/不变回归样本。
CALIBRATION_CASES = [
    ("衣服不想要了能退吗", "RP-01", True, "RP-09", 18.6451, 25.0300),
    ("衣服有瑕疵怎么办", "RP-03", True, "RP-08", 13.7841, 22.3335),
    ("码数不对想换一个", "RP-05", True, "RP-05", 14.5995, 23.8224),
    ("穿不了想换货", "RP-05", True, "RP-05", 15.7521, 21.5417),
    ("退我差价", "RP-07", True, "RP-07", 0.8598, 25.4747),
    ("衣服坏了能退换吗", "RP-03", False, "RP-03", 20.1540, 33.1946),
    ("多久能到账", "RP-06", False, "RP-06", 22.3620, 22.3620),
    ("物流破损怎么处理", "LP-04", False, "LP-04", 37.2938, 37.2938),
    ("新疆包邮吗", "LP-03", False, "LP-03", 27.0399, 31.6860),
]

# 原有用例/定标问句:别名扩展必须"原词不回归"(token 与分数逐位不变)。
FROZEN_QUERIES = [
    "7天无理由能退吗",
    "衣服尺码不合适能换货吗",
    "质量问题退货运费谁出",
    "退款多久到账",
    "偏远地区包邮吗",
    "物流破损怎么处理",
    "会员积分怎么兑换",
    "店铺有没有会员折扣活动",
    "今天天气怎么样",
]

# 语料确无对应条款:别名只做诚实扩展,修复后仍必须 no_hit。
UNCOVERED_QUERIES = [
    "会员积分怎么兑换",
    "店铺有没有会员折扣活动",
    "今天天气怎么样",
    "能开发票吗",
    "我要投诉",
    "少发了一件",
    "维修要钱吗",
]


def _parse_all():
    """复用 PolicyChunker 解析全部条款(与 load_policies.parse_policies 同源)。"""
    chunker = PolicyChunker()
    chunks = []
    for path in sorted(POLICIES_DIR.glob("*.md")):
        chunks.extend(chunker.parse(path.read_text(encoding="utf-8")))
    return chunks


@pytest.fixture(scope="module")
def corpus():
    return _parse_all()


@pytest.fixture(scope="module")
def backend(corpus):
    b = BM25Backend()
    b.build(corpus)
    return b


def test_alias_table_has_at_least_12_groups():
    """别名表 ≥ 12 组,且每组口语说法都能补出语料里的 canonical 词项。"""
    assert len(QUERY_ALIASES) >= 12
    for spoken, required in ALIAS_PROBES:
        expanded = tokenize_query(spoken)
        missing = [tok for tok in required if tok not in expanded]
        assert not missing, f"{spoken!r} 未补出 {missing!r}"


def test_expand_false_falls_back_to_plain_tokenize():
    """expand=False 必须与纯 tokenize 逐位一致(测试复算修复前分数依赖它)。"""
    for q in FROZEN_QUERIES + [c[0] for c in CALIBRATION_CASES]:
        assert tokenize_query(q, expand=False) == tokenize(q)


def test_expansion_is_deduped():
    """扩展只补充缺失词项,不改变原有 token 顺序;追加部分不重复、不与原词重复。

    注意:纯 ``tokenize`` 自身可能含重复(如"店铺有没有…"里"有"出现两次),
    去重语义只约束**追加的别名词项**,不能要求整体 token 列表无重复。
    """
    for q in FROZEN_QUERIES + [c[0] for c in CALIBRATION_CASES]:
        base = tokenize(q)
        tokens = tokenize_query(q)
        assert tokens[: len(base)] == base
        extra = tokens[len(base):]
        assert len(extra) == len(set(extra)), f"{q!r} 追加词项有重复"
        assert set(extra).isdisjoint(set(base)), f"{q!r} 追加了已有词项"


def test_alias_expansion_only_on_query_side(corpus, backend):
    """索引侧零改动:内部每篇仍只按 tokenize(text) 建索引。"""
    assert backend._tokenized == [tokenize(c.text) for c in corpus]


def test_frozen_queries_do_not_regress(backend):
    """原有用例/定标问句:修复前后 token、top1、分数逐位不变。"""
    for q in FROZEN_QUERIES:
        assert tokenize_query(q) == tokenize(q), f"{q!r} 不应被别名扩展改写"
        pre = backend.search(q, top_k=1, expand=False)
        post = backend.search(q, top_k=1, expand=True)
        assert pre.no_hit == post.no_hit
        pre_ids = [r.clause_id for r in pre.results]
        post_ids = [r.clause_id for r in post.results]
        assert pre_ids == post_ids
        if pre.results:
            assert pre.results[0].score == pytest.approx(
                post.results[0].score, abs=1e-9
            )


def test_spoken_query_calibration_table(backend):
    """≥6 个口语问句(含 ≥2 修复前 no_hit):修复后 top1 == 期望条款。

    修复前状态用 ``expand=False`` 复算:先以 ``threshold=0.0`` 取最高分条款/分数
    (与文档表一致),再断言默认阈值下确实 no_hit。
    """
    assert len(CALIBRATION_CASES) >= 6
    assert sum(1 for c in CALIBRATION_CASES if c[2]) >= 2

    for query, expect, pre_no_hit, pre_top, pre_score, post_score in (
        CALIBRATION_CASES
    ):
        pre_any = backend.search(query, top_k=1, threshold=0.0, expand=False)
        assert pre_any.results, f"{query!r} 修复前应至少有一个字面重叠条款"
        assert pre_any.results[0].clause_id == pre_top
        assert pre_any.results[0].score == pytest.approx(pre_score, abs=1e-4)

        pre = backend.search(query, expand=False)
        assert pre.no_hit is pre_no_hit, f"{query!r} 修复前 no_hit 状态不符"

        post = backend.search(query)
        assert not post.no_hit, f"{query!r} 修复后应命中"
        assert post.results[0].clause_id == expect, (
            f"{query!r} 修复后 top1={post.results[0].clause_id},期望 {expect}"
        )
        assert post.results[0].score == pytest.approx(post_score, abs=1e-4)


def test_uncovered_queries_still_no_hit(backend):
    """语料未覆盖的问句:别名扩展不能"无中生有",仍 no_hit=True。"""
    for q in UNCOVERED_QUERIES:
        result = backend.search(q)
        assert result.no_hit, f"{q!r} 不应因别名扩展被判为覆盖"
        assert result.results == []


def test_default_threshold_not_adjusted():
    """W7-3 未调整阈值:8 个原定标问句分布未变,阈值仍为 20.0。"""
    assert DEFAULT_THRESHOLD == 20.0
