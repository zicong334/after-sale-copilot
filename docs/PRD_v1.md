# 售后 Copilot PRD v1

> 版本:v1(2026-11-07)| 写入方:主会话(唯一) | 依据:stage7 Capstone 设计(G1-G9)+ 03§4/§5 + 风险契约六项检查单
> 本文件是项目唯一产品事实源。任务书与代码与本文件冲突时,以本文件为准;修改本文件必须在 §0 决策记录追加一行。

## 0. 决策记录(拍板留痕)

| # | 决策 | 结论 | 理由 |
|---|---|---|---|
| D-01 | 是否用 browser-use | **不用**。技术栈 = FastAPI + SQLAlchemy 2.0 + Alembic + SQLite(WAL) + LangGraph 一张六节点图 + rank-bm25 | 售后域没有浏览器自动化需求;browser-use 只增加不可复现性(03§5.3 的"做"清单里也没有它) |
| D-02 | 检索方案 | **v1 只做 BM25 + 条款号引用**;FAISS/向量检索留扩展点(`retrieval/vector_backend` 接口),不实现 | 降低依赖风险(faiss-cpu wheel 对 Python 版本敏感);政策文档量小,BM25+clause_id 已满足"答案带来源"的验收线;禁止 mock 冒充向量检索 |
| D-03 | Python 环境 | **Python 3.13.15,项目根 `.venv` 锁死**;依赖以 `requirements.lock.txt`(pip freeze)为准 | 本机 3.14/3.13 双版本共存,pip 与 python 不同版本是已暴露风险(记忆系统设计 pyproject 版本帽教训);任何会话不得用全局 python 装包 |
| D-04 | 排期模型 | **不绑日历周**。按 W1→W4 阶段门推进,每阶段拆成"单产物 dsh 会话";阶段末由主会话按任务书验收标准验收,通过才进下一阶段 | 执行者是 dsh 会话而非人天;保留四周结构的验收门与产出定义,去掉日历约束 |
| D-05 | LLM 接入 | **W1-W3 全程 FakeLLM/mock,无 key 全绿**;真实模型只在 W4-C 接入(LLMClient 抽象,DeepSeek 或 OpenAI 兼容,`.env` 不进 git) | G9 铁律:先冻结数据/契约,失败才能归因到层而不是采样运气;真实 key 待用户提供 |
| D-06 | git 与台账 | git 仓库初始化在项目根;**只有主会话 commit 和写 progress.md**;dsh 会话不碰 git,回执里给产物清单 | 单一写入方(G9);避免并发会话写坏台账 |
| D-07 | dsh 派发模型策略 | **既定策略=harness 默认(deepseek-flash/high),用户 2026-11-07 拍板**。此后派发不带 model 参数;若主会话评估某任务书复杂度超出 flash 能力上限,挂起并按规则汇报评估原因+建议模型,人工授权后才可升舱,严禁隐式取消重发(用户级记忆 `no-implicit-dispatch-changes`)。规则生效前的 W1-A(重发)/W1-B/W1-C/W2-A/W2-B 用了 v4-pro/high,属既成事实,产物已验收的不回炉 | 立规背景:W1-A 首发 flash 被主会话擅自取消改派 v4-pro,用户定性为违规 |
| D-08 | W4-C 真实模型 | key 由用户 2026-11-07 提供(只存 ROOT/.env,git 外);**模型锁定 deepseek-flash**(用户约束"只能用 DeepSeek-V4.1-Flash",API 实测正确 ID=deepseek-flash,该产品名无效);reasoning 模型,正文预算≥1024,空正文显式失败 | 主会话连通性实测 HTTP 200 |
| D-09 | 5xx 重试口径分层 | §4 第 2 项"5xx=退避重试≤2 次"**仅适用业务工具层**(有幂等键保证安全);**LLM 客户端在批量评测中 5xx/401/402/403/429 立即 BatchAbort**(烧钱纪律),单轮对话路径可按 transient 有限重试 | W4-C 报告 §10 risk12 披露的 PRD:56 与实现冲突,裁决为分层而非矛盾 |
| D-10 | 修复周期与重跑预算 | W4-C 第一轮判"未达到"(0.6944/critical 6)→ 启动 W4-C-FIX 确定性护栏包(七项,见任务书);修复只用 FakeLLM 验证,**修完后全量 48×3 真实重跑只许一次,由主会话亲自执行**(约 80 万 tokens,deepseek-flash 量级为个位数元,属用户"剩下的交给你了"授权范围);第二轮仍"未达到"→ 停线,完整分析上交用户裁决,不再自行循环烧钱 | 2026-11-08 00:3x 主会话依夜间授权决策,留痕待用户周一复核 |
| D-11 | 验收口径治理(真实 LLM 行为评测) | 用户 2026-11-09 选 C 拍板:"硬断言 100%"在真实模型采样下按类分层——**critical 类硬断言(资损/越权/隐私/确认门绕过)100% + 整体 pass_rate ≥0.9 + agreement ≥0.8 + critical_fail=0 + cost/时延真实计量(价目来源存档 data/eval/pricing_deepseek_20261109.md)+ 评测集/rubric 版本化 + 回滚可用 = "达到"**;非 critical 硬断言失败记已知缺口并成簇分析(同类 ≥3 必须修复或用户显式豁免)。按新口径第二轮(0.9236/critical 0/agreement 0.9722/无资损越权隐私失败)复判为"达到";用户同时选 B,第三轮小修+重跑照做,**最终判定以第三轮为准** | 采样非确定性与 100% 硬线的原理性张力(最终验收报告 §3);用户裁决 BC 组合 |
| D-12 | deepseek-flash 价目 | 输入(缓存未命中)空闲 1 元/高峰 2 元每百万;输入(命中)0.02/0.04;输出 4/8;周末及节假日全天空闲;reasoning_tokens 属输出子集不重复计费;行级 usage 未采缓存字段→成本按全未命中上界计,两轮实际 ≈2.53 元,第三轮预算 ≈2.5 元(高峰) | 用户 2026-11-09 提供官方价目,存档见 data/eval/pricing_deepseek_20261109.md |
| D-13 | 调度策略 v2 与夜间事故留痕 | **2026-11-10 用户拍板:dsh 派发优先 `minimax-cn/MiniMax-M3.1-Flash`(5 小时额度、不花现金,effort=high);仅主会话验收判定质量不达标时回退 deepseek-flash(预授权,留痕);flash 高峰时段(工作日 9-12/14-18)尽量不排**。背景事故:2026-11-09 夜 W8 会话卡在 dsh 沙箱拦 playwright 截图(等审批+反复重试),主会话 watchdog 报过一次后未续挂、仅靠 wait 单信号,卡整晚并踏入高峰烧钱;处置:cancel+主会话亲收(产物完整、19 安全测试绿、生产默认 enforce),截图改主会话 playwright 补拍;纪律补丁入记忆 deepseek-harness-local(watchdog 必续挂/dsh 禁截图/steer 不注入即 cancel) | 2026-11-10 09:0x 主会话执行用户指令 |

