"""W10-2 语义缓存:在 classify 之前命中"同一问题换个说法"的问答。

定位
----
缓存层放在 **classify 之前** —— 调用方(跑图之前)先用用户原文查缓存,命中就
直返缓存里的 ``reply`` / ``citations``,响应标 ``cache_hit: true``、usage 记
**0 token**;未命中才继续跑图,跑完把结果 ``store()`` 回来。

三条口径
--------
1. **规范化**(``_normalize``):小写 → 全角转半角 → 去标点/空白 → 繁简归一
   → 口语同义归一 → 去停用词。目的是让"退货期限是几天"与"退貨期限要多久"
   落到同一串。
2. **相似度**:``sklearn.feature_extraction.text.TfidfVectorizer(analyzer="char_wb",
   ngram_range=(2,3))`` 在"查询 + 全部未过期条目"这个语料上拟合,取 **余弦**,
   最高分 ``>= threshold``(默认 0.92)即命中。

   ⚠️ 为什么必须用字符 n-gram:中文没有空格,若整句成一个 token,改写句之间
   词集无交集,余弦恒为 0 —— 字符 2/3-gram 才能让改写句落到 0.92 以上。
   短文本(不足 n-gram 窗口)退化为精确/包含判断,不崩。
3. **永不缓存**(``is_cacheable``):①写操作意图;②含订单号;③含 PII 占位符或
   原文。写操作意图用**模块级关键词常量表**判定(不是按用例特判),并额外
   看 ``intent`` 名称(如 ``handoff`` / ``order_judge``)。

失效
----
- TTL 默认 30 分钟(``ttl_seconds=1800``),过期条目惰性清理。
- ``clear()`` 手动失效;``app.retrieval.hybrid.invalidate_cache()`` 在 KB 热
  更新时**顺带**调用它(条款变了,旧答案不能继续命中)。

无网络
------
本模块只做本地字符串计算(纯 CPU、无 IO、无网络),可直接进单测。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from sklearn.feature_extraction.text import TfidfVectorizer

from app.guardrails import (
    PII_ID_CARD_RE,
    PII_PHONE_RE,
    PII_PLACEHOLDER_RE,
)

__all__ = [
    "CacheEntry",
    "MIN_SIMILARITY_LEN",
    "POLICY_QUESTION_MARKERS",
    "SemanticCache",
    "STOPWORDS",
    "SYNONYM_GROUPS",
    "WRITE_INTENT_ACTION_KEYWORDS",
    "WRITE_INTENT_REQUEST_CUES",
    "WRITE_INTENT_STRONG_KEYWORDS",
    "semantic_cache",
]


# ---------------------------------------------------------------------------
# 规范化用的常量表
# ---------------------------------------------------------------------------

#: 停用词:问句客套、语气词与虚词。按**子串**删除(中文无空格),长词优先。
#:
#: 口径刻意保守:只收几乎不承载业务语义的虚词,避免把两个不同问题归一成
#: 同一串(归一后相同 → 余弦 1.0 → 误命中)。
STOPWORDS: frozenset[str] = frozenset(
    {
        # 客套/疑问(多字)
        "请问", "麻烦", "一下", "有没有", "是不是", "能不能", "可不可以",
        "为什么", "怎么", "怎样", "如何", "什么", "多少", "哪些", "哪个",
        "我想", "我要", "我需要", "我想要", "帮我", "替我", "给我", "马上",
        "立即", "立刻", "可以", "还是", "的话", "这个", "那个",
        # 语气/虚词(单字)
        "的", "了", "呢", "吗", "吧", "啊", "呀", "哦", "嗯", "哈", "呗",
        "啦", "嘛", "么", "没", "是", "我", "你", "您", "咱", "就", "都",
        "也", "还", "再", "很", "和", "与", "把", "被", "要", "能", "会",
    }
)
"""停用词集合(63 词):多字 30 个 + 单字 33 个。"""

_STOPWORDS_BY_LENGTH: tuple[str, ...] = tuple(
    sorted(STOPWORDS, key=lambda w: (-len(w), w))
)
"""按长度降序的替换顺序:保证"为什么"先于"什么"被删。"""

#: 口语同义归一:每组第一个是**规范形式**,其余是同义说法。
#:
#: 覆盖的是电商售后域常见问法族(时间问法 / 事务动作 / 费用名目),
#: 目的只有一个:让"换个说法"的同一问题归一到同一串,否则 0.92 余弦打不到。
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    # 时间问法族
    ("几天", "多久", "多长时间", "多少天", "几天时间", "几天之内", "几天内",
     "几天以内", "多久能"),
    # 事务动作族
    ("退货", "退貨", "退回商品"),
    ("退款", "退錢", "退钱", "退费"),
    ("取消", "撤销", "取消掉"),
    ("修改", "更改", "变更"),
    ("申请", "申报", "办理"),
    ("投诉", "举报"),
    # 费用名目族
    ("运费", "邮费", "邮运费"),
    ("发票", "开票凭证"),
)
"""口语同义归一表(规范形式在首位)。"""

#: 繁简归一:售后/电商域常用繁体字(取常用子集,不是完整 OpenCC 表)。
TRAD_TO_SIMPLIFY: dict[str, str] = {
    "貨": "货", "單": "单", "網": "网", "機": "机", "買": "买", "賣": "卖",
    "錢": "钱", "費": "费", "運": "运", "郵": "邮", "電": "电", "話": "话",
    "轉": "转", "換": "换", "應": "应", "該": "该", "規": "规", "則": "则",
    "條": "条", "遞": "递", "時": "时", "間": "间", "讓": "让", "給": "给",
    "類": "类", "頁": "页", "標": "标", "價": "价", "銀": "银", "兩": "两",
    "沒": "没", "個": "个", "們": "们", "這": "这", "說": "说", "問": "问",
    "訂": "订", "驗": "验", "賠": "赔", "償": "偿", "壓": "压", "檔": "档",
    "檢": "检", "測": "测", "訊": "讯", "響": "响", "據": "据", "擔": "担",
    "認": "认", "為": "为", "產": "产", "購": "购", "賬": "账", "號": "号",
    "碼": "码", "續": "续", "傳": "传", "儲": "储", "資": "资", "訊": "讯",
}
"""繁→简单字映射(售后域常用字)。"""

#: 全角 ASCII → 半角。
_FULLWIDTH_TABLE: dict[int, int] = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH_TABLE[0x3000] = 0x20

#: 去标点/空白:只保留中文、字母、数字。
_PUNCT_RE = re.compile(r"[^0-9a-z一-鿿]+")


# ---------------------------------------------------------------------------
# 永不缓存:写操作意图
# ---------------------------------------------------------------------------

#: 出现即判写操作意图的强动作词(与"政策问句"无关,一律不缓存)。
WRITE_INTENT_STRONG_KEYWORDS: tuple[str, ...] = (
    "转人工", "转接人工", "人工客服", "找客服", "投诉", "举报",
    "赔偿", "赔付", "补偿",
    "取消订单", "修改订单", "删除订单", "提交订单", "提交申请", "提交工单",
    "申请退款", "申请退货", "退货申请", "退款申请",
    "发起退款", "发起退货", "发起投诉", "发起申请",
    "立即退款", "马上退款", "立刻退款", "退款到账",
    "帮我退款", "帮我退货", "帮我取消", "帮我修改",
    "换货", "补发", "寄回", "下单", "开票", "开发票", "注销", "解绑",
)
"""强写意图关键词:出现即不缓存。"""

#: 需要配合"请求语气词"才判写意图的动作词(政策问句里常出现同名词)。
WRITE_INTENT_ACTION_KEYWORDS: tuple[str, ...] = (
    "退款", "退钱", "退货", "退掉", "取消", "撤销", "修改", "提交",
    "申请", "变更", "删除", "撤回", "重开", "重下",
)
"""动作词:本身还可能是政策问句的主语,需配合请求语气词才判写意图。"""

#: 请求语气词:把动作词从"话题"变成"指令"。
WRITE_INTENT_REQUEST_CUES: tuple[str, ...] = (
    "我要", "我想", "我需要", "我想要", "帮我", "替我", "给我", "麻烦帮",
    "马上", "立即", "立刻", "现在就", "我来", "帮我把", "我去",
)
"""请求语气词(有"我要退货"这类句式,没有就只是聊到"退货")。"""

#: 政策问句标记:出现即说明这句在**问规则**而不是**下指令**。
POLICY_QUESTION_MARKERS: tuple[str, ...] = (
    "怎么", "怎样", "如何", "多久", "几天", "多少", "什么", "哪些",
    "哪个", "为什么", "规定", "政策", "流程", "条件", "标准", "期限",
    "要求", "能否", "可不可以", "可以吗", "在哪", "区别", "解释",
    "说明", "规则", "时效", "怎么算", "算不算",
)
"""政策问句标记(把动作词从"指令"还原成"话题")。"""

#: 视为写操作的意图名(来自 LLM 意图枚举与常见自定义意图)。
WRITE_INTENT_NAMES: frozenset[str] = frozenset(
    {
        "handoff", "order_judge", "refund", "refund_request", "return_request",
        "cancel", "cancel_order", "modify", "modify_order", "complaint",
        "ticket", "apply", "submit", "compensation", "exchange", "handover",
    }
)
"""写操作意图名(精确匹配)。"""

#: 视为写操作的意图名片段(意图名可能是组合词)。
WRITE_INTENT_NAME_TOKENS: tuple[str, ...] = (
    "refund", "return", "cancel", "modify", "change", "complaint", "ticket",
    "handoff", "apply", "submit", "compensat", "exchange", "create",
    "update", "delete", "write",
)
"""写操作意图名片段(子串匹配,小写)。"""


# ---------------------------------------------------------------------------
# 永不缓存:订单号 / PII
# ---------------------------------------------------------------------------

#: 订单号形态:``seed-order-019`` / ``ORD-xxx`` / ``SO123456`` / 订单号+值 /
#: 长数字串(12 位以上,避开 11 位手机号)。
ORDER_NUMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"seed[-_ ]?order[-_ ]?[A-Za-z0-9]+", re.IGNORECASE),
    re.compile(r"\b(?:ORD|SO|DD|TNO|ORDER)[-_]?\d{3,}[A-Za-z0-9-]*\b", re.IGNORECASE),
    re.compile(r"订单号?\s*[:：#]?\s*[A-Za-z0-9][A-Za-z0-9-]{3,}", re.IGNORECASE),
    re.compile(r"(?<!\d)\d{12,}(?!\d)"),
)
"""订单号识别正则组(任一命中即不可缓存)。"""

#: PII 形态:占位符(``[PHONE_1]``/``[ID_1]``/``[ADDR_1]``)与原文手机号/身份证号。
PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    PII_PLACEHOLDER_RE,
    PII_PHONE_RE,
    PII_ID_CARD_RE,
)
"""PII 识别正则组(含占位符与原文)。"""


# ---------------------------------------------------------------------------
# 规范化实现
# ---------------------------------------------------------------------------

def _to_halfwidth(text: str) -> str:
    """全角 ASCII/全角空格 → 半角。"""
    return text.translate(_FULLWIDTH_TABLE)


def _trad_to_simplify(text: str) -> str:
    """繁体字 → 简体字(售后域常用字表)。"""
    return "".join(TRAD_TO_SIMPLIFY.get(ch, ch) for ch in text)


def _apply_synonyms(text: str) -> str:
    """口语同义归一:把同义说法替换成规范形式(长变体优先)。"""
    out = text
    for group in SYNONYM_GROUPS:
        canonical = group[0]
        variants = tuple(sorted(group[1:], key=len, reverse=True))
        for variant in variants:
            if variant in out:
                out = out.replace(variant, canonical)
    return out


def _strip_stopwords(text: str) -> str:
    """按子串删停用词(长词优先),不做词形变换。"""
    out = text
    for word in _STOPWORDS_BY_LENGTH:
        if word in out:
            out = out.replace(word, "")
    return out


def _normalize(text: str) -> str:
    """规范化文本:小写 → 半角 → 去标点 → 繁简 → 同义 → 去停用词。

    归一后的串既是**缓存键**也是**相似度比较的输入**;查询与入库走同一条
    流水线,因此"同义改述"能落到同一串。
    """
    body = str(text or "").lower()
    body = _to_halfwidth(body)
    body = _PUNCT_RE.sub("", body)
    body = _trad_to_simplify(body)
    body = _apply_synonyms(body)
    body = _strip_stopwords(body)
    return body.strip()


# ---------------------------------------------------------------------------
# 不可缓存判定
# ---------------------------------------------------------------------------

def _intent_is_write(intent: str | None) -> bool:
    """意图名是否属于写操作(``handoff`` / ``order_judge`` / 退款类)。"""
    name = str(intent or "").strip().lower()
    if not name:
        return False
    if name in WRITE_INTENT_NAMES:
        return True
    return any(token in name for token in WRITE_INTENT_NAME_TOKENS)


def _text_is_write_intent(text: str) -> bool:
    """文本是否表达**写操作意图**(而不是聊到某个动作词)。

    判定口径(模块级关键词表,不针对任何具体用例):

    1. 命中 ``WRITE_INTENT_STRONG_KEYWORDS`` → 写意图;
    2. 否则命中 ``WRITE_INTENT_ACTION_KEYWORDS``(退款/退货/取消/修改/提交/申请…)
       且命中 ``WRITE_INTENT_REQUEST_CUES``(我要/帮我/马上…) → 写意图;
    3. 动作词同时命中 ``POLICY_QUESTION_MARKERS``(怎么/多久/规定/流程…) → 视为
       **政策问句**,不算写意图(否则"退货期限是几天"这种可缓存问句会被误杀)。
    """
    body = _trad_to_simplify(str(text or ""))
    if any(keyword in body for keyword in WRITE_INTENT_STRONG_KEYWORDS):
        return True
    if not any(keyword in body for keyword in WRITE_INTENT_ACTION_KEYWORDS):
        return False
    if any(marker in body for marker in POLICY_QUESTION_MARKERS):
        return False
    return any(cue in body for cue in WRITE_INTENT_REQUEST_CUES)


def _has_order_number(text: str) -> bool:
    """文本是否含订单号。"""
    body = str(text or "")
    return any(pattern.search(body) for pattern in ORDER_NUMBER_PATTERNS)


def _has_pii(text: str) -> bool:
    """文本是否含 PII 占位符或原文(手机号 / 身份证号)。"""
    body = str(text or "")
    return any(pattern.search(body) for pattern in PII_PATTERNS)


# ---------------------------------------------------------------------------
# 相似度
# ---------------------------------------------------------------------------

MIN_SIMILARITY_LEN = 3
"""短文本阈值:归一后长度不足该值的文本不跑 n-gram,退化为精确/包含判断。"""


def _exact_or_contains(a: str, b: str) -> float:
    """短文本退化判定:完全相同 1.0;一方包含另一方也算同一问法 1.0;否则 0。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_text = (a, b) if len(a) <= len(b) else (b, a)
    if short in long_text:
        return 1.0
    return 0.0


