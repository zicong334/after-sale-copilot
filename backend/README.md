# 售后 Copilot — 后端交付说明

单店电商售后 Copilot:买家侧一个聊天入口 + 客服侧工单接管。FastAPI + SQLite(WAL) + LangGraph 六节点;
**不配置任何 API key 也能起服务、也能跑评测**(FakeLLM + FakeJudge,见「MOCK 声明清单」)。
唯一的目标是「可演示 + 可评测 + 可回滚」,范围按 `docs/PRD_v1.md` §1/§3 严格收口。

---

## 1. MVP 五条件对照

| # | PRD 五条件(原文) | 本仓库落点 | 证据(路径:行号) |
|---|---|---|---|
| 1 | **单店铺**:一个 merchant;但每张业务表带 `merchant_id`,所有查询从第一天就带租户条件 | 7 张主体表直带 `merchant_id`(product / review / chat_session / knowledge_item / order / refund_request / ticket);5 张纯从属表(product_variant / product_metric / chat_message / order_item / ticket_message)经主表推导、`eval_case` 是项目级资产(各模型注释写明理由);查询/写操作带租户过滤,越权返回 `AUTHZ_DENIED`。**与 PRD 「每张业务表」字面口径有偏差,见 §8 已知声明 3** | `backend/app/models/ticket.py:30`、`backend/app/models/catalog.py:55`、`backend/app/tools/shop.py:80`(越权) |
| 2 | **单任务类型**:售后(政策问答/订单判责/转人工三场景共用同一套状态机与工具) | 工具注册表只有 5 个售后工具,无售前/营销/生图能力 | `backend/app/tools/registry.py:101`、`backend/app/tools/shop.py:24` |
| 3 | **端到端**:买家输入 → 感知 → 意图 → 检索/工具 → 判定 →(确认)→ 写操作/回复 → 回执 | `/api/c/chat`(含 SSE)一次请求走完感知 → 六节点图 → 工具 → 落库 → 回执 | `backend/app/api/chat.py:95`、`backend/app/graph/build.py:96-111` |
| 4 | **有回执**:业务单号 + status + UTC 时间 + trace_id;verified 靠回读 | 写操作结果含 `refund_id`/`ticket_id` + `status` + `idempotency_key`;确认接口回读 DB 组装 `verified` 回执 | `backend/app/tools/refund.py:73`、`backend/app/api/confirm.py:209`、`backend/app/schemas/receipt.py:4` |
| 5 | **可复跑**:一条命令起服务、一条命令跑评测;评测 n=3、数字可重算 | `serve.bat` / `run_eval.bat`;评测 48×3,README 数字由脚本重算 | 本文件 §5、§6 |

## 2. 三场景(入口 / 出口 / 核心断言)

| 场景 | 入口 | 出口 | 核心断言 |
|---|---|---|---|
| 政策问答 | 买家问退换货期限/运费/尺码 | 答案 + `[n]` 条款号引用(可点回原文) | 引用真实存在;无命中明说,不编造 |
| 订单判责 | 订单号 + 诉求(退货/换货/仅退款) | 查订单 → 查政策 → 判定;符合 → **创建退款申请单(需人工确认)**;不符合 → 解释 + 替代方案 | 判定与金标准一致;不符合时零写操作;金额 > 实付直接拦 |
| 转人工 | 金额争议/投诉/连续 3 轮未解决/超轮次 | 建工单(带摘要)+ 会话置 `handed_off` + **AI 停止回复** + 人工领单守卫 | 工单真实创建;转人工后 AI 不再答;领单并发只成功一个 |

## 3. 架构图(文字版)

```
         买家(frontend/chat.html, SSE)                 客服(待做 UI,本期无)
                    │                                        │
                    └──────────────┬─────────────────────────┘
                                   ▼
                 FastAPI(backend/app/main.py)
                 /health · /api/c/chat(SSE)· /api/c/confirm/* · /api/c/handoff
                 /orders · /tickets · /refunds · /v1/maintenance
                                   ▼
   感知层 backend/app/perception        Pydantic 归一化(订单号/金额/诉求;失败=显式错误)
                                   ▼
   编排层 backend/app/graph             LangGraph 六节点 + SqliteSaver checkpointer
   perceive(纯函数) → classify → retrieve → act(自环,steps>8 强制 handoff)
                                              ↘ confirm 中断(先确认后执行,恢复不重放)
   handoff → compose(LLM 文案)
                                   ▼
   工具层 backend/app/tools            九字段注册表 + 5 工具(三层校验:registry/service/policy)
   read : shop__get_order / policy__search / policy__check_return
   write: refund__create / handoff__create_ticket(均 requires_confirm,落 confirm_request 确认卡)
                                   ▼
   数据层 SQLite WAL(backend/dev.db) 17 张业务表 = PRD 14 张 + tool_events/confirm_request(0002)
                                     + memory(0004);迁移 0001→0004
                                   ▼
   评测层 backend/app/eval             硬断言(FAIL_HARD/FAIL_CRITICAL)→ FakeJudge 三维 rubric → 6 指标报告
```