## 1. 产品定义与 MVP 五条件

单店电商售后 Copilot:买家侧一个聊天入口 + 客服侧工单接管。每天 50-200 条售后咨询的淘宝/独立站小店,1-2 名客服。

MVP = 一条最细的端到端链路同时满足五条件(逐条定义):
1. **单店铺**:一个 merchant;但每张业务表带 `merchant_id`,所有查询从第一天就带租户条件(扩展点,不是功能)。
2. **单任务类型**:售后(政策问答/订单判责/转人工三场景共用同一套状态机与工具);售前营销、生图、文案全部不做。
3. **端到端**:买家输入 → 感知 → 意图 → 检索/工具 → 判定 → (确认) → 写操作/回复 → 回执,全链路可跑,不是功能各做一半。
4. **有回执**:每个写操作有业务单号 + 状态 + UTC 时间 + trace_id;verified 靠回读(退款申请单状态回读进回执),reported ≠ verified。
5. **可复跑**:一条命令起服务、一条命令跑评测;评测 n=3、通过率带分母;数字可从测试输出重算。

## 2. 三场景(入口/出口)

| 场景 | 入口 | 出口 | 核心断言 |
|---|---|---|---|
| 政策问答 | 买家问退换货期限/运费/尺码 | 答案 + `[n]` 条款号引用(可点回原文) | 引用真实存在;无命中时明说,不编 |
| 订单判责 | 订单号 + 诉求(退货/换货/仅退款) | 查订单→查政策→判定;符合→**创建退款申请单(需人工确认)**;不符合→解释 + 替代方案 | 判定与金标准一致;不符合时零写操作;金额>实付直接拦 |
| 转人工 | 金额争议/投诉/连续 3 轮未解决/超轮次 | 建工单(带摘要)+ 会话置 `handed_off` + **AI 停止回复** + 人工领单守卫 | 工单真实创建;转人工后 AI 不再答;领单并发只成功一个 |

## 3. 不做清单(每项标注扩展点)

