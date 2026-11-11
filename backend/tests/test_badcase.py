"""W12-2 坏例回流测试:标记 → 草稿 → 用例级校验。

覆盖三件事:

1. ``POST /api/b/badcases`` 把现场快照(trace/转录/工具链/diff/judge)落库,
   缺 judge 时如实记 ``source=unavailable`` 而不伪造分数。
2. ``scripts/promote_badcase.py`` 能从 badcase 行生成 17 字段 regression 草稿,
   写 ``drafts/`` 并把坏例置 promoted;``--check`` 走官方用例级校验器。
3. ``data/eval/cases_v1.json`` 的字节与 mtime 全程不变(坏例不进集)。

关于验收口径:官方 ``scripts/validate_eval_cases.py`` 对任何"非 10 条"列表都
强制 48 条全集校验,单条草稿必然 exit 1;本文件同时断言"它的全部错误都是
集级(总数/配额/必造 anchor)",以此证明草稿用例本身没有规则问题。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import agent as agent_api
from app.api import get_db
from app.main import app
from app.models import (
    BadCase,
    ChatMessage,
    ChatSession,
    Merchant,
    Order,
    Ticket,
    TicketMessage,
)
from app.tools.events import ToolEvent

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CASES_V1 = _REPO_ROOT / "data" / "eval" / "cases_v1.json"
_PROMOTE = _REPO_ROOT / "backend" / "scripts" / "promote_badcase.py"
_VALIDATE = _REPO_ROOT / "backend" / "scripts" / "validate_eval_cases.py"

#: 与 ``promote_badcase.SET_LEVEL_PREFIXES`` 同口径的集级错误前缀。
_SET_LEVEL_PREFIXES = (
    "用例总数应为",
    "配额不符:",
    "缺少必造对抗用例 ",
    "缺少必造边界用例 ",
    "缺少必造回归用例 ",
)

TRACE_1 = "trace-w12b-0001"
TRACE_2 = "trace-w12b-0002"


@pytest.fixture()
def client(engine):
    """把 get_db 指向测试 engine(与 test_api.py 同一范式)。"""

    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    c = TestClient(app)
    yield c
    c.close()
    app.dependency_overrides.clear()
    agent_api._SUGGEST_CACHE.clear()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _seed(engine) -> dict[str, str]:
    """造一条"已转人工"的工单现场:聊天转录 + 工具链 + 坐席最终回复。"""
    now = datetime(2026, 11, 12, 3, 0, 0, tzinfo=timezone.utc)
    with Session(engine) as s:
        s.add(Merchant(id="m-1", name="测试店"))
        s.flush()  # 无 ORM relationship,靠 flush 保证父行先落,避免 FK 约束失败
        s.add(
            Order(
                id="seed-order-003",
                merchant_id="m-1",
                customer_id="cust-1",
                status="created",
                total_cents=259900,
                currency="CNY",
            )
        )
        s.flush()
        s.add(
            ChatSession(
                id="cs-1",
                merchant_id="m-1",
                customer_id="cust-1",
                status="handed_off",
                created_at=now,
            )
        )
        s.flush()
        s.add_all(
            [
                ChatMessage(
                    id="cm-1",
                    session_id="cs-1",
                    role="user",
                    content="我要退款,这个订单一直不发货",
                    trace_id=TRACE_1,
                    intent="refund",
                    created_at=now,
                ),
                ChatMessage(
                    id="cm-2",
                    session_id="cs-1",
                    role="assistant",
                    content="已为您核实,转人工同事继续处理。",
                    trace_id=TRACE_2,
                    created_at=now + timedelta(seconds=1),
                ),
            ]
        )
        s.flush()
        s.add(
            Ticket(
                id="tk-1",
                merchant_id="m-1",
                customer_id="cust-1",
                session_id="cs-1",
                order_id="seed-order-003",
                handoff_reason="complaint",
                status="queued",
                summary="退款一直不发货",
                created_at=now + timedelta(seconds=2),
            )
        )
        s.flush()
        s.add_all(
            [
                TicketMessage(
                    id="tm-1",
                    ticket_id="tk-1",
                    author_type="agent",
                    content="已核实,今天为您退款 2599 元。",
                    is_internal=False,
                    created_at=now + timedelta(minutes=1),
                ),
                TicketMessage(
                    id="tm-2",
                    ticket_id="tk-1",
                    author_type="agent",
                    content="内部备注:客户已催两次",
                    is_internal=True,
                    created_at=now + timedelta(minutes=2),
                ),
            ]
        )
        s.add_all(
            [
                ToolEvent(
                    trace_id=TRACE_1,
                    step=0,
                    tool_name="shop__get_order",
                    ok=True,
                    payload=json.dumps({"args": {"order_id": "seed-order-003"}}),
                    ts=now,
                ),
                ToolEvent(
                    trace_id=TRACE_1,
                    step=1,
                    tool_name="handoff__create_ticket",
                    ok=True,
                    payload=json.dumps({"args": {"reason": "complaint"}}),
                    ts=now + timedelta(seconds=1),
                ),
                # 编排事件(payload 带 event 键)不是工具调用,必须被过滤。
                ToolEvent(
                    trace_id=TRACE_2,
                    step=0,
                    tool_name="router_failover",
                    ok=False,
                    payload=json.dumps({"event": "router_failover", "to": "real"}),
                    ts=now + timedelta(seconds=2),
                ),
            ]
        )
        s.commit()

    # 建议原文来自工作台进程内缓存(服务端真值);故意与坐席最终回复不同,
    # 这样 diff 分支才有内容可断言。
    agent_api._SUGGEST_CACHE["tk-1"] = {
        "suggested_reply": "您好,已核实订单,退款 2599 元将在 1-3 个工作日到账。",
        "summary": "退款",
    }
    return {"ticket_id": "tk-1", "trace_id": TRACE_1}


def _post(client, payload: dict):
    return client.post("/api/b/badcases", json=payload)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_create_badcase_snapshot(client, engine, tmp_dir):
    _seed(engine)
    before = (_sha256(_CASES_V1), _CASES_V1.stat().st_mtime_ns)

    r = _post(client, {"ticket_id": "tk-1", "note": "答非所问", "service_id": "agent-1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["status"] == "new"
    assert data["ticket_id"] == "tk-1"
    # trace 来自聊天消息(去重)与工具事件;前端没传也应有
    assert TRACE_1 in data["trace_ids"] and TRACE_2 in data["trace_ids"]
    assert data["transcript_entries"] == 4  # 2 条聊天 + 2 条工单消息
    assert data["tool_chain_entries"] == 2  # 编排事件被过滤
    assert data["judge_score"] is None
    assert data["judge_source"] == "unavailable"
    # 建议缓存与坐席最终回复不同 → 有 diff
    assert data["has_diff"] is True

    with Session(engine) as s:
        row = s.scalars(select(BadCase)).one()
        assert row.merchant_id == "m-1"
        assert row.order_id == "seed-order-003"
        assert row.handoff_reason == "complaint"
        assert row.trace_id in (TRACE_1, TRACE_2)
        transcript = json.loads(row.transcript)
        assert [item["role"] for item in transcript] == [
            "user",
            "assistant",
            "agent",
            "agent",
        ]
        assert transcript[-1]["internal"] is True
        chain = json.loads(row.tool_chain)
        assert [item["tool"] for item in chain] == [
            "shop__get_order",
            "handoff__create_ticket",
        ]
        assert all(item["ok"] for item in chain)
        assert row.suggestion == "您好,已核实订单,退款 2599 元将在 1-3 个工作日到账。"
        assert "内部备注" not in (row.agent_final or "")
        assert "内部备注" not in (row.diff or "")  # 内部备注不进 diff
        assert "ai_suggestion" in (row.diff or "")
        # 不伪造 judge
        assert json.loads(row.judge_detail)["source"] == "unavailable"

    after = (_sha256(_CASES_V1), _CASES_V1.stat().st_mtime_ns)
    assert after == before


def test_create_badcase_accepts_injected_judge(client, engine):
    _seed(engine)
    r = _post(
        client,
        {
            "ticket_id": "tk-1",
            "judge": {"score": 0.25, "detail": {"source": "eval_run", "run_id": "r-1"}},
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["judge_score"] == 0.25
    assert data["judge_source"] == "eval_run"

    with Session(engine) as s:
        row = s.scalars(select(BadCase)).one()
        detail = json.loads(row.judge_detail)
        assert detail["source"] == "eval_run"
        assert detail["run_id"] == "r-1"


def test_create_badcase_rejects_unknown_ticket(client, engine):
    _seed(engine)
    r = _post(client, {"ticket_id": "tk-missing"})
    assert r.status_code == 404
    with Session(engine) as s:
        assert s.scalars(select(BadCase)).all() == []


class _Proc:
    """subprocess 结果的最小投影(本机沙箱禁用匿名管道,只能用文件重定向)。"""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run(args: list[str], cwd: Path, env_extra: dict[str, str]) -> _Proc:
    """跑子进程并把 stdout/stderr 落到文件。

    本机 DSH 沙箱禁止 ``CreatePipe``(``WinError 5``),``capture_output=True``
    会直接失败;用文件句柄重定向绕开管道。注意日志目录用 ``Path.mkdir`` 手建:
    ``tempfile.mkdtemp`` 会创建沙箱不可写的子目录(实测 ``PermissionError``)。
    """
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.update(env_extra)
    base = Path(env_extra.get("TMP") or (_REPO_ROOT / ".tmp" / "w12b"))
    log_dir = base / f"proc_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = log_dir / "out.txt", log_dir / "err.txt"
    with out_path.open("w", encoding="utf-8") as out_fh, err_path.open(
        "w", encoding="utf-8"
    ) as err_fh:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out_fh,
            stderr=err_fh,
            timeout=180,
        )
    return _Proc(
        proc.returncode,
        out_path.read_text(encoding="utf-8", errors="replace"),
        err_path.read_text(encoding="utf-8", errors="replace"),
    )


def test_promote_draft_then_case_level_validate(client, engine, tmp_dir):
    _seed(engine)
    before = (_sha256(_CASES_V1), _CASES_V1.stat().st_mtime_ns)

    r = _post(
        client,
        {"ticket_id": "tk-1", "note": "应直接退款并说明时效", "service_id": "agent-1"},
    )
    assert r.status_code == 201, r.text
    badcase_id = r.json()["data"]["badcase_id"]

    database = engine.url.database
    assert database, "测试 engine 必须是文件型 sqlite"
    db_url = "sqlite:///" + Path(database).as_posix()
    out_dir = Path(tmp_dir) / "drafts"
    env_extra = {
        "DATABASE_URL": db_url,
        "TMP": str(Path(tmp_dir) / "tmp"),
        "TEMP": str(Path(tmp_dir) / "tmp"),
        "PYTHONIOENCODING": "utf-8",
    }
    Path(env_extra["TMP"]).mkdir(parents=True, exist_ok=True)

    promoted = _run(
        [sys.executable, str(_PROMOTE), "--id", badcase_id, "--out-dir", str(out_dir)],
        cwd=_REPO_ROOT,
        env_extra=env_extra,
    )
    assert promoted.returncode == 0, promoted.stdout + promoted.stderr
    assert "layer=regression" in promoted.stdout

    draft_path = out_dir / f"regression_badcase_{badcase_id[:8]}.json"
    assert draft_path.exists(), sorted(p.name for p in out_dir.iterdir())
    case = json.loads(draft_path.read_text(encoding="utf-8"))[0]

    # 17 字段齐全,不多不少
    required = {
        "case_id",
        "layer",
        "scene",
        "user_input",
        "context",
        "ground_truth",
        "expected_tools",
        "forbidden_tools",
        "expected_numbers",
        "forbidden_phrases",
        "must_ask_confirmation",
        "expected_verdict",
        "weight",
        "repeat",
        "rubric_version",
        "source",
        "notes",
    }
    assert set(case) == required
    assert case["layer"] == "regression"
    assert case["case_id"].startswith("regression_badcase_")
    assert case["rubric_version"] == "cs-v2"
    assert case["source"] == f"badcase:{badcase_id}"
    assert case["user_input"] == "我要退款,这个订单一直不发货"
    assert case["context"]["badcase_id"] == badcase_id
    assert case["context"]["order_id"] == "seed-order-003"
    assert case["ground_truth"]["answer_fact"] == "已核实,今天为您退款 2599 元。"
    assert case["ground_truth"]["handoff"] is True
    assert case["ground_truth"]["intent"] == "refund"
    assert case["expected_tools"] == ["shop__get_order", "handoff__create_ticket"]
    assert case["expected_verdict"] == {"task_success": True, "handoff": True}
    # 入集需治理,notes 必须写明
    assert "入集需主会话治理" in case["notes"]

    # 坏例被置 promoted 并回填 case_id
    with Session(engine) as s:
        row = s.get(BadCase, badcase_id)
        assert row.status == "promoted"
        assert row.eval_case_id == case["case_id"]
        assert row.promoted_at is not None

    # 用例级校验:官方校验器隔离集级错误后 exit 0
    checked = _run(
        [sys.executable, str(_PROMOTE), "--check", str(draft_path)],
        cwd=_REPO_ROOT,
        env_extra=env_extra,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "通过官方用例级校验" in checked.stdout
    assert "集级错误" in checked.stdout

    # 官方 validate_eval_cases.py 对单条草稿必然 exit 1,但**所有**错误都是集级
    official = _run(
        [sys.executable, str(_VALIDATE), str(draft_path)],
        cwd=_REPO_ROOT,
        env_extra=env_extra,
    )
    assert official.returncode == 1, official.stdout + official.stderr
    assert "校验失败" in official.stderr
    detail_lines = [
        line.strip()[2:].strip()
        for line in official.stderr.splitlines()
        if line.strip().startswith("- ")
    ]
    assert detail_lines, official.stderr
    for message in detail_lines:
        assert message.startswith(_SET_LEVEL_PREFIXES), message

    # 集级错误数量可复算:总数 1 条 + 配额 5 条 + 必造 anchor 15 条 = 21
    assert len(detail_lines) == 1 + len(("golden", "adversarial", "edge", "regression")) + 15

    # cases_v1.json 字节与 mtime 双证未变
    after = (_sha256(_CASES_V1), _CASES_V1.stat().st_mtime_ns)
    assert after == before


def test_promote_refuses_ignored(client, engine, tmp_dir):
    _seed(engine)
    r = _post(client, {"ticket_id": "tk-1"})
    badcase_id = r.json()["data"]["badcase_id"]
    with Session(engine) as s:
        row = s.get(BadCase, badcase_id)
        row.status = "ignored"
        s.commit()

    database = engine.url.database
    db_url = "sqlite:///" + Path(database).as_posix()
    proc = _run(
        [sys.executable, str(_PROMOTE), "--id", badcase_id, "--out-dir", str(Path(tmp_dir) / "drafts")],
        cwd=_REPO_ROOT,
        env_extra={"DATABASE_URL": db_url, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 1
    assert "ignored" in proc.stdout + proc.stderr


def test_promote_check_rejects_broken_case(tmp_dir):
    """--check 对用例级问题必须 exit 1(证明隔离逻辑没有放水)。"""
    bad = {
        "case_id": "regression_badcase_broken",
        "layer": "regression",
        "scene": "坏例",
        "user_input": "x",
        "context": {},
        "ground_truth": {"handoff": True, "handoff_reason": "complaint", "intent": "bad_intent"},  # noqa: E501
        "expected_tools": ["not_a_tool"],
        "forbidden_tools": [],
        "expected_numbers": [],
        "forbidden_phrases": [],
        "must_ask_confirmation": False,
        "expected_verdict": {"task_success": True, "handoff": True},
        "weight": 1.0,
        "repeat": 1,
        "rubric_version": "cs-v2",
        "source": "badcase:x",
        "notes": "",
    }
    path = Path(tmp_dir) / "broken.json"
    path.write_text(json.dumps([bad], ensure_ascii=False), encoding="utf-8")
    proc = _run(
        [sys.executable, str(_PROMOTE), "--check", str(path)],
        cwd=_REPO_ROOT,
        env_extra={"PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "非法工具名" in proc.stdout + proc.stderr
