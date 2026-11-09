# 任务书 W8-FIX:MEMORY-001 微修 + 单案真实复验(用户裁决+授权 ≈0.03 元)

> 先读 `docs/任务书/00_通用约定.md`(§6/§7)。基线:W8 验收后的 HEAD(读 git log 确认)。
> 背景:第三轮真实评测残余簇 CS-MEMORY-001(3/3 fact_mismatch,回复未逐字含"尺码")。FIX-9 的确定性拼接 gate 在"意图与 scope 相关"上,真实模型意图漂移(policy_qa 等)时不触发。用户裁决:微修 + 单案真实复验。

## 微修(确定性,通用机制,禁 case 特判)

- `nodes.py` compose 记忆拼接 gate 改为:**State.memory 注入了 preference 且 用户文本或本轮回执文本命中该记忆 scope 的关键词表**(guardrails 常量:scope→关键词映射,如 size_recommendation→尺码/尺寸/大小/合身/偏紧/偏大/偏小)即拼接引用句,与 intent 标签解耦。
- 关键词表进 guardrails.py 常量区;拼接句式沿用 FIX-9。
- 测试:test_graph.py 增 2 例——①FakeLLM 故意把意图判成 policy_qa + 用户文本含"尺码" → 拼接仍触发;②用户文本与 scope 无关(问运费) → 不拼接(防过度触发)。

## 单案真实复验(唯一允许的真实 API 调用,预算 3 attempt)

- `run_eval.py` 增加 `--case-id <ID>` 过滤参数(白名单内小改,--repeat 照旧)。
- 命令:`run_eval.py --real --case-id CS-MEMORY-001 --repeat 3`(deepseek-flash 主模型;.env 主 key)。
- 通过标准:3/3 hard 通过(must_contain"尺码"命中);若 2/3 → 如实回执,主会话裁决是否接受散发;若 ≤1/3 → 回执失败证据,不许再跑。
- 复验报告 JSON 落 data/eval/reports/(标注 micro-verify)。

## 白名单

```
backend/app/graph/nodes.py(gate 改)
backend/app/guardrails.py(scope 关键词表)
backend/tests/test_graph.py(+2)
backend/scripts/run_eval.py(--case-id)
```

## 回归红线

1. 全量 pytest 基线+新增全绿(Fake 密封,除上述 3 attempt 外零真实 API)
2. check_w1 10/10;smoke 不倒退
3. 回执:gate 改前后代码对照、两新测试输出、复验报告 metrics 原文(pass_rate/critical/tokens/cost)
