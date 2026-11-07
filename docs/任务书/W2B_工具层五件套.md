# 任务书 W2-B:工具层——注册表九字段 + 5 个工具 + ToolResult 三件套

> 先读 `docs/任务书/00_通用约定.md`。前置:W1-B(services)、W2-A(retrieval)已验收。

## 目标

把 W1-B 的服务和 W2-A 的检索包成 5 个带九字段契约的工具;统一 ToolResult;三层校验(schema→resolve→业务规则);错误回喂四要素;写工具带幂等键与 requires_confirm 标记。

## 只需读这几段(只读)

1. `docs/PRD_v1.md` §5(工具清单)、§4 第 1/2 项(危险动作与失败语义)
2. 03 文档第 650-674 行(§4.2.3 九字段表 + ToolResult/Event 三件套 + 双层校验)
3. G4 卡全文(refund__create 九字段参考答案、三层校验分工、越权返回 NOT_FOUND_OR_FORBIDDEN)
4. 正例:`AI智能旅游助手v3(约束文档设计)\backend\ai_service.py` 第 234-244 行(`{server}__{tool}` 命名空间分发);反例:`电商客服完整项目\backend\routers\b_admin.py` 第 98-107 行(error 包成 ok:true 的样子,别学)

## 产物文件(白名单)

```
backend/app/tools/__init__.py
backend/app/tools/registry.py       # ToolSpec 九字段(name/description/input_schema/output_schema/side_effect/timeout_s/retry/idempotency_key/requires_confirm)+ 注册表 + schema 校验(jsonschema 或 pydantic 动态模型,申请依赖写回执)+ 统一调用入口 invoke(name,args,ctx)->ToolResult
backend/app/tools/results.py        # ToolResult/ToolError 模型:{ok,data,error{code,message,retryable,param},meta{tool,duration_ms,cached}}
backend/app/tools/shop.py           # shop__get_order(read):resolve 校验=订单存在且属于 ctx.customer_id,否则 NOT_FOUND_OR_FORBIDDEN(对外不区分不存在/越权,内部日志记真因)
backend/app/tools/policy.py         # policy__search(read,包装 W2-A 检索,返回带 clause_id);policy__check_return(read,输入 order 状态+原因+天数,输出 {eligible, clause_id, reason};规则引擎=纯函数,天数/原因/条款对照表写成常量并测试)
backend/app/tools/refund.py         # refund__create(write,requires_confirm=True):幂等键=sha256(order_id|refund_request|reason|amount_cents),复用 W1-B refunds 服务;金额>实付→业务拒绝不重试
backend/app/tools/handoff.py        # handoff__create_ticket(write,requires_confirm=True):建工单带 handoff_reason+summary
backend/app/tools/events.py         # Event 写 trace:每次 invoke 记 {type:tool_call,ts,trace_id,step,payload摘要}(先落 SQLite 表 tool_events——本任务书允许在 backend/app/tools/migrations_add_tool_events.py 里加一张 alembic 迁移,这是唯一允许的 models/alembic 改动)
backend/tests/test_tools.py         # ≥20 个测试
```

## 实现要点

1. input_schema 全部 JSON Schema、`additionalProperties:false`、required 显式;order_id 用 pattern `^ORD-[0-9A-Z-]+$`
2. description 写"什么时候用/什么时候别用"(给未来 LLM 看的,照 G4 卡 refund__create 示例的口气)
3. 失败四分类落在 ToolError.retryable + code:SCHEMA_ERROR(回喂,不重试)/NOT_FOUND_OR_FORBIDDEN(不重试)/BUSINESS_REJECT(不重试)/TIMEOUT(UNKNOWN_OUTCOME,retryable:false,meta 标注"先查幂等台账")/TRANSIENT(重试≤2、退避、同一幂等键)
4. timeout_s 用注册表值包 asyncio.wait_for;写工具在 requires_confirm=True 时,invoke 不直接执行,返回 `{ok:true,data:{pending_confirm:四要素卡片内容,confirm_token}}`——真正执行在 W3 的 interrupt 确认后调 `invoke_confirmed(confirm_token)`;本任务书只需实现到"生成确认卡+token 落库",token 表可放 tool_events 或独立 confirm_request 表(迁移里加)
5. 每个工具的错误路径都必须有测试;越权测试用两个 customer fixture

## 验收标准

1. pytest 全绿(全量跑,含之前的),贴输出
2. 演示脚本或测试证明:schema 错回喂四要素齐全、越权返回 NOT_FOUND_OR_FORBIDDEN 且不含订单任何字段、同幂等键调两次只有一张退款申请单、refund 金额>实付被 BUSINESS_REJECT、写工具返回 pending_confirm 卡片含四要素
3. 5 个工具九字段总表打印出来贴回执(可从 registry dump)
4. tool_events 表里能看到每次 invoke 一条事件(trace_id 贯穿一次演示调用链)

## 禁止

- 不改 W1-B services 的守卫逻辑(只能调用);除白名单里那一个迁移外不碰 models/alembic
- 需要新依赖(如 jsonschema)→ 先回执申请,主会话装好后你再继续(或用 pydantic 等价实现,不申请)
