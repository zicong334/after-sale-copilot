"""demo_w2.py — W2-C 三场景命令行演示(全部 MOCK,不接真实模型)。

运行(项目根目录):
    .venv\\Scripts\\python backend/scripts/demo_w2.py

演示内容(数据取自 data/eval/cases_w1.json 的真实 user_input):
1. 政策问答:answer + [n] 引用 + citations 映射(引用 clause_id 来自 knowledge_item);
2. 订单判责(符合):shop__get_order → policy__check_return → refund__create 出**确认卡**;
3. 订单判责(超期):拒绝受理 + 引用条款,零写操作;
4. 转人工:handoff__create_ticket 出确认卡 + 会话 handed_off;同一会话再发消息被入口拦截,
   AI 不再回复(打印 ai_replied=False 与拦截 trace 事件)。

⚠️ MOCK:回复全部来自 ``app.llm.base.FakeLLM`` 的规则分类 + 模板;W4-C 才接真实模型。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import KnowledgeItem, Merchant, Order  # noqa: E402
from app.trace import trace_events  # noqa: E402
from scripts.load_policies import parse_policies, upsert_policies  # noqa: E402
from scripts.seed import seed  # noqa: E402

MERCHANT_ID = "seed-merchant-1"
CASES_PATH = ROOT / "data" / "eval" / "cases_w1.json"
POLICY_DIR = ROOT / "data" / "policies"

LINE = "=" * 86


def ensure_data() -> None:
    """dev.db 缺 seed/政策时补齐(幂等),保证演示可复现。"""
    with SessionLocal() as session:
        merchant = session.get(Merchant, MERCHANT_ID)
        orders = session.scalar(select(func.count()).select_from(Order)) or 0
        if merchant is None or orders == 0:
            counts = seed(session)
            print(f"[准备] 已执行 seed:{counts}")
        knowledge = session.scalar(select(func.count()).select_from(KnowledgeItem)) or 0
        if knowledge == 0:
            result = upsert_policies(session, parse_policies(POLICY_DIR))
            session.commit()
            print(f"[准备] 已导入政策:{result}")


def load_cases() -> dict[str, dict]:
    return {c["case_id"]: c for c in json.loads(CASES_PATH.read_text(encoding="utf-8"))}


def customer_for(context: dict) -> str:
    """演示用的当前客户:上下文指定优先,否则用订单归属,默认 seed-cust-001。"""
    if context.get("current_customer_id"):
        return context["current_customer_id"]
    order_id = context.get("order_id")
    if order_id:
        with SessionLocal() as session:
            order = session.get(Order, order_id)
            if order is not None:
                return order.customer_id
    return "seed-cust-001"


def print_trace(trace_id: str) -> None:
    print(f"  trace_id = {trace_id}")
    with SessionLocal() as session:
        for event in trace_events(session, trace_id):
            payload = json.loads(event.payload)
            detail = {k: v for k, v in payload.items() if k != "event"}
            text = json.dumps(detail, ensure_ascii=False)
            if len(text) > 110:
                text = text[:110] + "…"
            print(f"  step{event.step:02d} {event.tool_name:24s} ok={str(event.ok):5s} {text}")


def print_citations(data: dict) -> None:
    if not data["citations"]:
        return
    for citation in data["citations"]:
        snippet = str(citation["snippet"]).replace("\n", " ")[:70]
        print(f"  引用[{citation['index']}] {citation['clause_id']} ({citation['version']}):{snippet}")


def print_confirmation(data: dict, note: str = "") -> None:
    card = data.get("pending_confirm")
    if not card:
        print("  确认卡:无(本场景无写操作)")
        return
    print("  确认卡(待人工/客户确认后才真正执行):")
    token = str(card.get("confirm_token") or "")
    if len(token) > 16:
        token = token[:12] + "…"
    print(f"    what            : {card.get('what')}")
    print(f"    impact          : {card.get('impact')}")
    print(f"    irreversibility : {card.get('irreversibility')}")
    print(f"    alternatives    : {card.get('alternatives')}")
    print(f"    confirm_token   : {token}")
    if note:
        print(f"    说明            : {note}")


def run_turn(
    client: TestClient,
    *,
    title: str,
    case: dict,
    session_id: str | None = None,
    customer_id: str | None = None,
) -> dict:
    context = dict(case.get("context") or {})
    customer = customer_id or customer_for(context)
    print(LINE)
    print(f"【{title}】case_id={case['case_id']} 场景={case['scene']}")
    print(f"  用户({customer}):{case['user_input']}")
    body = {
        "merchant_id": MERCHANT_ID,
        "customer_id": customer,
        "text": case["user_input"],
        "context": context,
    }
    if session_id:
        body["session_id"] = session_id
    response = client.post("/api/c/chat", json=body)
    data = response.json()
    if response.status_code != 200:
        print(f"  请求失败:{response.status_code} {data}")
        return data
    print(
        f"  判定:intent={data['intent']} conf={data['confidence']} "
        f"path={data['classification_path']} reason={data['intent_reason']}"
    )
    print(f"  ai_replied={data['ai_replied']} session_status={data['session_status']}")
    print_trace(data["trace_id"])
    print(f"  工具调用:{[c['name'] + ('(ok)' if c['ok'] else '(fail)') for c in data['tool_calls']]}")
    print("  最终回复:")
    for line in data["reply"].splitlines():
        print(f"    {line}")
    print_citations(data)
    return data


def main() -> int:
    print(LINE)
    print("W2-C 演示:感知层 + 会话薄路由 + FakeLLM 三场景")
    print("⚠️ 本演示全部为 MOCK(FakeLLM 规则+模板),未接真实模型、未发外部网络请求。")
    ensure_data()
    cases = load_cases()
    client = TestClient(app)

    # 1) 政策问答
    policy_case = cases["CS-POLICY-001"]
    run_turn(
        client,
        title="场景一:政策问答(引用条款 + 数字)",
        case=policy_case,
    )

    # 2) 订单判责 —— 符合政策:出退款确认卡
    eligible_case = cases["CS-REFUND-004"]
    eligible = run_turn(
        client,
        title="场景二:订单判责(符合 → 退款确认卡,尚未真正退款)",
        case=eligible_case,
    )
    if eligible.get("pending_confirm"):
        print_confirmation(
            eligible,
            note="W2-C 只到确认卡;refund_request 落库要等 W3 确认流执行 execute()。",
        )

    # 3) 订单判责 —— 超期:拒绝 + 零写操作
    overdue_case = cases["CS-REFUND-003"]
    overdue = run_turn(
        client,
        title="场景三:订单判责(超期 → 拒绝受理,零写操作)",
        case=overdue_case,
    )
    write_tools = [c for c in overdue.get("tool_calls", []) if c["name"] == "refund__create"]
    print(f"  写操作检查:refund__create 调用次数={len(write_tools)}(应为 0)")

    # 4) 转人工 + 转人工后再发消息被拦截
    handoff_case = cases["CS-HANDOFF-002"]
    handoff = run_turn(
        client,
        title="场景四:转人工(工单确认卡 + 会话 handed_off)",
        case=handoff_case,
    )
    print_confirmation(
        handoff,
        note="W2-C 出工单确认卡;Ticket(queued) 落库由 W3 确认流执行 handoff.execute()。",
    )
    if handoff.get("session_id"):
        follow = {
            "case_id": "CS-HANDOFF-002+1",
            "scene": "转人工后继续发消息",
            "user_input": "我再补充一句:地址改成公司前台",
            "context": {},
        }
        blocked = run_turn(
            client,
            title="场景五:转人工后再次进线(AI 必须停止回复)",
            case=follow,
            session_id=handoff["session_id"],
            customer_id=customer_for(dict(handoff_case.get("context") or {})),
        )
        print(f"  拦截检查:ai_replied={blocked.get('ai_replied')} "
              f"intent={blocked.get('intent')} 工具调用={blocked.get('tool_calls')}")

    print(LINE)
    print("演示结束:4 段场景 + 1 段转人工拦截,均通过 HTTP 薄路由 /api/c/chat 完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