def _tfidf_cosine_scores(query: str, candidates: Sequence[str]) -> list[float]:
    """用字符 2/3-gram TF-IDF 余弦,批量比较 query 与候选。

    语料 = query + 候选( IDF 在这个语料上拟合);``TfidfVectorizer`` 默认
    ``norm="l2"``,所以矩阵行积即余弦相似度。短文本(长度 <
    ``MIN_SIMILARITY_LEN``)改用精确/包含判断。
    """
    scores: list[float] = [0.0] * len(candidates)
    long_idx: list[int] = []
    for index, candidate in enumerate(candidates):
        if len(query) < MIN_SIMILARITY_LEN or len(candidate) < MIN_SIMILARITY_LEN:
            scores[index] = _exact_or_contains(query, candidate)
        else:
            long_idx.append(index)
    if not long_idx:
        return scores
    corpus = [query] + [candidates[index] for index in long_idx]
    try:
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3))
        matrix = vectorizer.fit_transform(corpus)
    except ValueError:
        # 语料里全是空串/无 n-gram → 退化为精确/包含判断,不让缓存崩掉。
        for index in long_idx:
            scores[index] = _exact_or_contains(query, candidates[index])
        return scores
    query_vector = matrix[0]
    sims = (matrix[1:] @ query_vector.T).toarray().ravel()
    for offset, index in enumerate(long_idx):
        scores[index] = float(sims[offset])
    return scores