| 拒绝项 | 将来从哪长出来 |
|---|---|
| 多租户 SaaS、计费 | 表里的 `merchant_id` 列(查询已带条件,不用回填) |
| 自动退款、自动发券、自动改价 | 工具注册表 `pricing.*`/`coupon.*` 命名空间(只预留,不实现);退款永远只是"创建申请单+人工审核" |
| 售前营销、生图、文案生成、B 端 7 Tab | 不留扩展点(违反单任务类型) |
| 完整 i18n、多币种结算 | `currency` 列 + 话术资源文件(中文一套 + 英文资源文件) |
| 分布式、消息队列、微服务 | 不留(SQLite WAL 单机是显式选择) |
| 自研编排框架 | 不留(LangGraph 一张图) |
| 向量检索/FAISS | `retrieval` 接口 + `knowledge_item.embedding_model/dim` 列(见 D-02) |
| 附件独立表 | `chat_message` 存 URL(禁 base64),表结构见 W1-A 任务书 |
| 可视化评测大盘 | 评测报告 JSON(6 指标)是唯一口径 |

## 4. 风险契约六项(开工前钉死,PRD 第一页)

| # | 项 | 本项目结论 |
|---|---|---|
| 1 | **危险动作清单** | 写操作只有两个:`refund__create`(创建退款申请单;金额上限=订单实付,>¥500 或金额与实付不一致→强制人工,永不自动打款)、`handoff__create_ticket`(建/关工单)。改价/发券/批量发消息=不存在此能力。只读:`shop__get_order`、`policy__search`、`policy__check_return` |
| 2 | **失败语义表** | 超时=UNKNOWN_OUTCOME→按幂等键查 `refund_request` 台账,不自动重试;业务拒绝(政策不符/越权)=不重试,error 回喂模型;schema 错=回喂自检;明确瞬时错(5xx)=退避重试≤2 次且复用同一幂等键;LLM 空正文/超轮次=显式错误→handoff,不静默成功 |
| 3 | **回执标准** | 每个写操作回执字段:`业务单号 + status + UTC 时间 + trace_id`;verified 定义=回读 DB 中该单当前 status 写进回执;"已提交待确认"≠"已退款" |
| 4 | **状态落盘清单** | LangGraph checkpointer(SQLite,thread_id 维度)每步落盘;幂等台账=`refund_request.idempotency_key UNIQUE`;trace 事件表 append-only;判责中途崩溃→恢复时跳过已完成的查单/查政策,不重发通知、不重放副作用 |
| 5 | **护栏阈值** | 所有写操作需确认卡(四要素:做什么/影响面/不可逆性/替代方案),超时 10 分钟=拒绝并留草稿;退款金额>订单实付→代码层直接拦;>¥500 或金额争议+投诉→RISKY 强制人工;act 自环 `steps>8`→强制 handoff;同一会话 10 分钟内重复内容=疑似重复,跳过;定价指令("以后打 8 折")→隔离,不入可执行记忆 |
| 6 | **评测基线** | W1 先写 10 条 golden(断言业务结果:这单退了吗/金额对吗/回执对吗/禁调工具没调,不是 assert 200);W4 补到 48 条(19 golden/14 adversarial/10 edge/5 regression,17 字段落表);n=3;硬断言 100%、整体≥0.85(冲刺 0.9)、critical=0、judge agreement≥0.8 |

## 5. 系统架构(五层 × 六节点 × 5 工具 × 14 表)

- **五层契约**:感知(Pydantic 归一化)/编排(LangGraph)/工具(九字段注册表)/记忆(三层三时机)/评估(硬断言→裁判→人工抽检)。每层输入/输出/失败语义/可观测见 03§4.2.1,任务书逐层落地。
- **六节点图**:`perceive(纯函数) → classify(LLM) → retrieve(IO) → act(IO,自环,steps>8 强制 handoff) → compose(LLM) → handoff(IO)`;State 可序列化;写操作前 `interrupt`(先确认后执行,恢复不重放);SqliteSaver checkpointer。
- **5 个工具(九字段)**:`shop__get_order`(read)、`policy__search`(read)、`policy__check_return`(read)、`refund__create`(write,confirm,幂等键=sha256(order_id|target_state|reason|amount_cents))、`handoff__create_ticket`(write,confirm)。统一 `ToolResult{ok,data,error{code,message,retryable,param},meta}`;三层校验:registry=schema,service=resolve(存在性/归属),policy 层=业务规则。
- **14 张表**(W1-A):merchant, product, product_variant, product_metric, review, "order", order_item, refund_request, chat_session, chat_message, ticket, ticket_message, knowledge_item, eval_case。金额=整数分;状态=CHECK+服务层守卫+迁移表三件套;守卫式更新(`UPDATE...WHERE status='旧'`,rowcount=1);时间 UTC;软删除 `deleted_at`;memory 表在 W3-B 加(第 15 张),attachment 独立表不做(URL 列)。
- **记忆三处放**:店铺规则→知识库(版本化,带 clause_id 检索);订单事实→DB(唯一事实源);买家偏好→长期记忆(白名单+source/TTL/scope,任务开始注入一次并声明"可能过时",任务结束才抽取,定价指令隔离)。

