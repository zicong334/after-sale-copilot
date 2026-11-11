"""check_w1_cases.py — 用 FakeLLM(MOCK)跑 data/eval/cases_w1.json 的 10 条 case。

运行(项目根目录):
    .venv\\Scripts\\python backend/scripts/check_w1_cases.py

硬断言(不做任何"凑分"处理):
1. expected_tools 必须都被调用(按工具名,含失败调用也算调用过);
2. forbidden_tools 不得被调用;
3. expected_numbers 必须都出现在最终回复里(数字对齐 PRD 口径);
4. forbidden_phrases 不得出现在最终回复里;
5. must_ask_confirmation=true 时必须给出确认卡(pending_confirm);
6. ground_truth.eligible 与确认卡/拒绝结果一致;
7. ground_truth.error_code 出现时,工具调用必须报出同一错误码;
8. ground_truth.session_status 出现时,会话状态必须一致;
9. 回复中的 [n] 引用必须映射到 knowledge_item 里真实存在的 clause_id。

⚠️ MOCK:意图判定与回复由 ``app.llm.base.FakeLLM`` 规则+模板产出,未接真实模型。
W2-C 边界说明:写工具只到"确认卡",refund_request / ticket 落库由 W3 确认流执行,
因此 ground_truth.ticket_created 只作备注(不算失败)。
白名单说明:任务书 W2-C 的白名单 10 项漏列了验收条款要求的本脚本
(docs/任务书/W2C_感知与会话路由.md 验收第 3 条),本文件按验收条款创建;
该偏差已在执行回执【未尽事项】中说明。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# ---------------------------------------------------------------------------
# 评测 harness 内部模式,生产默认 enforce 不受影响。
#
# 本脚本用 TestClient 直连服务(等价于本地进程内调用,没有真实登录动作),
# W8 鉴权中间件上线后默认 enforce 会让 /api/c/chat 直接 401。
# 因此在**本进程内**把 AUTH_MODE 设为 internal:中间件放行,身份由请求体的
# merchant_id / customer_id 内部兜底(与 W8 之前的行为一致)。
# 该 env 只作用于本脚本进程,不写 .env、不改服务默认值——服务/生产默认仍是
# enforce(缺 AUTH_MODE 即 enforce,见 app/security.py:auth_mode)。
# ---------------------------------------------------------------------------
os.environ.setdefault("AUTH_MODE", "internal")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import KnowledgeItem  # noqa: E402
from scripts.demo_w2 import CASES_PATH, MERCHANT_ID, customer_for, ensure_data  # noqa: E402

PASS_THRESHOLD = 6


def kb_clause_ids() -> set[str]:
    with SessionLocal() as session:
        rows = session.scalars(select(KnowledgeItem.clause_id)).all()
    return {str(r) for r in rows if r}


def run_case(client: TestClient, case: dict, clause_ids: set[str]) -> dict:
    context = dict(case.get("context") or {})
    body = {
        "merchant_id": MERCHANT_ID,
        "customer_id": customer_for(context),
        "text": case["user_input"],
        "context": context,
    }
    response = client.post("/api/c/chat", json=body)
    if response.status_code != 200:
        return {"case": case, "http_status": response.status_code, "body": response.json()}

    data = response.json()
    reply = str(data.get("reply") or "")
    calls = [c["name"] for c in data.get("tool_calls") or []]
    call_errors = [c.get("error_code") for c in data.get("tool_calls") or [] if c.get("error_code")]
    pending = data.get("pending_confirm")
    gt = dict(case.get("ground_truth") or {})

    checks: list[tuple[str, bool, str]] = []

    missing_tools = [name for name in case.get("expected_tools") or [] if name not in calls]
    checks.append(("expected_tools", not missing_tools, f"missing={missing_tools}; calls={calls}"))

    hit_forbidden = [name for name in case.get("forbidden_tools") or [] if name in calls]
    checks.append(("forbidden_tools", not hit_forbidden, f"hit={hit_forbidden}; calls={calls}"))

    missing_numbers = [n for n in case.get("expected_numbers") or [] if str(n) not in reply]
    checks.append(("expected_numbers", not missing_numbers, f"missing={missing_numbers}"))

    hit_phrases = [p for p in case.get("forbidden_phrases") or [] if p in reply]
    checks.append(("forbidden_phrases", not hit_phrases, f"hit={hit_phrases}"))

    if case.get("must_ask_confirmation"):
        checks.append(("confirmation_card", pending is not None, f"pending_confirm={pending is not None}"))

    if "eligible" in gt:
        eligible = bool(gt["eligible"])
        ok = (pending is not None) if eligible else (pending is None)
        checks.append(
            (
                "ground_truth.eligible",
                ok,
                f"eligible={eligible}; pending_confirm={pending is not None}; reply={reply[:80]}",
            )
        )

    if gt.get("error_code"):
        ok = gt["error_code"] in call_errors
        checks.append(("ground_truth.error_code", ok, f"want={gt['error_code']}; got={call_errors}"))

    if gt.get("session_status"):
        want = gt["session_status"]
        got = data.get("session_status")
        pending_tool = str((pending or {}).get("tool") or "")
        # W11 B8 契约:handed_off 改为"确认建单成功后"才写库;工单确认卡挂起期间会话仍是
        # active,但转人工已进入"待确认"态。此处按与 app/eval/hard_assert.py 相同的口径,
        # 把"待确认转人工"视为 handed_off 已达成,不改 data/eval 的既有 ground_truth。
        pending_handoff = (
            want == "handed_off" and got == "active" and pending_tool == "handoff__create_ticket"
        )
        checks.append(
            (
                "ground_truth.session_status",
                got == want or pending_handoff,
                f"want={want}; got={got}; pending_tool={pending_tool or '-'}; "
                f"pending_handoff={pending_handoff}",
            )
        )

    citations = data.get("citations") or []
    bad_citations = [c.get("clause_id") for c in citations if c.get("clause_id") not in clause_ids]
    checks.append(("citations_real", not bad_citations, f"bad={bad_citations}; citations={[c.get('clause_id') for c in citations]}"))

    notes: list[str] = []
    if gt.get("ticket_created"):
        notes.append(
            "ticket_created:W2-C 只出工单确认卡(pending_confirm),Ticket 落库待 W3 确认流"
        )
    if gt.get("refund_created") is False and "refund__create" not in calls:
        notes.append("refund_created=false 已满足(未调用 refund__create)")

    return {
        "case": case,
        "http_status": response.status_code,
        "data": data,
        "checks": checks,
        "failed": [name for name, ok, _ in checks if not ok],
        "notes": notes,
    }


def main() -> int:
    print("=" * 96)
    print("W2-C 评测自检:cases_w1.json 10 条(⚠️ 全部 MOCK:FakeLLM 规则+模板,未接真实模型)")
    print(f"数据:{CASES_PATH}")
    ensure_data()
    clause_ids = kb_clause_ids()
    print(f"knowledge_item 真实 clause_id 数:{len(clause_ids)}")
    print("=" * 96)

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    client = TestClient(app)
    results: list[dict] = []
    for case in cases:
        result = run_case(client, case, clause_ids)
        results.append(result)
        passed = result["http_status"] == 200 and not result["failed"]
        detail = ""
        if result["http_status"] != 200:
            detail = f" HTTP {result['http_status']} {result['body']}"
        elif not passed:
            for name, ok, info in result["checks"]:
                if not ok:
                    detail += f"\n      ✗ {name}: {info}"
        print(f"[{'PASS' if passed else 'FAIL'}] {case['case_id']:<16s} {case['scene']}{detail}")

    print("-" * 96)
    for result in results:
        for note in result.get("notes") or []:
            print(f"  备注 {result['case']['case_id']}: {note}")

    passed_count = sum(1 for r in results if r["http_status"] == 200 and not r["failed"])
    total = len(results)
    print("-" * 96)
    print(f"结果:{passed_count}/{total} 通过(阈值 {PASS_THRESHOLD}/10,不改断言)")
    for result in results:
        if result["http_status"] == 200 and result["failed"]:
            print(f"  未通过 {result['case']['case_id']}:{result['failed']}")
        elif result["http_status"] != 200:
            print(f"  未通过 {result['case']['case_id']}:HTTP {result['http_status']}")
    print("=" * 96)
    if passed_count < PASS_THRESHOLD:
        print(f"未达标:{passed_count}/{total} < {PASS_THRESHOLD}")
        return 1
    print(f"达标:{passed_count}/{total} >= {PASS_THRESHOLD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
