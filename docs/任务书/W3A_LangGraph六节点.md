# 任务书 W3-A:LangGraph 六节点图 + checkpointer + interrupt 确认门

> 先读 `docs/任务书/00_通用约定.md`。前置:W2-C 已验收。

## 目标

把 W2-C 的 MOCK 直调编排替换成 LangGraph 一张图:六节点、条件边、act 自环带硬上限、写操作前 interrupt、SqliteSaver 每步落盘、thread_id 恢复不重放副作用。

## 只需读这几段(只读)

1. `docs/PRD_v1.md` §5(六节点图)、§4 第 4 项(状态落盘)
2. G3 卡全文(节点边界/State 字段/第 9 圈行为/重启恢复/为什么不用 DAG)+ G7 卡(先确认后执行,interrupt 前的副作用会被重放)
3. 03 文档第 616-648 行(§4.2.2 图伪代码 + 四条硬规则)
4. 02 踩坑清单 P0-C2(interrupt 与重放,读该条即可)

## 产物文件(白名单)

```
backend/app/graph/__init__.py
backend/app/graph/state.py          # State TypedDict:percept/intent/retrieved/plan/observations/draft/verdict/handoff/steps/trace_id/confirm(全部可序列化)
backend/app/graph/nodes.py          # 六节点:perceive(纯函数)/classify(FakeLLM)/retrieve(IO)/act(IO+自环)/compose(FakeLLM)/handoff(IO)
backend/app/graph/build.py          # 图组装:conditional_edges(act→needs_more_steps→{more:act,done:compose});steps>8 强制 handoff(reason=step_limit);SqliteSaver checkpointer(独立 db 文件 data/checkpoints.db);写工具确认=interrupt(先 interrupt 收确认,恢复后才 invoke_confirmed——副作用绝不在 interrupt 之前)
backend/app/graph/runner.py         # 对外入口:run(thread_id, percept)->结果;resume(thread_id, confirm_decision)->恢复;替换 W2-C 的 w1_router 调用点(api/chat.py 只改 import 与调用两行,允许)
backend/tests/test_graph.py         # ≥14 个,FakeLLM 回放(L2):三场景各 1;act 自环 2 圈后 done;steps 到 9 强制 handoff 且第 9 圈没调工具;interrupt 挂起→恢复→退款单只有一张;进程重启模拟(新 runner 实例+同 thread_id)恢复到挂起点;恢复时已执行副作用不重放(用计数桩证明);超轮次 trace 事件完整
```

## 实现要点

1. 节点边界铁律:perceive 里不许调 LLM,act 里不许直接写库(只经工具层),compose 里不许发 IO
2. State 只放可序列化字段;工具结果存摘要+引用 id,不塞大对象
3. checkpointer 用 `langgraph-checkpoint-sqlite` 的 SqliteSaver;thread_id=chat_session.id
4. 恢复语义写进 runner.py docstring:interrupt 恢复时节点从头重跑→所以确认收集必须在任何副作用之前(G3 自查题答案的机制)
5. W2-C 的 `check_w1_cases.py` 现在改走图重跑一遍,结果对比贴回执(不许倒退)

## 验收标准

1. pytest 全量绿,贴输出
2. `check_w1_cases.py` 走图后 pass 数 ≥ W2-C 基线,贴前后对比
3. 演示脚本或测试输出证明:挂起→"重启"(新实例)→resume→退款单一张、trace 里无重复副作用事件
4. steps=9 强制 handoff 的测试输出原文
5. `grep -rn "w1_router" backend/app` 只剩注释/文档引用(编排已换图)

## 禁止

- 不接真实 LLM;不改工具层/服务层逻辑;checkpoints.db 不进 git(主会话 .gitignore 已管,你不要动)