# ---------------------------------------------------------------------------
# 缓存条目与缓存本体
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    """一条缓存:一次问答的完整结果(回复 + 引用 + 意图信息)。"""

    reply: str = ""
    citations: list[dict] = field(default_factory=list)
    intent: str = ""
    confidence: float = 0.0
    intent_reason: str = ""
    created_at: float = 0.0
    """入库时刻(``time.time()`` 秒)。"""

    def age_seconds(self) -> float:
        """这条缓存已经存在多久(秒);不小于 0。"""
        return max(0.0, _now() - self.created_at)


@dataclass
class _Record:
    """内部存储记录:归一后的查询串 + 缓存条目。"""

    normalized: str
    entry: CacheEntry


def _now() -> float:
    """当前时间(秒)。测试可 monkeypatch 本模块的 ``time.time``。"""
    return time.time()


class SemanticCache:
    """语义缓存:归一 + TF-IDF 字符 n-gram 余弦的问答复用层。

    用法::

        cache = SemanticCache()
        hit = cache.lookup("退貨期限要多久")
        if hit is None:
            ...  # 跑图,拿到结果
            cache.store("退货期限是几天", payload)
    """

    def __init__(
        self,
        *,
        threshold: float = 0.92,
        ttl_seconds: int = 1800,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._threshold = float(threshold)
        self._ttl = float(ttl_seconds)
        self._clock: Callable[[], float] = clock if clock is not None else _now
        self._records: dict[str, _Record] = {}
        self._hits = 0
        self._misses = 0
        self._stores = 0
        self._lock = threading.RLock()

    # -- 内部 ------------------------------------------------------------
    @property
    def threshold(self) -> float:
        """命中阈值(余弦下界)。"""
        return self._threshold

    @property
    def ttl_seconds(self) -> float:
        """条目存活时长(秒)。"""
        return self._ttl

    def _purge_expired(self) -> None:
        """删掉过期条目(惰性清理)。"""
        now = self._clock()
        stale = [
            key
            for key, record in self._records.items()
            if now - record.entry.created_at > self._ttl
        ]
        for key in stale:
            self._records.pop(key, None)

    # -- 对外 ------------------------------------------------------------
    def is_cacheable(self, text: str, *, intent: str | None = None) -> bool:
        """该请求是否允许进缓存。

        三类永不缓存:①写操作意图;②含订单号;③含 PII 占位符/原文。
        ``intent`` 可选(``store`` 时从 payload 带出):已知意图是写操作
        (``handoff``/``order_judge``/退款类)时直接不缓存。
        """
        body = str(text or "")
        if not body.strip():
            return False
        if _intent_is_write(intent):
            return False
        if _text_is_write_intent(body):
            return False
        if _has_order_number(body):
            return False
        if _has_pii(body):
            return False
        return True

    def lookup(self, text: str) -> CacheEntry | None:
        """查缓存。

        返回命中的条目(``reply`` + ``citations`` 等),调用方据此标
        ``cache_hit: true`` 并记 0 token;未命中/不可缓存/过期 → ``None``。
        """
        with self._lock:
            self._purge_expired()
            if not self.is_cacheable(text):
                # 不可缓存的请求不进缓存,也不查库(计一次未命中)。
                self._misses += 1
                return None
            query = _normalize(text)
            if not query:
                self._misses += 1
                return None
            keys = list(self._records.keys())
            candidates = [self._records[key].normalized for key in keys]
            scores = _tfidf_cosine_scores(query, candidates)
            best_index = -1
            best_score = 0.0
            for index, score in enumerate(scores):
                if score > best_score:
                    best_score = score
                    best_index = index
            if best_index < 0 or best_score < self._threshold:
                self._misses += 1
                return None
            self._hits += 1
            source = self._records[keys[best_index]].entry
            # 返回副本:避免调用方改到缓存里的 citations。
            return CacheEntry(
                reply=source.reply,
                citations=[dict(item) for item in source.citations],
                intent=source.intent,
                confidence=source.confidence,
                intent_reason=source.intent_reason,
                created_at=source.created_at,
            )

    def store(self, text: str, payload: Mapping[str, Any]) -> bool:
        """写入缓存。

        ``payload`` 至少含 ``reply`` / ``citations`` / ``intent`` /
        ``confidence`` / ``intent_reason``;``citations`` 是 ``list[dict]``。
        不可缓存 → 返回 ``False`` 且不入库;成功 → ``True``。
        """
        with self._lock:
            self._purge_expired()
            data = dict(payload or {})
            intent = data.get("intent")
            if not self.is_cacheable(text, intent=intent if isinstance(intent, str) else None):
                return False
            normalized = _normalize(text)
            if not normalized:
                return False
            reply = str(data.get("reply") or "")
            if not reply.strip():
                return False
            raw_citations = data.get("citations") or []
            citations: list[dict] = []
            for item in raw_citations:
                if isinstance(item, Mapping):
                    citations.append(dict(item))
                    continue
                try:
                    citations.append(dict(item))
                except (TypeError, ValueError):
                    # 非字典型引用(如自定义对象)无法安全转成 dict:跳过而不是崩。
                    continue
            try:
                confidence = float(data.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            entry = CacheEntry(
                reply=reply,
                citations=citations,
                intent=str(intent or ""),
                confidence=confidence,
                intent_reason=str(data.get("intent_reason") or ""),
                created_at=self._clock(),
            )
            self._records[normalized] = _Record(normalized=normalized, entry=entry)
            self._stores += 1
            return True

    def clear(self) -> None:
        """清空所有缓存条目(手动失效;统计计数保留)。"""
        with self._lock:
            self._records.clear()

    def stats(self) -> dict:
        """统计:``{"size": n, "hits": h, "misses": m, "stores": s}``。

        ``size`` 是当前**未过期**条目数;``hits``/``misses`` 是查询计数
        (不可缓存的查询也计一次 miss —— 它同样没有被缓存满足)。
        """
        with self._lock:
            self._purge_expired()
            return {
                "size": len(self._records),
                "hits": self._hits,
                "misses": self._misses,
                "stores": self._stores,
            }


#: 模块级单例:调用方直接用它,不要各处 ``new``(否则缓存不共享)。
semantic_cache = SemanticCache()
