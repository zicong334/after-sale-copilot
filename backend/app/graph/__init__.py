"""app.graph — W3-A 的 LangGraph 六节点编排(替换 W2-C 的 MOCK 直调)。

模块划分:``state``(共享状态)/ ``nodes``(六节点 + 条件边)/ ``build``(装配与
checkpointer)/ ``runner``(对外入口:run / resume / handle_turn)。

典型用法::

    from app.graph import runner

    result = runner.run(thread_id=session_row.id, percept=percept)
    if result.pending_confirm:
        # 图停在确认门,等人工确认
        final = runner.resume(thread_id=session_row.id, confirm_decision=True)
"""

from app.graph.build import (
    DEFAULT_CHECKPOINT_PATH,
    RECURSION_LIMIT,
    build_graph,
    checkpoint_db_path,
    open_checkpointer,
)
from app.graph.runner import (
    HANDOFF_BLOCKED_REPLY,
    TurnResult,
    build_blocked_turn,
    handle_turn,
    resume,
    run,
)
from app.graph.state import NODE_STEP_LIMIT, GraphState, initial_state

__all__ = [
    "DEFAULT_CHECKPOINT_PATH",
    "HANDOFF_BLOCKED_REPLY",
    "NODE_STEP_LIMIT",
    "RECURSION_LIMIT",
    "GraphState",
    "TurnResult",
    "build_blocked_turn",
    "build_graph",
    "checkpoint_db_path",
    "handle_turn",
    "initial_state",
    "open_checkpointer",
    "resume",
    "run",
]
