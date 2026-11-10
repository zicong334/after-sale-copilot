"""W9-1 稠密向量后端:sklearn TF-IDF + TruncatedSVD(本机形态实现)。

生产缝(与 W9 任务书 §2 规划一致)
------------------------------------------------
本机没有 GPU / 向量库,用 scikit-learn 在内存里拟合:
``TfidfVectorizer(FeatureUnion) → TruncatedSVD(dim=64) → L2 归一化``。
它 **fulfill** :class:`app.retrieval.base.VectorBackend` 抽象契约;
``base.VectorBackend`` 本身保留,供 pgvector / Milvus 后端实现同一接口
(生产环境把 :meth:`TfidfSvdVectorBackend.search` 换成 ANN 查询、
:meth:`TfidfSvdVectorBackend.export_vectors` 换成写 ``kb_vector`` /
向量库,上层 ``HybridRetriever`` 无需改动)。

特征工程(两路拼接,兼顾抗分词误差与词项精确)
------------------------------------------------
1. ``char_wb`` 2-3 gram:中文没有空格,``char_wb`` 在无空格文本上退化为
   带边界填充的字符 n-gram —— 对"退钱"/"退款"这类一字之差天然鲁棒。
2. ``word`` 1-2 gram,``tokenizer=tokenize``:复用 BM25 同一套词项口径
   (ASCII 串 + CJK unigram + CJK bigram),让两路检索共享词表先验。

降维与归一化
------------
- ``TruncatedSVD(n_components=dim, random_state=RANDOM_STATE)``:固定随机
  种子,同一语料多次拟合结果可复现。
- 小语料保护:``n_components = max(1, min(dim, n_samples-1, n_features-1))``,
  否则 SVD 在 18 条条款 / 热更新后的少量条款上无法拟合。
- 输出做 L2 归一化,余弦相似度 = 点积,分数落在 ``[-1, 1]``(实际多为
  ``[0, 1]``)。

持久化
------
:meth:`TfidfSvdVectorBackend.export_vectors` 返回
``[(clause_id, version, float32 bytes)]``,由 ``HybridRetriever`` 写入
``kb_vector`` 表(迁移 0007);本模块不直接依赖 DB,保持检索层纯净。
"""
from __future__ import annotations

import warnings
from typing import Iterable

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.preprocessing import normalize

from app.retrieval.base import RetrievedChunk, SearchResult, VectorBackend
from app.retrieval.bm25_backend import strip_stopwords, tokenize
from app.retrieval.chunker import PolicyChunk

VECTOR_MODEL_NAME = "tfidf-svd/char_wb2-3+word1-2/v1"
"""落 ``kb_vector.model`` 的模型标识;改动特征或降维必须升版本。"""

VECTOR_DIM = 64
"""目标维度;小语料会自动降维,实际维度见 ``backend.dim``。"""

RANDOM_STATE = 42
"""TruncatedSVD 随机种子(固定,保证可复现)。"""

VECTOR_FLOOR = 0.0
"""默认余弦下限:<= 0 视为不相关(HybridRetriever 用融合分阈值再过滤)。"""


def _vector_text(chunk: PolicyChunk) -> str:
    """向量化文本 = 标题 + 正文;标题重复不伤害 TF-IDF 的相对权重。"""
    title = (chunk.title or "").strip()
    return f"{title} {chunk.text}".strip()