## 4. 快速开始

前提:Windows(或 Git Bash)、项目根已就绪,`.venv` 按 `requirements.lock.txt` 装好;**不需要任何 key**。

```bat
:: ① 一键起服务(迁移 → 空库自动 seed → uvicorn;Ctrl+C 停止)
serve.bat
:: 浏览器打开 http://127.0.0.1:8000/health 应返回 {"ok":true,"status":"healthy"};聊天页 http://127.0.0.1:8000/
```

```bat
:: ② 一键评测(48 条 × repeat=3,FakeLLM+FakeJudge,无 key)
run_eval.bat
:: 只想冒烟:run_eval.bat --smoke
```

手动等价命令(在项目根执行):

```bat
.venv\Scripts\python -m alembic -c backend\alembic.ini upgrade head
.venv\Scripts\python backend\scripts\seed.py
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000   :: 需 PYTHONPATH=backend
```

- 起服务的环境变量:`SERVE_HOST`(默认 `127.0.0.1`)、`SERVE_PORT`(默认 `8000`)、`DATABASE_URL`。
- 评测参数见 `backend/scripts/run_eval.py --help`(默认 `--repeat 3`,报告落 `data/eval/reports/`)。

## 5. 测试

```bat
:: 全量测试(263 个用例;--basetemp 每次换一个全新目录,不复用旧 .tmp)
set TMP=%CD%\.tmp
set TEMP=%CD%\.tmp
.venv\Scripts\python -m pytest backend/tests -q -p no:cacheprovider --basetemp=.tmp\pytest_%RANDOM%
```

## 6. 测试与评测数字(脚本生成,勿手改)

**生成命令**(在项目根执行,输出即下一段;`backend/tests/test_delivery.py::test_readme_numbers_match_script` 会逐字比对):

```bat
set PYTHONPATH=%CD%\backend
.venv\Scripts\python backend\scripts\gen_readme_numbers.py
```

<!-- gen_readme_numbers:begin -->
<!-- 本段由 `python backend/scripts/gen_readme_numbers.py` 自动生成,勿手改;
     数字来源:pytest --collect-only 收集数 + data/eval/reports/ 全量 FakeLLM 基线报告。 -->
- 测试:334 个 pytest 用例(收集数;全量运行命令见「快速开始」)
- 评测集:48 条 × n=3 = 144 次尝试(adversarial 14 / edge 10 / golden 19 / regression 5)
- FakeLLM 基线通过率:141/144 = 0.9792(edge 层 27/30 = 0.9000)
- critical 失败:0
- 裁判一致率:141/144 = 0.9792
- p95 用户等待:1162.0 ms
- 单条成本(估算):$0.00007293;整轮合计 $0.0105021
- 基线报告:`data/eval/reports/w4a-20261107T164830Z-7b5b3c.json`
<!-- gen_readme_numbers:end -->

口径说明:以上是 **FakeLLM + FakeJudge 的 Mock 基线**(`meta.mode="mock"`),**不代表真模型能力**;
报告里 `cost_per_case_usd` 是按 MOCK 价目从字符数估算的,不是真实账单(W4-C 换真 key 后才有真实成本)。

## 7. MOCK 声明清单

> 原则:mock 本身不算错,**把 mock 声称成真实实现才算错**(03§4.4 补充)。以下实现全部在代码注释里标注 `MOCK`。

