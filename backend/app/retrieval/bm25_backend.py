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

查询侧同义别名扩展(W7-3)不改变上述分布:``QUERY_ALIASES`` 只在 ``tokenize``
结果上**补充**口语别名的 canonical 词项,且按去重方式合入(``tokenize_query``);
原有 8 个定标问句 + 弱相关探针「今天天气怎么样」(共 9 条)的 token 列表与
BM25Plus 分数**逐位不变**(tests/test_aliases.py 复算断言),阈值与索引
(``build`` 仍只用 ``tokenize``)均未改动。

口语问句修复前后对比(``search(..., expand=False)`` 即修复前;分数为 BM25Plus,
threshold=20.0 下不足 20 记为 no_hit;数字可复算,见 tests/test_aliases.py):

- 衣服不想要了能退吗:修复前 no_hit(最高 RP-09 18.6451)→ 修复后 RP-01 25.0300;
- 衣服有瑕疵怎么办:修复前 no_hit(最高 RP-08 13.7841)→ 修复后 RP-03 22.3335;
- 码数不对想换一个:修复前 no_hit(最高 RP-05 14.5995)→ 修复后 RP-05 23.8224;
- 穿不了想换货:修复前 no_hit(最高 RP-05 15.7521)→ 修复后 RP-05 21.5417;
- 退我差价:修复前 no_hit(最高 RP-07 0.8598)→ 修复后 RP-07 25.4747;
- 衣服坏了能退换吗:修复前 RP-03 20.1540 → 修复后 RP-03 33.1946;
- 多久能到账:修复前 RP-06 22.3620 → 修复后 RP-06 22.3620(原词已覆盖,不变);
- 物流破损怎么处理:修复前 LP-04 37.2938 → 修复后 LP-04 37.2938(同上);
- 新疆包邮吗:修复前 LP-03 27.0399 → 修复后 LP-03 31.6860。
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

#: 查询侧同义别名表(W7-3):买家口语说法 → 条款语料里的 canonical 词项。
#:
#: 只加在**查询侧**:``build()`` 索引仍然只用 ``tokenize``,条款原文零改动。
#: canonical 词项一律选语料正文里真实出现的 unigram/bigram(可用 ``tokenize``
#: 复算),否则追加的词项不会命中任何文档,等于无效别名。
#:
#: 语料(``data/policies/*.md``)共 18 条:RP-01~RP-12 退换货、LP-01~LP-06 物流。
#: 「发票/开票」「少件漏发」「保修」在语料里没有对应条款,这里按最近的语料说法
#: 或如实保留 canonical 词(如"发票"),检索仍会因低于阈值而 no_hit —— 只扩展
#: 同义表达,不替政策"无中生有"。
QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    # 1) 退款/退差价(退钱/赔我 → 退款;差价 → 退款金额条款的金额/优惠词)
    "退钱": ("退款",),
    "退我": ("退款",),
    "差价": ("金额", "优惠", "满减", "支付"),
    "赔我": ("退款",),
    "赔钱": ("退款",),
    "退费": ("退款",),
    # 2) 质量问题(坏了/有问题/瑕疵/故障 → 质量、问题 + 条款里的具体形态)
    "坏了": ("质量", "问题", "破损", "故障"),
    "坏掉": ("质量", "问题", "破损", "故障"),
    "有问题": ("质量", "问题", "破损", "故障"),
    "瑕疵": ("质量", "问题", "破损", "故障"),
    "毛病": ("质量", "问题", "破损", "故障"),
    "故障": ("质量", "问题"),
    "质量差": ("质量", "问题"),
    # 3) 七天无理由(不想要了/后悔了 → 无理由 的 bigram + 退货)
    "不想要了": ("无理", "理由", "退货"),
    "不想要": ("无理", "理由", "退货"),
    "后悔了": ("无理", "理由", "退货"),
    "不喜欢": ("无理", "理由", "退货"),
    # 4) 物流破损(快递/包裹破了 → 破损、外包装、补发;"物流破"本身已命中,
    #    只补"破损"以保证既有定标问句 token 不变)
    "快递破": ("破损", "外包装", "补发"),
    "包裹破": ("破损", "外包装", "补发"),
    "压坏": ("破损", "外包装", "补发"),
    "外包装破": ("破损", "外包装", "补发"),
    "物流破": ("破损",),
    # 5) 尺码换货(尺码不对/穿不了 → 尺码;穿不了同时补"换货")
    "尺码不对": ("尺码",),
    "尺码不合适": ("尺码",),
    "码数不对": ("尺码",),
    "穿不了": ("尺码", "换货"),
    "穿不上": ("尺码", "换货"),
    "太小": ("尺码",),
    "太大": ("尺码",),
    # 6) 错发漏发(发错货/寄错 → 语料里的"发错/错发")
    "发错货": ("发错", "错发"),
    "发错了": ("发错", "错发"),
    "寄错": ("发错", "错发"),
    "发错": ("发错", "错发"),
    # 7) 少件漏发(少发/没收到 → 语料里只有"发货"可对齐)
    "少发": ("发货",),
    "少件": ("发货",),
    "漏发": ("发货",),
    "没收到": ("发货", "物流"),
    # 8) 发票/开票:语料无发票条款,canonical 如实保留,不硬凑到别的条款
    "发票": ("发票",),
    "开票": ("发票",),
    # 9) 换货(换个/换新的 → 换货)
    "换个": ("换货",),
    "换一个": ("换货",),
    "换新的": ("换货",),
    "能换吗": ("换货",),
    # 10) 保修/维修(修/维修 → 语料里最接近的"故障"= 质量问题)
    "维修": ("故障",),
    "保修": ("故障",),
    "能修": ("故障",),
    "修好": ("故障",),
    # 11) 时效(催/多久到 → 到账 / 发货时效)
    "多久到": ("到账",),
    "什么时候到": ("到账",),
    "催": ("时效", "现货", "48"),
    "发货慢": ("时效", "现货", "48"),
    "还不发货": ("时效", "现货", "48"),
    # 12) 投诉/举报(→ 语料里的"人工客服")
    "投诉": ("人工", "客服"),
    "举报": ("人工", "客服"),
    "曝光": ("人工", "客服"),
    # 13) 包邮/运费(免邮/邮费 → 包邮门槛条款的包邮、99、实付)
    "免邮": ("包邮", "99", "实付"),
    "邮费": ("运费", "包邮"),
    # 14) 偏远地区(新疆/西藏等 → 偏远)
    "新疆": ("偏远",),
    "西藏": ("偏远",),
    "内蒙古": ("偏远",),
    "青海": ("偏远",),
    "宁夏": ("偏远",),
}


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


