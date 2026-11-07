# 任务书 W1-B:服务骨架——FastAPI 单 app + /health + 守卫式 CRUD + seed + 无 key 启动

> 先读 `docs/任务书/00_通用约定.md` 并全程遵守。前置:W1-A 已验收(models/ 与 alembic/ 已存在,直接 import,不得修改它们;发现模型缺陷→回执汇报,不自行改)。

## 目标

一条命令起服务、`/health` 200;订单/工单的状态推进全部走**守卫式更新**;mock 数据 seed 可重复执行。全项目从此有"可回到的绿色基线"。

## 只需读这几段(只读)

1. `docs/PRD_v1.md` §4(失败语义/回执)、§5(五层契约)
2. 03 文档第 577-587 行(ticket/order/refund 状态机迁移表——你的服务层守卫照这张表实现)
3. G2 卡(守卫式更新:`UPDATE ... WHERE status='旧状态'`,影响行数=1 才算成功;TOCTOU 禁止先查再改)
4. 正例骨架:`CLI深度使用（好像项目还没玩明白）\app\main.py` 第 43-58 行(单 app+健康检查+会话中间件)
5. 反例(避坑,不改它):`电商客服完整项目\backend\services\tickets.py` 第 76-117 行(claim/close 无守卫的样子)

## 产物文件(白名单)

```
backend/app/main.py                 # 单 app;lifespan 里建 engine;/health;静态错误处理注册;不挂业务路由(用 include_router)
backend/app/errors.py               # 统一错误体 {ok:false,error:{code,message,retryable,param}} + 8 个 handler(含 RequestValidationError→400、业务冲突→409、未捕获→500 且不泄露堆栈)
backend/app/schemas/                # Pydantic v2 请求/响应模型(order、ticket、通用回执 Receipt)
backend/app/services/orders.py      # 订单状态推进:pay/ship/deliver/refund_complete,全部守卫式
backend/app/services/tickets.py     # claim/close/add_message,守卫式;关单后 add_message 拒绝
backend/app/services/refunds.py     # create_refund_request(带 idempotency_key,重复提交返回已有单而非报错=幂等命中)/ approve(申请→审核)/ reject(→驳回)/ mark_refunded(审核→退款,守卫:必须存在审核态单)+ 每一步产出 Receipt(回读 status 写进回执=verified)
backend/app/api/orders.py           # REST:GET/POST 订单、POST 状态推进
backend/app/api/tickets.py          # REST:GET/POST 工单、claim、close、消息
backend/app/api/refunds.py          # REST:创建/审核/驳回/退款完成
backend/scripts/seed.py             # mock 数据:1 merchant、2 客服、10 客户、30 订单(覆盖各状态)、若干工单;幂等(先删后插或 upsert,重复跑不翻倍)
backend/tests/test_api.py           # TestClient 测试,≥18 个
backend/tests/test_services.py      # 服务层测试(含并发守卫),≥12 个
```

## 实现要点(硬要求)

1. **守卫式更新**:所有状态迁移用 `UPDATE … WHERE id=? AND status='旧'`,`rowcount!=1` → 返回 409 `STATE_CONFLICT`(带当前真实状态);禁止 SELECT 后 UPDATE
2. **refund 幂等**:`create_refund_request` 先按 `idempotency_key` 查,命中→返回已有单(回执标 `idempotent_hit:true`);金额>订单实付→直接 422 `AMOUNT_EXCEEDS_PAID`,不建单;`uq_refund_active` 冲突→409
3. **回执(Receipt)**:每个写操作响应体带 `{refund_request_id|ticket_id|order_id, status(回读值), occurred_at_utc, idempotent_hit}`——status 必须是写完后**重新 SELECT 回来的值**
4. **refund 状态推进**:`申请→审核`(approve,记 decided_by/decided_at)、`申请→驳回`(reject)、`审核→退款`(mark_refunded;同时守卫式把 order 推 `delivered→refunded`,守卫条件含"存在 status='退款' 的退款单")
5. **无 key 启动**:整个 app 不 import 任何 LLM/外部 key;`uvicorn app.main:app` 能直接起
6. seed 数据里必须有:1 张 9 天前 delivered 的订单(给 W1-C 判责用例用)、1 张 3 天前 delivered、1 张 paid 未发货
7. 测试必含:非法迁移被 409;两个"并发" claim 同一工单只有一个成功(同事务序列模拟即可);关单后加消息被拒;幂等键重复提交只有一张单;金额超实付被拦;seed 跑两遍数据量不变

## 验收标准(主会话逐条核)

1. `ROOT\.venv\Scripts\python -m pytest backend/tests -q` 全绿(含 W1-A 的模型测试一起跑),贴原始输出
2. 起服务后 `curl http://127.0.0.1:8000/health` 返回 200,贴命令与响应原文;然后停服务
3. `python scripts/seed.py` 连跑两遍,贴两遍后的行数统计,证明幂等
4. 用 curl 或 httpx 脚本演示一次完整退款链:创建申请(带幂等键)→重复创建(命中幂等)→approve→mark_refunded→回读订单 status='refunded',贴每步响应原文
5. `grep -rn "TODO\|FIXME\|NotImplemented" backend/app backend/scripts backend/tests` 为空

## 禁止

- 不改 W1-A 产物(models/alembic/db.py);不改 docs/
- 不接任何真实 LLM、不发外部网络请求
- 错误响应不许 `{ok:true, data:{error:…}}` 这种包法(失败必须 ok:false 冒泡)