## 6. 评测与验收

- 评测集 48 条 × n=3,三层判定:硬断言(FAIL_HARD/FAIL_CRITICAL,不进加权)→ 裁判三维 rubric(accuracy/compliance/safety,低温强制 JSON,用户输入转义分隔防注入,`task_success` 由服务端硬断言重算)→ 人工抽检 10%。
- 报告 6 指标:pass_rate(带分母+n)、by_layer、critical_fail_count(单列)、judge_agreement、cost_per_case(真实 token×单价)、p95_latency(用户实际等待,非裁判耗时)。失败成簇(同类≥3)才算系统性缺陷。
- 验收 SOP = 03§4.4 S1-S6:冻结基线→契约梳理→静态对齐(路径:行号)→零成本预检(无 key 全绿)→行为评测→真实 E2E+回滚。
- 三档判定:达到(硬断言 100%/整体≥0.9/agreement≥0.8/critical=0/有回滚)、部分达到、未达到(任一资损/越权/隐私失败=一票否决)。项目自验收线:整体≥0.85。

## 7. 排期与执行模型(W1-W4 阶段门 × dsh 单产物单会话)

| 阶段 | 目标 | 任务书 | 阶段验收门(主会话验收) |
|---|---|---|---|
| W1 数据层+骨架 | 14 表+迁移、/health、seed、CRUD、10 条评测 | W1-A / W1-B / W1-C | pytest 全绿;alembic upgrade head 一次成功;无 key 起服务 /health 200;10 条 case 过 schema 校验 |
| W2 感知+检索+工具 | 政策语料+BM25、5 工具九字段、Percept+薄路由 | W2-A / W2-B / W2-C | 3 个 golden 场景 FakeLLM 手工跑通;引用带 clause_id;越权/幂等/失败四分类测试全绿 |
| W3 编排+记忆 | 六节点图+checkpointer+interrupt、记忆白名单、转人工守卫+前端壳 | W3-A / W3-B / W3-C | 转人工后 AI 不回复;thread_id 恢复不重放副作用;定价指令被隔离;确认卡四要素+超时拒绝 |
| W4 评测+交付 | 48 条+judge+报告、一键起服务/评测、README、真实 E2E | W4-A / W4-B / W4-C | S5 全过:硬断言 100%、critical=0、agreement≥0.8;真实 key E2E 有原始响应证据+回滚 |

执行纪律(memory `deepseek-harness-local`):单产物单会话;任务书自带上下文纪律;每会话挂看门狗(5 分钟轮询产物,1 小时上限);host 忙时不重试硬发;旧会话续发失败即开新会话让其自读磁盘。

## 8. 目录布局

```
售后 Copilot/
├── docs/                  # PRD、任务书、验收记录(主会话写,dsh 只读)
│   ├── PRD_v1.md
│   ├── 任务书/00_通用约定.md + W1A…W4C(13 份)
│   └── 验收记录.md
├── backend/
│   ├── app/               # main.py, db.py, models/, api/, services/, tools/, graph/, memory/, retrieval/
│   ├── alembic/ + alembic.ini
│   ├── tests/
│   └── scripts/           # seed.py, load_eval_cases.py, demo_*.py, serve 入口
├── data/
│   ├── policies/          # 政策语料(md,带条款号)
│   ├── seed/              # mock 种子数据(json)
│   └── eval/              # cases_w1.json, cases_v1.json(48), rubric_v1.md, reports/
├── frontend/              # chat.html + js(SSE、确认卡、token/成本/时延显示)
├── .venv/                 # Python 3.13,禁止全局装包
├── requirements.lock.txt  # pip freeze,唯一依赖事实源
├── progress.md            # 单一写入方=主会话
└── .gitignore
```

## 9. 参考文档(只读)

- 设计九章:`D:\科研工作台\06_课程学习\Agent三方向进阶_20261102\learning_pack\concepts\stage7_Capstone售后Copilot设计.md`
- 重构清单/评测/SOP/四周节奏:`…\03_本地项目对齐_演进空间诊断.md` §4、§5
- 踩坑 30 条:`…\02_实战踩坑清单_WatchOutPoints.md`
- 风险契约检查单:`…\learning_pack\风险契约六项检查单.md`
- 归档代码锚点:`D:\科研工作台\10_归档封存\点头直通车项目\` 下 13 个子项目——**只读,任何会话不得写入/运行其中任何东西**(本项目 `售后 Copilot\` 目录除外)
