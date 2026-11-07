"""state.py — W3-A 六节点图的共享状态(``State``)。

设计约束(03§4.2.2 第 2 条):State 里只放**可 JSON 序列化**的值。
它要原样落进 SqliteSaver 的 checkpoint,并在进程重启(新的 graph 实例)后被读回,
所以这里不放 ORM 对象、不放 Session / LLM 句柄、不放工具完整返回体:
工具结果只在 ``observations`` 里留摘要(name / ok / error_code / note),
业务事实只留紧凑引用(``retrieved`` 里的 clause_id 与条款原文)。

字段语义:

- ``percept``:感知层归一化结果(``Percept.model_dump()``,纯字典);
- ``memory``:任务开始时注入一次的买家长期偏好包(``{text,count,scopes}``,纯字典);
- ``context``:会话槽位(接口层传入的确定性字段:order_id / days_since_delivery …);
- ``intent``:意图判定与槽位 {name, confidence, reason, path, order_id};
- ``retrieved``:检索/判定用到的条款(含 clause_id,compose 据此出 ``[n]`` 引用);
- ``plan``:待执行的工具调用队列(act 每圈消费一个,条件边据此自环);
- ``observations``:已执行工具调用的摘要(对应 ``tool_calls`` 的对外九字段裁剪版);
- ``draft``:compose 产出的回复文本(挂起那一轮也要有,HTTP 响应才能带 reply);
- ``verdict``:判定事实包(compose 的输入,决定用哪套话术);
- ``handoff``:是否走转人工分支(明示人工 / 连续 3 轮 / 金额争议 / 步数上限);
- ``steps``:act 已执行圈数(``> NODE_STEP_LIMIT`` 强制转人工,第 9 圈不再调工具);
- ``trace_id``:本轮 trace(跨"挂起 → 重启 → resume"保持不变,事件才能串成一条链);
- ``confirm``:确认门状态机,``None`` 表示本轮没有待确认的写操作。

``confirm`` 的字段(runner / nodes 共用):

``{"tool": str, "args": dict, "card": {四要素}, "token": str,
   "replied": bool, "decided": bool, "approved": bool | None,
   "executed": {"ok": bool, "data": dict, "error": dict} | None}``

“建卡”与“interrupt 收确认”必须落在两个不同的 super-step(P0-C2):
``confirm`` 存在但 ``decided=False`` 时节点一进来就 ``interrupt``;恢复时节点
从头重跑,此时 ``confirm`` 已在 checkpoint 里,所以不会二次建卡。
"""

from __future__ import annotations

from typing import Any, TypedDict

#: act 自环的上限(正例 ai_service.py:661-664 ``MAX_TOOL_ROUNDS``)。
#: 超过该圈数仍未收敛 → 第 ``NODE_STEP_LIMIT + 1`` 圈不再调工具,强制转人工。
NODE_STEP_LIMIT = 8


class GraphState(TypedDict, total=False):
    """六节点图的共享状态(每个字段都必须可 JSON 序列化)。"""

    percept: dict[str, Any]
    memory: dict[str, Any]
    context: dict[str, Any]
    intent: dict[str, Any]
    retrieved: list[dict[str, Any]]
    plan: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    draft: str
    verdict: dict[str, Any]
    handoff: bool
    steps: int
    trace_id: str
    confirm: dict[str, Any] | None


#: 每轮进线的初始状态:所有字段都显式赋值。
#: 同一个 thread_id(同一 chat_session)会被多轮复用,而 LangGraph 会把新一轮
#: 输入**合并**到该 thread 已有的 channel 值上——只有把每个字段都写全,
#: 上一轮残留的 steps / observations / confirm 才不会被带进本轮。
def initial_state(
    *, text: str, context: dict[str, Any] | None, trace_id: str
) -> GraphState:
    """构造本轮进线的干净初始状态(每个字段都显式重置)。"""
    return {
        "percept": {"text": text},
        "memory": {},
        "context": dict(context or {}),
        "intent": {},
        "retrieved": [],
        "plan": [],
        "observations": [],
        "draft": "",
        "verdict": {},
        "handoff": False,
        "steps": 0,
        "trace_id": trace_id,
        "confirm": None,
    }