| 位置 | MOCK 内容 | 对结论的影响 |
|---|---|---|
| `backend/app/llm/base.py:115` | `FakeLLM`:关键词规则分类 + 模板回复,无网络、无模型 | 评测=Mock 基线,不是真模型能力;`model_name` 返回 `fake-rule-v1 (MOCK)` |
| `backend/app/eval/judge.py:110` | `FakeJudge`:确定性规则打分,`scored_by=heuristic` | 裁判一致率只能证明判定链路自洽;W4-C 用真裁判 |
| `backend/app/api/chat.py:78` | `usage` 计量:按字符数估 token × MOCK 价目,`estimated: true` | 前端显示的 token/成本是估算值,不是账单 |
| `backend/app/eval/engine.py:57` | 评测 `cost_per_case`:与 chat 完全一致的 MOCK 口径 | 报告 `cost_per_case_usd` 是估算 |
| `backend/app/graph/nodes.py:101` | 无命中 query rewrite:关键词映射 | 检索改写能力弱于 LLM rewrite |
| `backend/app/memory/store.py:176` | 任务结束记忆抽取:规则回退(FakeLLM 不支持抽取 schema) | 抽取出的是规则候选,不是模型抽取 |
| `backend/app/orchestrator/w1_router.py:3` | W2-C 的「MOCK 直调」编排基线 | **已不在主链路**(main/chat 走 `app.graph.runner`),保留为对照 |

### 7.1 确定性护栏清单(W4-C-FIX)

> 原则:每项 FIX 的验收对象是**代码守卫**(确定性、可单测);prompt 约束只是辅助。
> 守卫常量与纯函数全部集中在 `backend/app/guardrails.py`(单一可审计入口),节点/工具只做接线。

| FIX | 失败簇 | 守卫位置 | 确定性语义 |
|---|---|---|---|
| FIX-1 订单核验强制链 | authorization 6 / REFUND-006 | `guardrails.needs_order_verification` + `nodes.act` | 文本/上下文有订单号且意图非政策问答、本轮尚未调 `shop__get_order` → 编排层强制注入一次;无订单号则先追问,不发工具 |
| FIX-2 退款前置策略门 | forbidden_tool | `tools/refund.py` handler | 同 trace 无 `policy__check_return` 的 eligible 证据(按入参纯函数重算)→ `BUSINESS_REJECT`,零写入 |
| FIX-3 注入感知 | EDGE-008 / ADV-001 等 | `guardrails.INJECTION_RULES` + `perception.Percept.flags` + `nodes.classify/act` + `memory` 隔离 | 技术型注入(SQL/命令/伪造工具调用)硬拦截零工具;自然语言越权剥离后只处理正当诉求;注入文本不生成可执行记忆 |
| FIX-4 反编造 | S6 确认前称已转接 | `guardrails.audit_status_claims` + `nodes.compose` | 回复里的状态声称(已转接/已退款/已提交/查不到…)必须在本轮 trace/DB 有证据,否则逐句替换为"我尚未…";转人工确认前统一"将为您转接人工(待确认)" |
| FIX-5 引用保真 | fact_mismatch 7 | `guardrails.citation_phrases/ensure_citation_fidelity` + `nodes.compose` | 条款关键数字短语(如"满 99 元包邮""8 元基础运费")未逐字出现 → 确定性追加条款原文;政策问答必含"政策"字样 |
| FIX-6 转人工原因枚举 | handoff_reason 7 | `guardrails.HANDOFF_REASON_ENUM/map_handoff_reason` + `tools/handoff.py` schema | `reason` 只接受七枚举,入库为枚举;summary 保留自然语言;评测期望值同步 `cs-eval-v1-20261108b` |
| FIX-7 记忆场景意图 | MEMORY-001 | `llm/base.py` 规则 + `nodes.classify` few-shot + `nodes.compose` memory | 尺码/偏好/以后推荐类陈述判 `other`,回复引用记忆注入正文;不误判政策问答、不误调退款工具 |

护栏单测:`backend/tests/test_guardrails.py`(纯函数);图级回归:`backend/tests/test_graph.py` 第 6 节(`fix1/fix3/fix4/fix5/fix7`)。

## 8. 已知声明(交接必读)

1. **Ticket 无 session_id**:`ticket` 表只有 `merchant_id` / `customer_id` / `refund_request_id` 等列,没有 `session_id`;
   转人工摘要按 `merchant_id + customer_id` 关联上下文生成,不按会话精确回溯(PRD 未要求,列为扩展点)。
   证据:`backend/app/models/ticket.py:30-34`。
2. **usage 为 MOCK 估算**:本轮 `usage.tokens_in/tokens_out/cost_usd` 是按字符数 × MOCK 价目的**估算**
   (`estimated: true`),**不是真实 API 账单**;真实计量待 W4-C 接真实 key。
   证据:`backend/app/api/chat.py:78-91`。
