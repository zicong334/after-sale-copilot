# 任务书 W2-C:感知层 + 会话薄路由 + FakeLLM 三场景跑通

> 先读 `docs/任务书/00_通用约定.md`。前置:W2-A、W2-B 已验收。

## 目标

买家从 HTTP 进来的一句话变成归一化 Percept;会话/消息落库带 trace_id;用 FakeLLM(规则+脚本,显式标 MOCK)把三个 golden 场景端到端串起来——这是 W3 上图前的"无图直调"基线。

## 只需读这几段(只读)

1. `docs/PRD_v1.md` §2(三场景入口/出口)、§5(五层契约)
2. 03 文档第 608-615 行(五层契约表:感知层/编排层行)+ 第 691-705 行(反模式 1:关键词只做高置信快捷路径)
3. G3 卡(perceive=纯函数不调 LLM;节点边界)
4. 反例(别学):`电商客服完整项目\backend\agents\c_router.py` 第 26-30 行(13 词硬编码 confidence)

## 产物文件(白名单)

```
backend/app/llm/__init__.py
backend/app/llm/base.py             # LLMClient 抽象(complete(messages, schema)->str/json);FakeLLM 实现(规则分类+模板回复,类名/注释/回执显式标 MOCK);W4-C 才接真模型
backend/app/perception.py           # Percept{text, attachments:[url], locale, channel} Pydantic 模型 + normalize(去零宽字符、长度上限 4000 截断并标记、图片只收 URL 拒 base64)
backend/app/api/chat.py             # POST /api/c/chat:薄路由——Percept 校验→chat_session/chat_message 落库(带 trace_id)→调 orchestrator→回复落库→返回;路由里零业务逻辑
backend/app/orchestrator/w1_router.py  # MOCK 编排(显式标注"W3 将被 LangGraph 图替换"):FakeLLM 意图分类(policy_qa/order_judge/handoff/other)→按场景直调 W2 工具(不经图)→组装回复;政策问答带 [n] 引用(clause_id);判责走 policy__check_return→符合则出确认卡(pending_confirm);连续 3 轮未解决或用户要人工→handoff__create_ticket+会话置 handed_off
backend/app/trace.py                # trace_id 生成(uuid7 或 uuid4)+ 结构化事件写 tool_events(type 字段区分)
backend/scripts/demo_w2.py          # 命令行演示三场景(读 cases_w1.json 里 3 条对应 case 的 user_input,打印每步工具调用与最终回复)
backend/tests/test_perception.py    # ≥6:归一化、超长、base64 拒收、空输入 400
backend/tests/test_chat_route.py    # ≥10:三场景各至少 1 条端到端(FakeLLM);转人工后再次发消息被拒(AI 不回复);消息落库带 trace_id;引用里的 clause_id 在 knowledge_item 中真实存在
```

## 实现要点

1. 意图分类的关键词规则只允许做"高置信快捷路径"(命中即返回,confidence=1.0 且有消费方),其余一律走 FakeLLM 的规则表;规则表写成常量便于 W3 替换
2. `handoff` 判定三触发:金额争议(RISKY 关键词+金额不符)、连续 3 轮同意图未解决、用户明示人工;触发后 `chat_session.status='handed_off'`,路由入口先查会话状态,非 active 直接返回固定话术不调编排(这就是 G7 的"AI 停止回复"判定点)
3. 回复中的引用格式 `[n]` ↔ clause_id 映射随响应返回(citations 数组),前端 W3-C 用
4. 每步工具调用、意图判定、会话状态变更都写 trace 事件(同一 trace_id)

## 验收标准

1. pytest 全量绿,贴输出
2. `demo_w2.py` 三场景演示输出原文贴回执:政策问答带 clause_id 引用;判责-符合出确认卡;判责-超期拒绝且无写操作;转人工后再发消息被拒
3. 对 W1-C 的 10 条 case 里可无图跑通的部分(至少 6 条),写一个 `scripts/check_w1_cases.py` 用 FakeLLM 跑并输出每条 pass/fail(硬断言:expected_tools 调了、forbidden_tools 没调、关键数字在回复里)——结果贴回执;不达 6/10 也照实贴,这是 W3 的输入基线,不许改断言硬凑
4. grep 证明所有 MOCK 标注就位:`grep -rn "MOCK" backend/app | head -20`

## 禁止

- 不接真实 LLM;不上 LangGraph(W3-A 的事);不改 W1/W2-A/W2-B 产物
