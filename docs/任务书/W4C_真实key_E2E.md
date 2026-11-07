# 任务书 W4-C:真实模型 E2E(阻塞项:等用户提供 API key)

> 先读 `docs/任务书/00_通用约定.md`。前置:W4-B 已验收 + 用户已提供 LLM API key(DeepSeek 或 OpenAI 兼容)。key 只进 `ROOT/.env`(已 gitignore),绝不进代码/日志/回执/评测报告。

## 目标

S5+S6:真实模型跑 48 条 × n=3 出正式报告;真实 E2E 主流程+错误分支有原始响应证据;三档判定给出"达到/部分达到/未达到"结论。

## 产物文件(白名单)

```
backend/app/llm/real_client.py     # LLMClient 真实实现(httpx,OpenAI 兼容 /chat/completions;base_url/model/key 全从 .env 读;超时+重试≤2 退避;usage 从响应取真实 token)
backend/app/llm/__init__.py        # 允许改:按 .env 有无 key 自动选 Real/Fake(无 key 仍全绿=D-05)
backend/app/eval/judge.py          # 允许改:FakeJudge→真实裁判(独立模型或同模型低温;仍保留确定性兜底 scored_by=heuristic)
.env.example                       # 字段名+注释,无真实值
data/eval/reports/<run_id>.json/md # 正式报告(真实 usage 计价:cost_per_case_usd 按 provider 价目表,价目写进报告 meta)
docs/验收报告_W4C.md                # 允许写:S5 六指标+三档判定结论、S6 真实 E2E 记录(主流程+2 个错误分支的原始响应摘录,key 打码)、人工抽检 10%(5 条)记录、judge_agreement 计算过程、遗留风险清单
```

## 验收标准

1. 无 key 时 pytest 仍全绿(自动降级 Fake),贴证据;有 key 时 `run_eval.bat` 完整跑 48×3,贴正式报告 JSON
2. 六指标:硬断言 100%、整体≥0.85(冲刺 0.9)、critical_fail=0、agreement≥0.8、cost/p95 有真实数字——未达线照实写"未达到"+失败成簇分析,不许稀释
3. S6:真实模型跑三场景各 1 条 E2E(含一次确认卡、一次转人工),原始请求/响应摘录进验收报告(key/authorization 打码)
4. 回滚方法演练记录在验收报告里
5. `grep -rn` 证明全仓无 key 明文(只 .env,且 .env 不在 git)

## 禁止

- key 不进任何产物/日志/回执;裁判与被判模型若同源需在报告声明偏差风险;不许用裁判耗时冒充时延