class TfidfSvdVectorBackend(VectorBackend):
    """TF-IDF(char_wb + word)→ TruncatedSVD → L2 归一化 的稠密向量后端。

    生产缝:本机用 sklearn 拟合,pgvector / Milvus 后端实现同一 ``VectorBackend``
    契约即可替换(详见模块 docstring)。

    用法与 ``BM25Backend`` 对齐::

        backend = TfidfSvdVectorBackend()
        backend.index(chunks)          # 全量重建(空列表清空)
        result = backend.search(q, top_k=5, threshold=0.0)
    """

    def __init__(
        self,
        *,
        dim: int = VECTOR_DIM,
        random_state: int = RANDOM_STATE,
        clean: bool = True,
    ) -> None:
        self._dim_target = int(dim)
        self._random_state = int(random_state)
        self._clean = bool(clean)
        self._chunks: list[PolicyChunk] = []
        self._vectorizer: FeatureUnion | None = None
        self._svd: TruncatedSVD | None = None
        self._matrix: np.ndarray | None = None
        self.dim: int = 0

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------
    def index(self, chunks: Iterable[PolicyChunk]) -> None:
        """全量重拟合:语料 = 传入条款(调用方只传 active 条款)。"""
        self._chunks = list(chunks)
        if not self._chunks:
            self._vectorizer = None
            self._svd = None
            self._matrix = None
            self.dim = 0
            return

        corpus = [self._prepare(_vector_text(c)) for c in self._chunks]
        self._vectorizer = self._build_vectorizer()
        tfidf = self._vectorizer.fit_transform(corpus)

        n_components = max(
            1,
            min(self._dim_target, tfidf.shape[0] - 1, tfidf.shape[1] - 1),
        )
        self._svd = TruncatedSVD(
            n_components=n_components, random_state=self._random_state
        )
        # 极小语料(条款数 < 3)时 SVD 的总方差可能为 0,sklearn 内部算
        # explained_variance_ratio_ 会出一个除零 RuntimeWarning;该属性本实现
        # 不使用(只取变换后的稠密向量),故此处静默,避免污染测试与服务日志。
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="invalid value encountered in divide",
                category=RuntimeWarning,
            )
            dense = self._svd.fit_transform(tfidf)
        self.dim = int(n_components)
        self._matrix = normalize(np.asarray(dense, dtype=np.float64))

    def _build_vectorizer(self) -> FeatureUnion:
        return FeatureUnion(
            [
                (
                    "char",
                    TfidfVectorizer(
                        analyzer="char_wb",
                        ngram_range=(2, 3),
                        sublinear_tf=True,
                        min_df=1,
                    ),
                ),
                (
                    "word",
                    TfidfVectorizer(
                        analyzer="word",
                        tokenizer=tokenize,
                        preprocessor=None,
                        token_pattern=None,
                        lowercase=False,
                        ngram_range=(1, 2),
                        sublinear_tf=True,
                        min_df=1,
                    ),
                ),
            ]
        )

    def _prepare(self, text: str) -> str:
        """查询侧与索引侧共用同一清洗口径(停用词只在 Hybrid 路径开启)。"""
        return strip_stopwords(text) if self._clean else text

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(
        self, query: str, top_k: int = 5, threshold: float = VECTOR_FLOOR
    ) -> SearchResult:
        """余弦 top-k;``threshold`` 是余弦下限(含等于)。"""
        if (
            self._matrix is None
            or self._svd is None
            or self._vectorizer is None
            or not query.strip()
        ):
            return SearchResult(results=[], no_hit=True)

        query_vec = self._svd.transform(
            self._vectorizer.transform([self._prepare(query)])
        )
        query_vec = normalize(np.asarray(query_vec, dtype=np.float64))
        sims = self._matrix @ query_vec.ravel()

        order = np.argsort(-sims)[: max(0, int(top_k))]
        results: list[RetrievedChunk] = []
        for idx in order:
            score = float(sims[idx])
            if score < threshold:
                break
            chunk = self._chunks[int(idx)]
            results.append(
                RetrievedChunk(
                    clause_id=chunk.clause_id,
                    version=chunk.version,
                    text=chunk.text,
                    score=score,
                )
            )
        return SearchResult(results=results, no_hit=len(results) == 0)

    # ------------------------------------------------------------------
    # 持久化辅助(写 kb_vector 表用)
    # ------------------------------------------------------------------
    def export_vectors(self) -> list[tuple[str, str, bytes]]:
        """导出 ``[(clause_id, version, vec_bytes)]``(float32 小端,长度 = dim*4)。"""
        if self._matrix is None:
            return []
        out: list[tuple[str, str, bytes]] = []
        for chunk, row in zip(self._chunks, self._matrix):
            out.append(
                (
                    chunk.clause_id,
                    chunk.version,
                    np.asarray(row, dtype="<f4").tobytes(),
                )
            )
        return out

    @property
    def model_name(self) -> str:
        return VECTOR_MODEL_NAME


def decode_vector(blob: bytes) -> np.ndarray:
    """把 ``kb_vector.vec`` BLOB 还原成 float32 一维数组(供测试/校验用)。"""
    return np.frombuffer(blob, dtype="<f4").copy()


__all__ = [
    "RANDOM_STATE",
    "VECTOR_DIM",
    "VECTOR_FLOOR",
    "VECTOR_MODEL_NAME",
    "TfidfSvdVectorBackend",
    "decode_vector",
]
