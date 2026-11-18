"""test_audit.py — W7-5 审计链导出验收。

覆盖:
1. golden 退款 trace(TestClient + FakeLLM 密封)整条链路:感知→分类→检索→工具
   →确认→执行→回执;断言七段齐全、字段回读、缺失字段显式 null;
2. 按 refund_id 反查得到同一 case(trace ↔ refund 双向定位);
3. 进程内调 ``main(argv)`` 与 subprocess 跑 CLI:stdout 恰好一个 JSON、退出码 0;
4. 未知 trace:非 0 退出,stdout 不输出任何内容,错误只进 stderr;
5. 未确认的 trace:确认卡 pending、退款字段全 null、missing_stages 精确;
6. 政策问答 trace:无退款/无确认,但引用条款可由检索事件确定性回读。

说明:golden 链路必须先把 data/policies 装进临时库,否则图内 ``_clause()`` 查不到
knowledge_item,citations 会为空(导出脚本不编造条款)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.api.chat as chat_mod
from app.api import get_db
from app.graph import build as graph_build
from app.llm.base import FakeLLM
from app.main import app
from scripts.export_audit import STAGE_ORDER, build_audit_report, main as export_main
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
SCRIPT = BACKEND / "scripts" / "export_audit.py"
POLICIES_DIR = ROOT / "data" / "policies"

MERCHANT_ID = "seed-merchant-1"
CUSTOMER_REFUND = "seed-cust-010"
ELIGIBLE_TEXT = "3 天前收到的衣服有线头,是质量问题,我要退货退款"
REFUND_CONTEXT = {"order_id": "seed-order-020"}
POLICY_TEXT = "你们的退货政策规定质量问题几天内可以退?"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    """临时库灌 seed 业务数据 + data/policies 知识库(引用回读依赖它)。"""
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICIES_DIR))
        session.commit()


@pytest.fixture()
def ckpt(tmp_dir, monkeypatch) -> Path:
    """图断点库隔离到 .tmp:chat 与 confirm 都按运行期常量解析。"""
    path = tmp_dir / "checkpoints.db"
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", path)
    return path


@pytest.fixture()
def client(engine, seeded, ckpt, monkeypatch):
    """TestClient + FakeLLM 密封:显式替换 build_llm,并清掉可能存在的真实 key。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(chat_mod, "build_llm", lambda **kwargs: FakeLLM())

    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def golden_refund(client) -> dict:
    """跑一条 golden 退款 trace 并完成确认,返回 trace/refund/thread 标识。"""
    body = post_chat(client, ELIGIBLE_TEXT, context=REFUND_CONTEXT)
    assert body["pending_confirm"], "合格退款必须停在确认门"
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]
    resp = client.post(
        f"/api/c/confirm/{token}/confirm", json={"thread_id": thread_id}
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["decision"] == "confirmed"
    return {
        "trace_id": out["trace_id"],
        "refund_id": out["receipt"]["refund_request_id"],
        "thread_id": thread_id,
        "token": token,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def post_chat(
    client: TestClient,
    text: str,
    *,
    customer_id: str = CUSTOMER_REFUND,
    context: dict | None = None,
    session_id: str | None = None,
) -> dict:
    body = {
        "merchant_id": MERCHANT_ID,
        "customer_id": customer_id,
        "text": text,
        "context": context,
    }
    if session_id:
        body["session_id"] = session_id
    resp = client.post("/api/c/chat", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _db_path(engine) -> str:
    """engine 指向的 SQLite 文件绝对路径(传给 CLI 的 --db)。"""
    return str(engine.url.database)


def _cli_env() -> dict[str, str]:
    """子进程环境:临时目录指向 .tmp,清掉真实 LLM key / DATABASE_URL。"""
    env = os.environ.copy()
    tmp_dir = str(ROOT / ".tmp")
    env["TMP"] = tmp_dir
    env["TEMP"] = tmp_dir
    env["PYTHONIOENCODING"] = "utf-8"
    for key in ("PYTHONPATH", "LLM_API_KEY", "DATABASE_URL"):
        env.pop(key, None)
    return env


# ---------------------------------------------------------------------------
# 1. golden 退款 trace:七段齐全 + 回读字段
# ---------------------------------------------------------------------------


def test_golden_refund_audit_chain_complete(client, engine, golden_refund):
    with Session(engine) as db:
        report = build_audit_report(db, trace_id=golden_refund["trace_id"])

    case = report["case"]
    assert case["trace_id"] == golden_refund["trace_id"]
    assert case["refund_id"] == golden_refund["refund_id"]
    assert case["session_id"] == golden_refund["thread_id"]
    assert case["schema_version"] == 1
    # 缺失字段必须是显式 null,不能省略键
    assert "ticket_id" in case and case["ticket_id"] is None
    assert case["exported_at"].endswith("+00:00"), "导出时间必须是 ISO8601 UTC"

    integrity = report["integrity"]
    assert integrity["stages_present"] == list(STAGE_ORDER)
    assert integrity["missing_stages"] == []
    assert integrity["complete"] is True

    # 工具事件保留 payload(只存入参,不含返回结果,不编造)
    tools = [item["tool"] for item in report["tool_events"]]
    assert tools == ["shop__get_order", "policy__check_return", "refund__create"]
    check = next(t for t in report["tool_events"] if t["tool"] == "policy__check_return")
    assert check["payload"]["args"] == {
        "order_status": "delivered",
        "reason": "质量问题",
        "days_since_delivery": 3,
    }
    assert check["ok"] is True
    assert check["created_at"].endswith("+00:00")

    # 引用条款:clause_id + version 都必须回读自 knowledge_item
    assert report["citations"], "golden 退款必须能回读到引用条款"
    assert all(c["clause_id"] and c["version"] for c in report["citations"])
    assert any(
        c["clause_id"] == "RP-03" and c["version"] == "v1"
        for c in report["citations"]
    )

    # 确认卡:pending → confirmed 迁移 + token
    confirm = report["confirm"]
    assert confirm["token"] == golden_refund["token"]
    assert confirm["status"] == "confirmed"
    assert confirm["decided_by"] is None  # 自动执行,无人工坐席,不编造
    transitions = confirm["transitions"]
    assert len(transitions) == 2
    assert transitions[0]["from"] is None and transitions[0]["to"] == "pending"
    assert transitions[0]["at"].endswith("+00:00")
    assert transitions[1]["from"] == "pending" and transitions[1]["to"] == "confirmed"
    assert transitions[1]["at"].endswith("+00:00")

    # 退款回执:回读值齐全 + verified
    refund = report["refund"]
    assert refund["refund_id"] == golden_refund["refund_id"]
    assert refund["amount_cents"] > 0
    assert refund["status"] == "申请"
    receipt = refund["receipt"]
    assert receipt["kind"] == "refund"
    assert receipt["refund_request_id"] == golden_refund["refund_id"]
    assert receipt["order_id"] == "seed-order-020"
    assert receipt["amount_cents"] == refund["amount_cents"]
    assert receipt["occurred_at_utc"].endswith("+00:00")
    assert receipt["trace_id"] == golden_refund["trace_id"]
    assert receipt["verified"] is True
    assert receipt["idempotent_hit"] is False

    # 执行者:模型名 / 规则路径 / 坐席
    actors = report["actors"]
    assert actors["model"] == "fake-rule-v1 (MOCK)"
    assert actors["rules"] == ["rule"]
    assert "agent" in actors and actors["agent"] is None

    # 感知层:用 user 消息确定性重放,字段完整
    assert len(report["percepts"]) == 1
    percept = report["percepts"][0]
    assert percept["text"] == ELIGIBLE_TEXT
    assert percept["truncated"] is False
    assert percept["original_length"] is None
    assert isinstance(percept["flags"], list)
    assert percept["created_at"].endswith("+00:00")

    # 编排事件:分类 + 确认执行
    assert [e["event"] for e in report["events"]] == [
        "router__classify",
        "graph__confirm_executed",
    ]

    # 时间线覆盖七段标记
    stages = {item["stage"] for item in report["timeline"]}
    assert stages >= set(STAGE_ORDER)
    # 时间线按时间升序(ISO8601 UTC 字符串可直接比较)
    timeline_at = [item["at"] for item in report["timeline"]]
    assert timeline_at == sorted(timeline_at)


# ---------------------------------------------------------------------------
# 2. refund_id 反查:trace ↔ refund 双向定位一致
# ---------------------------------------------------------------------------


def test_refund_id_lookup_returns_same_case(client, engine, golden_refund, capsys):
    with Session(engine) as db:
        by_trace = build_audit_report(db, trace_id=golden_refund["trace_id"])
        by_refund = build_audit_report(db, refund_id=golden_refund["refund_id"])

    assert by_refund["case"]["trace_id"] == by_trace["case"]["trace_id"]
    assert by_refund["case"]["refund_id"] == by_trace["case"]["refund_id"]
    assert by_refund["integrity"]["complete"] is True
    # 除导出时间外,两种入口应得到同一份 case
    assert (
        {k: v for k, v in by_refund["case"].items() if k != "exported_at"}
        == {k: v for k, v in by_trace["case"].items() if k != "exported_at"}
    )
    # CLI 也必须支持 --refund-id 入口,且与 --trace-id 得到同一 case
    code = export_main(
        ["--refund-id", golden_refund["refund_id"], "--db", _db_path(engine)]
    )
    assert code == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["case"]["trace_id"] == by_trace["case"]["trace_id"]
    assert cli_report["integrity"]["complete"] is True


# ---------------------------------------------------------------------------
# 3. CLI:进程内 main() 与 subprocess 都只输出一个 JSON 对象
# ---------------------------------------------------------------------------


def test_main_prints_single_json_in_process(client, engine, golden_refund, capsys):
    code = export_main(
        [
            "--trace-id",
            golden_refund["trace_id"],
            "--db",
            _db_path(engine),
            "--pretty",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)  # 整段 stdout 必须能整体解析
    assert report["case"]["trace_id"] == golden_refund["trace_id"]
    assert report["integrity"]["complete"] is True


def _run_cli(tmp_dir: Path, argv: list[str]) -> tuple[int, bytes, bytes]:
    """跑 CLI 子进程:stdout/stderr 重定向到真实文件。

    DSH 沙盒下 ``_winapi.CreatePipe`` 会被拒绝(WinError 5),因此不能
    用 capture_output=True 的管道模式,改为写文件后回读。
    """
    out_path = tmp_dir / "cli_stdout.json"
    err_path = tmp_dir / "cli_stderr.txt"
    with open(out_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *argv],
            cwd=str(BACKEND),
            env=_cli_env(),
            stdout=out_fh,
            stderr=err_fh,
            timeout=180,
        )
    return proc.returncode, out_path.read_bytes(), err_path.read_bytes()


def test_cli_subprocess_prints_single_json(client, engine, golden_refund, tmp_dir):
    code, stdout, stderr = _run_cli(
        tmp_dir,
        ["--trace-id", golden_refund["trace_id"], "--db", _db_path(engine)],
    )
    assert code == 0, stderr.decode("utf-8", "replace")
    report = json.loads(stdout.decode("utf-8"))
    assert report["case"]["trace_id"] == golden_refund["trace_id"]
    assert report["case"]["refund_id"] == golden_refund["refund_id"]
    assert report["integrity"]["complete"] is True


def test_cli_unknown_trace_fails_cleanly(client, engine, tmp_dir):
    code, stdout, stderr = _run_cli(
        tmp_dir,
        [
            "--trace-id",
            "00000000000000000000000000000000",
            "--db",
            _db_path(engine),
        ],
    )
    assert code != 0
    assert stdout.strip() == b"", "失败时 stdout 不得有任何内容"
    assert "导出失败" in stderr.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 4. 未确认 trace:确认卡 pending,退款/执行/回执显式缺失
# ---------------------------------------------------------------------------


def test_pending_confirm_trace_marks_missing_stages_with_nulls(client, engine):
    body = post_chat(client, ELIGIBLE_TEXT, context=REFUND_CONTEXT)
    assert body["pending_confirm"], "首轮应停在确认门"
    with Session(engine) as db:
        report = build_audit_report(db, trace_id=body["trace_id"])

    confirm = report["confirm"]
    assert confirm["token"] == body["pending_confirm"]["confirm_token"]
    assert confirm["status"] == "pending"
    assert len(confirm["transitions"]) == 1
    assert confirm["transitions"][0]["from"] is None
    assert confirm["transitions"][0]["to"] == "pending"
    assert "decided_by" in confirm and confirm["decided_by"] is None

    # 未执行:退款段全部显式 null,不省略
    assert report["refund"] == {
        "refund_id": None,
        "amount_cents": None,
        "status": None,
        "receipt": None,
    }
    assert "refund_id" in report["case"] and report["case"]["refund_id"] is None
    assert report["integrity"]["missing_stages"] == ["execute", "receipt"]
    assert report["integrity"]["complete"] is False
    assert "agent" in report["actors"] and report["actors"]["agent"] is None
    # 检索阶段已发生,引用条款仍可回读
    assert report["citations"]


# ---------------------------------------------------------------------------
# 5. 政策问答 trace:无退款/无确认,引用由 policy__search 事件确定性回读
# ---------------------------------------------------------------------------


def test_policy_trace_replays_citations_and_nulls_refund(client, engine):
    body = post_chat(client, POLICY_TEXT)
    assert body["intent"] == "policy_qa", body
    with Session(engine) as db:
        report = build_audit_report(db, trace_id=body["trace_id"])

    assert report["confirm"] == {
        "token": None,
        "status": None,
        "transitions": [],
        "decided_by": None,
    }
    assert report["refund"]["refund_id"] is None
    assert report["refund"]["receipt"] is None
    assert any(t["tool"] == "policy__search" for t in report["tool_events"])
    assert report["citations"], "policy__search 事件应能确定性回读出引用条款"
    assert all(c["clause_id"] and c["version"] for c in report["citations"])
    missing = set(report["integrity"]["missing_stages"])
    assert {"confirm", "execute", "receipt"} <= missing
    assert report["integrity"]["complete"] is False
