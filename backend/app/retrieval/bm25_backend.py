"""BM25 检索后端(rank-bm25)。

与正例 `私有化AI助手/backend/services/bm25_service.py` 相比,刻意"反过来做"的两点:
1. **启动无条件重建**:``build()`` 每次全量重建索引,不增量(正例启动不重建 → 陈旧)。
2. **带 rerank 阈值**:``search`` 低于 ``threshold`` 显式返回空 + ``no_hit=True``(正例无阈值 → 硬凑)。

分词:项目不依赖 jieba(不在 requirements.lock.txt,且任务书禁止新增依赖),
采用无依赖的"ASCII 字母数字串成词 + 中文字符 unigram + 中文字符 bigram"方案,
对"7天无理由 / 15天质量 / 运费承担"这类短语可稳定召回(见 ``tokenize``)。

threshold 定标依据(数字可复算;定标脚本为一次性内存脚本,跑完即删,不落盘):

用 8 个定标问句(6 个政策覆盖 + 2 个政策未覆盖)实测 BM25Plus 分数分布:

- 覆盖问句 top1 分数(下界 24.2297):
  7天无理由能退吗 → RP-01 = 24.2297;
  衣服尺码不合适能换货吗 → RP-05 = 38.2489;
  质量问题退货运费谁出 → RP-02 = 26.0909;
  退款多久到账 → RP-06 = 26.2416;
  偏远地区包邮吗 → LP-02 = 47.8596;
  物流破损怎么处理 → LP-04 = 37.2938。
- 未覆盖问句 top1 分数(上界 15.1093,仅字面弱重叠,如"兑换"的"换"字):
  会员积分怎么兑换 → 8.4758;
  店铺有没有会员折扣活动 → 15.1093。
- 弱相关参考:今天天气怎么样 → 4.8720。

选值 ``DEFAULT_THRESHOLD = 20.0`` 的理由:未覆盖上界 15.1093 与覆盖下界
24.2297 之间恰好留有约 9 分的空隙;20.0 取在两者之间的干净整数,覆盖问句全部
≥ 24.23(> 20 → 命中),未覆盖问句全部 ≤ 15.11(< 20 → no_hit),两边各留约 4~5 分
安全边际,既不漏召回也不把弱重叠噪声判成命中。
"""
from __future__ import annotations

import re

from rank_bm25 import BM25Plus

from app.retrieval.base import RetrievedChunk, SearchResult
from app.retrieval.chunker import PolicyChunk

# 检索分数阈值:低于此值的命中视为"政策未覆盖"(no_hit=True)。
# 定标依据见本模块 docstring 末尾;8 个定标问句分布见 load_policies 定标流程与
# tests/test_retrieval.py 的阈值断言。
DEFAULT_THRESHOLD: float = 20.0


def tokenize(text: str) -> list[str]:
    """中文无依赖分词:ASCII 字母数字串 + 中文字符 unigram + 中文字符 bigram。

    - 连续 ``[A-Za-z0-9]`` 串整体成一个词(小写),保留 "7"、"v1"、"15" 等。
    - 每个 CJK 字符单独成词(unigram),保底单字召回。
    - 相邻两个 CJK 字符成 bigram("无理由" → "无理"、"理由"),短语匹配更准。
    """
    tokens: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9]+", text):
        tokens.append(m.group().lower())
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(cjk)
    for i in range(len(cjk) - 1):
        tokens.append(cjk[i] + cjk[i + 1])
    return tokens


class BM25Backend:
    """rank-bm25(BM25Plus)检索器:启动无条件重建,search 带 threshold。

    - ``build(chunks)``:无条件全量重建索引;空列表清空索引。
    - ``search(query, top_k, threshold)``:返回 ``SearchResult``;低于阈值返回
      空列表且 ``no_hit=True``。
    """

    def __init__(self) -> None:
        self._chunks: list[PolicyChunk] = []
        self._tokenized: list[list[str]] = []
        self._bm25: BM25Plus | None = None

    def build(self, chunks: list[PolicyChunk]) -> None:
        """无条件重建索引(每次全量,不增量;空列表清空索引)。"""
        self._chunks = list(chunks)
        self._tokenized = [tokenize(c.text) for c in self._chunks]
        if self._tokenized:
            self._bm25 = BM25Plus(self._tokenized)
        else:
            self._bm25 = None

    def search(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> SearchResult:
        """BM25 检索,返回 SearchResult(results + no_hit)。

        - 无索引 / 空查询 / 无有效 token → 空结果 + no_hit=True。
        - 只保留与查询至少共享一个词项的文档(BM25Plus 的 δ 下限项会让零重叠
          文档拿到正分,必须先做词项重叠过滤,再按分数降序截断)。
        - 分数低于 threshold 的命中被丢弃;最终 results 为空则 no_hit=True。
        """
        if self._bm25 is None or not query.strip():
            return SearchResult(results=[], no_hit=True)

        q_tokens = tokenize(query)
        q_set = set(q_tokens)
        if not q_set:
            return SearchResult(results=[], no_hit=True)

        scores = self._bm25.get_scores(q_tokens)

        # 词项重叠过滤(正例 bm25_service.py 的 M5 教训:先过滤、再排序截断)
        candidates: list[tuple[float, int]] = []
        for idx, (doc_tokens, score) in enumerate(
            zip(self._tokenized, scores)
        ):
            if set(doc_tokens) & q_set:
                candidates.append((float(score), idx))

        candidates.sort(key=lambda x: x[0], reverse=True)

        results: list[RetrievedChunk] = []
        for score, idx in candidates[:top_k]:
            if score < threshold:
                break
            chunk = self._chunks[idx]
            results.append(
                RetrievedChunk(
                    clause_id=chunk.clause_id,
                    version=chunk.version,
                    text=chunk.text,
                    score=score,
                )
            )
        return SearchResult(results=results, no_hit=len(results) == 0)