def tokenize_query(text: str, *, expand: bool = True) -> list[str]:
    """查询侧分词 = ``tokenize`` + 口语别名的 canonical 词项(W7-3)。

    - 别名按子串命中 ``QUERY_ALIASES`` 的 key(如问句含"坏了"→ 追加"质量""问题");
    - **只补充 tokenize 结果里还没有的词项**(去重):原词已经命中的问句(如
      "质量问题..." 本身含"质量""问题")不会因扩展而改变 token 列表,因此 8 个
      既有定标问句的 BM25 分数与阈值分布**逐位不变**(tests/test_aliases.py 复算);
    - ``expand=False`` 退化为纯 ``tokenize``,供测试测量"修复前"分数。
    """
    tokens = tokenize(text)
    if not expand:
        return tokens
    seen = set(tokens)
    for alias, canonicals in QUERY_ALIASES.items():
        if alias not in text:
            continue
        for canonical in canonicals:
            if canonical not in seen:
                tokens.append(canonical)
                seen.add(canonical)
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
        *,
        expand: bool = True,
    ) -> SearchResult:
        """BM25 检索,返回 SearchResult(results + no_hit)。

        - 无索引 / 空查询 / 无有效 token → 空结果 + no_hit=True。
        - 只保留与查询至少共享一个词项的文档(BM25Plus 的 δ 下限项会让零重叠
          文档拿到正分,必须先做词项重叠过滤,再按分数降序截断)。
        - 分数低于 threshold 的命中被丢弃;最终 results 为空则 no_hit=True。
        - ``expand=True``(默认)走 ``tokenize_query``(W7-3 查询侧别名扩展);
          ``expand=False`` 只用 ``tokenize``,供测试复算"修复前"分数。
        """
        if self._bm25 is None or not query.strip():
            return SearchResult(results=[], no_hit=True)

        q_tokens = tokenize_query(query, expand=expand)
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
