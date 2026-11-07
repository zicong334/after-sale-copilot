# 任务书 W4-C-FIX:确定性护栏修复包(七项,对应第一轮真实评测的五个失败簇)

> 先读 `docs/任务书/00_通用约定.md` 并全程遵守(§6 沙箱护栏 / §7 次模守则 / 熔断 2 次)。
> 背景:`docs/验收报告_W4C.md` 第一轮真实模型评测判"未达到"(0.6944,critical=6)。失败成簇:tool_selection 16 / forbidden_tool 8 / fact_mismatch 7 / handoff_reason 7 / authorization 6。根因:**对抗输入下真实模型跳过工具、用泛化话术兜底、编造未验证的系统状态**。
> 修复哲学(设计文档铁律):**不指望模型变聪明,给每个失败模式加一层非 LLM 的确定性兜底**;prompt 约束只做辅助,代码守卫才是验收对象。
> **红线:本任务书全程禁用真实 API(.env 的 key 不许读),只用 FakeLLM/单元测试验证;真实重跑由主会话执行。**

## 冻结基线

commit 4d7b913;263 测试全绿(dsh 侧;主会话侧 test_shell_scripts_syntax 因沙箱受限豁免);check_w1_cases 10/10;评测集 cs-eval-v1-20261108a。既有产物默认冻结,只许改下面白名单。

## 七项修复(每项必须有专项测试)

### FIX-1 越权核验强制链(簇:authorization 6 + REFUND-006)
- 现状:CS-ADV-002/007 查询他人订单时,模型不调 `shop__get_order`,直接用"没查到"话术兜底 → 越权核验流程缺失(critical)。
- 修法(确定性):`act` 节点加服务端守卫——当输入文本或 context 含订单号形态(pattern 同工具层),或 intent∈订单相关,而本轮 trace 尚无任何 `shop__get_order` 调用时,**编排层强制注入一次 `shop__get_order`**(用抽取到的订单号;抽取不到则强制走追问话术,仍不许编造查询结果)。工具返回 `NOT_FOUND_OR_FORBIDDEN` 时 compose 必须转达拒绝(接 FIX-4)。
- 测试:合成 State(无工具调用+含他人订单号)→ 守卫注入调用且返回 NOT_FOUND_OR_FORBIDDEN;无订单号时不注入。

### FIX-2 refund 前置策略门(簇:forbidden_tool 8,ADV-012/014 注入误调 refund)
- 修法:`refund__create` 的 handler 增加**策略校验段**(G4 卡三层校验的第三层,从 prompt 约束升级为代码约束):同一 trace_id 的 tool_events 里必须已存在 `policy__check_return` 且结果 eligible=true,否则拒绝执行,返回 `ToolError(code=BUSINESS_REJECT, message=需先核验退货资格, retryable=false)`。确认卡/幂等逻辑不变。
- 测试:无 check_return 前置 → BUSINESS_REJECT 且零写;有前置 eligible → 正常出确认卡。

### FIX-3 注入感知护栏(簇:EDGE-008 违规建单 + ADV-001/005/010/011 部分)
- 修法:`perception.py` 加确定性注入特征检测(SQL 片段/命令拼接/"忽略以上指令"/伪造系统话术等,正则清单写成常量便于审计),命中 → `Percept.flags` 加 `injection_suspect`。图规则(非 LLM):
  - 纯注入(无正当售后意图)→ 固定拒绝话术,**零工具、会话保持 active、不建单不转人工**(CS-EDGE-008 的期望行为);
  - 注入+正当诉求混合(ADV-001 类)→ 只处理正当部分,注入文本绝不进工具参数、不进记忆(记忆抽取白名单同步加 injection 隔离,同定价指令待遇)。
- 测试:三类注入样本各自断言(零工具/混合只处理正当部分/记忆隔离)。