3. **与 PRD §1 条件 1 的偏差(主动汇报)**:PRD 原文要求「每张业务表带 `merchant_id`」;实际 14 张业务表中
   7 张主体表直带该列,5 张纯从属表经主表推导(理由写在模型注释:
   `backend/app/models/catalog.py:55`、`catalog.py:83`、`chat.py:50`、`orders.py:60`、`ticket.py:75`),
   `eval_case` 为项目级资产(`backend/app/models/eval.py:21`),`merchant` 本身是租户根。
   租户隔离的实际效果不受影响(查询/写操作都带租户条件,越权有 `AUTHZ_DENIED` 测试);
   若验收要求严格照字面「每张表都有列」,需补一条迁移并把从属表的写入路径改为显式传租户,
   属结构性改动,本棒(交付棒)白名单外,列为 W4-C/后续候选。
   证据:`grep -n "merchant_id" backend/app/models/*.py`(上面 5 条注释 + 7 个列定义)。

## 9. 不做清单(照 PRD §3,每项标注将来从哪长出来)

| 拒绝项 | 将来从哪长出来 |
|---|---|
| 多租户 SaaS、计费 | 表里的 `merchant_id` 列(查询已带条件,不用回填) |
| 自动退款、自动发券、自动改价 | 工具注册表 `pricing.*` / `coupon.*` 命名空间(只预留,不实现);退款永远只是「创建申请单 + 人工审核」 |
| 售前营销、生图、文案生成、B 端 7 Tab | 不留扩展点(违反单任务类型) |
| 完整 i18n、多币种结算 | `currency` 列 + 话术资源文件(中文一套 + 英文资源文件) |
| 分布式、消息队列、微服务 | 不留(SQLite WAL 单机是显式选择) |
| 自研编排框架 | 不留(LangGraph 一张图) |
| 向量检索 / FAISS | `retrieval` 接口 + `knowledge_item.embedding_model` / `dim` 列 |
| 附件独立表 | `chat_message` 存 URL(禁 base64) |
| 可视化评测大盘 | 评测报告 JSON(6 指标)是唯一口径 |

## 10. 扩展点清单

| 扩展点 | 现状(已预留的东西) | 接法 |
|---|---|---|
| 多租户 | 所有业务表 `merchant_id` + 查询带租户条件 | 接入鉴权后把「当前 merchant」从请求上下文注入,不改表 |
| 向量检索 | `retrieval` 模块接口 + `knowledge_item.embedding_model/dim` 列 | 在 `policy__search` 里换/加一个 retriever 实现,BM25 保留为兜底 |
| 新工具 | 九字段注册表(`backend/app/tools/registry.py:101`)+ `pricing.*`/`coupon.*` 命名空间 | 新增 `ToolSpec` + handler 并 `register()`,写工具必须 `requires_confirm=True` + `idempotency_key` |
| 多币种 / i18n | `currency` 列 + 话术资源文件 | 金额仍存整数分,展示层换算;文案从资源文件取 |
| 真实 LLM / 裁判 | `LLMClient` 抽象 + `FakeLLM` / `FakeJudge` 可注入(W4-C 换实现) | 实现 `LLMClient` 子类注入 `runner` / `engine`,不动图与断言 |
| 评测可视化 | 报告 JSON 6 指标(`data/eval/reports/`) | 直接读 JSON 渲染,不改变口径 |

## 11. 回滚(数据库)

`backend/scripts/rollback.py` 把「备份 → Alembic downgrade → 三项校验」固定成一个可重复动作:

```bat
:: 备份当前库并回滚到 0003(0004_memory 之前)
.venv\Scripts\python backend\scripts\rollback.py --target 0003

:: 再升级回 head(校验 integrity_check / foreign_key_check / alembic_version)
.venv\Scripts\python backend\scripts\rollback.py --upgrade

:: 查看版本链 / 指定库文件
.venv\Scripts\python backend\scripts\rollback.py --list
.venv\Scripts\python backend\scripts\rollback.py --target 0003 --db-url sqlite:///backend/dev.db
```

备份默认落 `.tmp/rollback/`(SQLite 官方 backup API,含 WAL 内容);脚本不会自动删备份。
注意:`--upgrade` 不会恢复被 downgrade 删掉的表数据,需要旧数据时从备份文件恢复。

## 12. 目录速览

```
backend/
├── app/            main.py / db.py / models/ / api/ / services/ / tools/ / graph/ /
│                   memory/ / retrieval/ / eval/ / perception / schemas/ / trace
├── alembic/        0001_initial → 0002_add_tool_events → 0003_ticket_summary → 0004_memory
├── scripts/        seed.py / load_eval_cases.py / run_eval.py / gen_readme_numbers.py / rollback.py
├── tests/          263 个用例(含 test_delivery.py 交付验收)
└── README.md       本文件
```
