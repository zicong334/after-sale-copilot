"""``app.eval`` — W4-A 评测引擎(三层判定:硬断言 → 裁判 → 报告)。

本包只依赖既有 W1/W2/W3 产物,**不接真实 LLM**:
- ``hard_assert``:服务端硬断言,直接对 DB 状态 + trace 事件 + 回复文本做判定,
  负责重算 ``task_success``(裁判无权修改)。
- ``judge``:三维 rubric 裁判。W4-C 之前由 :class:`FakeJudge` 做确定性打分,
  标记 ``MOCK`` + ``scored_by=heuristic``;接口按 ``LLMClient`` 抽象预留 W4-C 替换点。
- ``engine``:逐 case、逐 repeat 跑图(FakeLLM),采集 artifact 交给硬断言/裁判。
- ``report``:6 指标报告 JSON/Markdown 生成 + 失败成簇。
- ``validate_cases``:48 条用例集的配额/字段校验(不修改 W1-C 冻结脚本)。

artifact 契约(engine 产出、hard_assert/judge 消费)::

    {
      "case_id": str, "attempt": int, "layer": str,
      "input": {"raw": str, "normalized": str|None, "attachments": [...]},
      "input_error": {"code": "VALIDATION_ERROR", "message": str}|None,
      "truncated": bool, "original_length": int|None,
      "session_id": str|None,
      "replies": [{"stage": str, "text": str, "intent": str, "latency_ms": float}],
      "reply": str,                    # 全部 stage 回复按序拼接(数字/短语断言用)
      "final_reply": str,              # 最后一条 stage 回复
      "session_status": str|None,
      "refunds": [{"order_id","amount_cents","status","idempotency_key"}],
      "tickets": [{"id","status","handoff_reason","order_id"}],
      "memory": [{"text","status","executable","scope"}],
      "confirm": {"asked": bool, "tool_name": str|None, "amount_cents": int|None,
                  "what","impact","irreversibility","alternatives", "status": str|None},
      "trace": [{"step","tool_name","ok","error_code","payload": dict}],
      "tool_names": [str],
      "failed_tools": [{"tool_name","error_code"}],
      "orders": {"before": [...], "after": [...], "intact": bool},
      "flags": {"replay_writes": int, "idempotency_writes": int,
                "toctou_error_code": str|None, "followup_ai_replied": bool|None},
      "latency_ms_total": float,
      "usage": {"tokens_in": int, "tokens_out": int, "cost_usd": float},
    }

``ground_truth`` 键(全部可选,详见 ``validate_cases`` 的 GROUND_TRUTH_KEYS):
intent、clause_id、answer_fact、must_contain、handoff、handoff_reason、
ticket_created、ticket_status、session_status、ai_stops_reply、refund_created、
refund_amount_cents、refund_status、eligible、reason、input_error、truncated、
original_length_min、tool_error、memory_status、memory_executable、
memory_text_contains、orders_intact、no_write、repeat_turns、approve、
followup_input、replay_resume、idempotency_retry、toctou_tamper_total_cents、
max_refund_amount_cents、pending_amount_cents、critical_if_refund_created、
critical_if_privacy_leak、critical_if_pricing_memory_active。
"""

from app.eval.hard_assert import FAIL_CRITICAL, FAIL_HARD, Finding, HardResult, evaluate
from app.eval.judge import FakeJudge, JudgeVerdict, LLMJudge, build_judge
from app.eval.report import build_report, render_markdown, write_report

__all__ = [
    "FAIL_CRITICAL",
    "FAIL_HARD",
    "Finding",
    "HardResult",
    "evaluate",
    "FakeJudge",
    "JudgeVerdict",
    "LLMJudge",
    "build_judge",
    "build_report",
    "render_markdown",
    "write_report",
]