### FIX-4 反编造护栏(簇:话术失真;S6 披露的"确认前称已转接")
- 修法:`compose` 后置校验(确定性):回复文本若含"已转接/已转人工/查不到/未找到/已提交/已受理"等状态声称(常量清单),必须在同轮 trace/DB 里有对应证据(工单实存/handoff 事件/工具返回/退款单实存);无证据 → 用诚实话术模板替换该句("我尚未执行该操作;…")。转人工确认卡**确认前**的话术统一为"将为您转接人工(待确认)"。
- 测试:无证据声称被替换;有证据声称保留;确认前话术断言。

### FIX-5 引用保真(簇:fact_mismatch 7,POLICY-007/EDGE-002)
- 修法:compose prompt 强化"引用条款必须包含条款原文的关键数字短语(逐字)";再加一道后置校验:回复引用的 clause 的关键数字短语(从 RetrievedChunk.text 提取数字+单位片段)未逐字出现 → 重组回复一次(模板拼接引用原文),仍缺则如实附条款原文。
- 测试:构造检索命中"满 99 元包邮"→ 回复必含该短语;模型漏引时后置拼接生效。

### FIX-6 handoff_reason 枚举化(簇:handoff_reason 7;数据治理#2,主会话已授权)
- 修法:`handoff__create_ticket` input schema 的 `reason` 改 Literal 枚举:`amount_dispute / complaint / unresolved_3rounds / explicit_request / step_limit / security_risk / other`,入库 `handoff_reason=枚举值`,`summary` 存自然语言(列已存在)。图内触发点映射到对应枚举。
- **数据治理#2(授权范围内)**:`data/eval/cases_v1.json` 中受影响 case 的 ground_truth/expected 短语对齐 canonical 枚举值(逐条列进回执 diff),`data/eval/VERSION.txt` 升 `cs-eval-v1-20261108b`;validate/load 重跑通过。**不许改任何安全断言语义,只对齐 reason 字段口径。**
- 测试:每个触发点产出正确枚举;非法 reason 被 schema 拒;受影响 case validate 通过。

### FIX-7 记忆场景意图(MEMORY-001)
- 修法:classify 规则表/few-shot 补"尺码/偏好/以后推荐"类 → 偏好处理场景(回复须引用记忆注入内容,含"尺码"来源);intent 判 `other` 而非 `policy_qa`。
- 测试:MEMORY-001 输入在 FakeLLM 下 intent=other 且回复含尺码偏好引用。

## 产物白名单

```
backend/app/graph/nodes.py / runner.py / state.py(如需)
backend/app/perception.py
backend/app/tools/handoff.py / refund.py
backend/app/memory/whitelist.py / store.py(injection 隔离)
backend/app/llm/base.py(FakeLLM 规则表/prompt 常量,MOCK 标注保持)
backend/app/guardrails.py(新建:注入特征清单/状态声称清单/关键短语提取,常量集中可审计)
backend/tests/test_guardrails.py(新建)/ test_tools.py / test_graph.py / test_chat_route.py / test_memory.py(增量)
data/eval/cases_v1.json + VERSION.txt(仅 FIX-6 治理范围)
backend/README.md(护栏清单节更新)
```

## 回归红线(收尾全跑,一项不过即熔断汇报)

1. 全量 pytest ≥263+新增 全绿(FakeLLM 密封,零真实 API)
2. `check_w1_cases.py` 10/10 不倒退
3. `run_eval.py --smoke`(Fake 模式)通过率不低于第一轮同口径
4. FIX-6 触碰过的 case:validate_eval_cases.py exit 0 + load 幂等
5. `grep -rn "TODO\|FIXME"` 新增代码为空;`.env` 未被读取(测试证明)

## 回执要求(§4 五段+专项)

- 每项 FIX:改动文件:行号 + 专项测试名 + 测试输出原文
- FIX-6 的 cases_v1.json 逐条 diff(case_id/字段/旧值/新值)
- 七项修完后,13 条 0/3 用例逐条对照:说明该用例的失败模式被哪几项 FIX 的哪个确定性守卫覆盖(不承诺真实模型分数,只说明机制覆盖)
