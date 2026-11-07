# 任务书 W2-A:政策知识库——语料 + 分块 + BM25 检索(带 clause_id)

> 先读 `docs/任务书/00_通用约定.md`。前置:W1-A 验收(knowledge_item 表)。

## 目标

店铺政策成为可检索、可引用、版本化的知识源:检索结果永远带 `clause_id + version`,低于阈值显式返回"无命中",不编。v1 只做 BM25(D-02),向量后端留接口。

## 只需读这几段(只读)

1. `docs/PRD_v1.md` §5(记忆三处放:店铺规则进知识库)、D-02
2. 03 文档第 604-705 行(§4.2 五层契约:检索层的输入/输出/失败语义)
3. 正例(结构参考,不装它的包):`私有化AI助手\backend\services\bm25_service.py` 与 `chunking_service.py`(各读前 100 行即可,注意它的缺陷:无 rerank 阈值、启动不重建索引——你要反过来做)

## 产物文件(白名单)

```
data/policies/return_policy.md      #  mock 店铺政策,≥12 条条款,每条带稳定条款号(如 RP-07天无理由-01)、生效日期、版本号 v1;内容覆盖:7天无理由/15天质量/运费承担/尺码换货/退款时效/不予退货情形
data/policies/logistics_policy.md   # ≥6 条条款,同上格式
backend/app/retrieval/__init__.py
backend/app/retrieval/chunker.py    # 按条款分块:每块={clause_id, version, title, text};条款号从 md 结构解析,不手写映射
backend/app/retrieval/bm25_backend.py  # rank-bm25 索引;启动无条件重建;search(query, top_k, threshold) -> [RetrievedChunk(clause_id, version, text, score)]
backend/app/retrieval/base.py       # VectorBackend 抽象接口(扩展点,只有接口+NotImplementedError 注释说明,回执标注这是"声明的扩展点"不算假实现)
backend/scripts/load_policies.py    # 解析 md → upsert knowledge_item 表(带 version/embedding_model=NULL)+ 重建 BM25 索引;幂等
backend/tests/test_retrieval.py     # ≥10 个测试
```

## 实现要点

1. 分块以条款为最小单位;"7 天无理由能退吗"必须召回 RP 对应条款且 top1 正确
2. `search` 返回低于 threshold 时为空列表并带 `no_hit:true` 元信息——上层据此明说"政策未覆盖",禁止硬凑
3. threshold 定标:写一个小脚本用 ≥8 个真实问句(含 2 个政策没覆盖的问题)测出分数分布,把选值和依据写进 `bm25_backend.py` 模块 docstring(数字可复算)
4. knowledge_item 每次 upsert 版本号递增策略写进 load_policies.py docstring

## 验收标准

1. pytest 全绿,含:条款解析数=md 里条款数、top1 命中正确条款、无覆盖问题返回 no_hit、load 脚本跑两遍 knowledge_item 行数不变
2. 贴 8 个定标问句的检索结果原文(clause_id+score)
3. `grep -rn "TODO\|FIXME" backend/app/retrieval` 为空(base.py 的扩展点注释除外,需解释)

## 禁止

- 不接 LLM、不装 faiss/chromadb;不写工具层(W2-B 才把检索包成 policy__search)
