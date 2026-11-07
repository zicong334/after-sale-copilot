"""build.py — W3-A 六节点图的装配(节点 + 边 + checkpointer)。

边拓扑(与任务书一致)::

    START → perceive → classify → retrieve → act
                                              │  needs_more_steps(条件边)
                    ┌─────────────────────────┼──────────────────────┐
                 "more"                     "done"                "handoff"
                    ↓                          ↓                      ↓
                   act                     compose                 handoff
                                               │  after_compose        │
                               ┌───────────────┴──────────┐            │
                        "act"(退款确认门)        "handoff"(转人工门)     │
                               ↓                          ↓            │
                              act                      handoff ←───────┘
                                                          │
                                                       compose → END

要点:

- ``act`` 自环由条件边实现(``needs_more_steps`` 返回 ``"more"``),循环上限由
  ``state["steps"]`` 自己守住(第 9 圈不再调工具),不依赖 LangGraph 的
  ``recursion_limit``(后者超限是抛异常,不是优雅降级);
- 写工具的确认门是 ``interrupt``:节点在 ``interrupt`` 之前只建卡、不做业务副作用,
  resume 时节点从头重跑也安全(P0-C2);
- checkpointer 用独立文件 ``data/checkpoints.db``(已 gitignore),与业务库 ``dev.db``
  分离:图状态是运行期断点,不是业务数据。``thread_id = chat_session.id``。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.graph.nodes import after_compose, make_nodes, needs_more_steps
from app.graph.state import GraphState
from app.llm import LLMClient
from app.models import ChatSession

#: backend/ 目录(app/graph/build.py → parents[2])
_BACKEND_DIR = Path(__file__).resolve().parents[2]
#: 项目根(ROOT)
ROOT_DIR = _BACKEND_DIR.parent

#: 图状态断点库(独立文件,已 gitignore;不进业务库)
DEFAULT_CHECKPOINT_PATH = ROOT_DIR / "data" / "checkpoints.db"

#: 单次 invoke 的 super-step 上限:正常一轮 ≈ 6 + 自环圈数(≤9)+ compose,
#: 50 有足够余量;真正防跑飞的是 state["steps"] 的 NODE_STEP_LIMIT。
RECURSION_LIMIT = 50


def checkpoint_db_path(path: str | Path | None = None) -> Path:
    """确认断点库路径(默认 ``ROOT/data/checkpoints.db``),必要时建目录。"""
    target = Path(path) if path is not None else DEFAULT_CHECKPOINT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def open_checkpointer(path: str | Path | None = None) -> SqliteSaver:
    """打开 SqliteSaver(每次调用新建连接:跨进程重启后也能读回同一 thread)。

    不使用 ``SqliteSaver.from_conn_string``:它是 contextmanager,生命周期与本模块
    "一次 run 一次连接"的用法不一致。``check_same_thread=False`` 是 sqlite 驱动要求
    (saver 内部自带锁);``setup()`` 由 saver 首次 ``cursor()`` 时自动调用,无需手调。
    """
    db_path = checkpoint_db_path(path)
    conn = sqlite3.connect(db_path.as_posix(), check_same_thread=False)
    return SqliteSaver(conn)


def build_graph(
    *,
    session: Session,
    session_row: ChatSession,
    checkpointer: SqliteSaver,
    llm: LLMClient | None = None,
    prior_intent_counts: dict[str, int] | None = None,
) -> Any:
    """装配并编译六节点图(每次调用重新编译:节点闭包绑定本轮的 Session)。"""
    nodes = make_nodes(
        session=session,
        session_row=session_row,
        llm=llm,
        prior_intent_counts=prior_intent_counts,
    )
    builder = StateGraph(GraphState)
    for name in ("perceive", "classify", "retrieve", "act", "compose", "handoff"):
        builder.add_node(name, nodes[name])

    builder.add_edge(START, "perceive")
    builder.add_edge("perceive", "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "act")
    builder.add_conditional_edges(
        "act",
        needs_more_steps,
        {"more": "act", "done": "compose", "handoff": "handoff"},
    )
    builder.add_edge("handoff", "compose")
    builder.add_conditional_edges(
        "compose",
        after_compose,
        {"act": "act", "handoff": "handoff", "end": END},
    )
    return builder.compile(checkpointer=checkpointer)
