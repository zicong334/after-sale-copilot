#!/usr/bin/env python
"""scripts/promote_badcase.py — W12-2:坏例 → eval case 草稿(不入集)。

用法(仓库根目录)::

    .venv\\Scripts\\python backend/scripts/promote_badcase.py --list
    .venv\\Scripts\\python backend/scripts/promote_badcase.py --id <badcase_id>
    .venv\\Scripts\\python backend/scripts/promote_badcase.py --check data/eval/drafts/<case_id>.json

产出
----
- ``data/eval/drafts/<case_id>.json``:单条 case 的 JSON 数组(17 字段,
  ``layer="regression"``),``ground_truth`` 取自坐席最终正确行为。
- ``data/eval/drafts/<case_id>.meta.json``:来源与治理说明(badcase_id、
  trace_id、生成时间、judge 来源、草稿 sha256)。
- 完成后把 ``badcase.status`` 置 ``promoted``、回填 ``eval_case_id`` 与
  ``promoted_at``;``status="ignored"`` 的坏例拒绝导出。

为什么不直接并入 ``data/eval/cases_v1.json``
-------------------------------------------
该文件冻结 48 条 + 分层配额(golden 19/adversarial 14/edge 10/regression 5),
且 regression 配额已被必造用例 ``CS-REG-001..005`` 占满。追加第 6 条会同时
撞"总数 48"和"配额精确相等"两条集级规则,替换任一既有用例则会丢必造 anchor。
因此草稿只落 ``drafts/``,入集必须走主会话的版本升级流程(改 VERSION.txt、
重排配额、更新 rubric),本脚本不做。

``--check`` 的验收口径(重要)
-----------------------------
官方 ``scripts/validate_eval_cases.py`` 对任何"非 10 条"的列表都会委托
``app.eval.validate_cases.validate_cases``,该函数强制 48 条 + 配额 + 必造 ID,
**单条草稿在它那里必然 exit 1**。所以 ``--check`` 的做法是:调用同一个官方
用例级校验器,把只与"整集规模"有关的错误(总数/配额/必造 anchor)显式隔离,
其余任一错误都算失败;并把隔离掉的那些逐条打印,便于人工复核。判定结果:
用例级错误 0 条 → exit 0。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent
_REPO_ROOT = _BACKEND_DIR.parent
for _path in (str(_SCRIPT_DIR), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from sqlalchemy import select  # noqa: E402

from app.eval.validate_cases import (  # noqa: E402
    TOOL_NAMES,
    validate_cases,
)
from app.models import BadCase, ChatMessage  # noqa: E402
from app.db import SessionLocal, utc_now  # noqa: E402

DEFAULT_OUT_DIR = _REPO_ROOT / "data" / "eval" / "drafts"
CASES_V1 = _REPO_ROOT / "data" / "eval" / "cases_v1.json"
_SEED_ORDER_RE = re.compile(r"^seed-order-0(0[1-9]|[12][0-9]|30)$")
_HANDOFF_REASONS = {
    "amount_dispute",
    "complaint",
    "unresolved_3rounds",
    "explicit_request",
    "step_limit",
    "security_risk",
    "other",
}

#: 只与"整集规模/配额/anchor"有关的集级错误前缀。``--check`` 会隔离这些,
#: 其余任何错误都是草稿本身不合格。
SET_LEVEL_PREFIXES = (
    "用例总数应为",
    "配额不符:",
    "缺少必造对抗用例 ",
    "缺少必造边界用例 ",
    "缺少必造回归用例 ",
)


def is_set_level_error(message: str) -> bool:
    """该错误是否只描述"整集规模"而非草稿用例本身。"""
    return any(message.startswith(prefix) for prefix in SET_LEVEL_PREFIXES)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _load_json_text(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def find_badcase(session: Any, selector: str) -> BadCase:
    """按完整 id 或唯一前缀取坏例;不存在/歧义都抛 SystemExit(报错文案给人工看)。"""
    rows = list(
        session.scalars(select(BadCase).where(BadCase.id == selector)).all()
    )
    if not rows:
        rows = list(
            session.scalars(
                select(BadCase)
                .where(BadCase.id.like(f"{selector}%"))
                .order_by(BadCase.created_at.asc())
            ).all()
        )
    if not rows:
        raise SystemExit(f"找不到坏例: {selector}")
    if len(rows) > 1:
        ids = ", ".join(row.id for row in rows[:10])
        raise SystemExit(f"前缀命中多条坏例,请给完整 id: {ids}")
    return rows[0]


def _last_user_message(session: Any, session_id: str | None) -> ChatMessage | None:
    if not session_id:
        return None
    return session.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id, ChatMessage.role == "user")
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    ).first()


def _ticket_session_id(session: Any, ticket_id: str) -> str | None:
    from app.models import Ticket

    ticket = session.get(Ticket, ticket_id)
    return ticket.session_id if ticket is not None else None


def _user_input_and_intent(session: Any, row: BadCase) -> tuple[str, str | None]:
    """取本轮用户原话与意图:优先业务库,其次快照转录,最后人工备注。"""
    session_id = _ticket_session_id(session, row.ticket_id)
    message = _last_user_message(session, session_id)
    if message is not None and (message.content or "").strip():
        return message.content.strip(), (message.intent or None)

    transcript = _load_json_text(row.transcript, [])
    if isinstance(transcript, list):
        for item in reversed(transcript):
            if isinstance(item, dict) and item.get("role") == "user":
                text = str(item.get("content") or "").strip()
                if text:
                    return text, None
    if (row.note or "").strip():
        return row.note.strip(), None
    return "坐席标记坏例(转录缺失,请人工补全 user_input)", None


def build_case(session: Any, row: BadCase) -> dict[str, Any]:
    """把一行 badcase 变成 17 字段的单条 eval case 草稿。"""
    case_id = f"regression_badcase_{row.id[:8]}"
    user_input, intent = _user_input_and_intent(session, row)
    tool_chain = _load_json_text(row.tool_chain, [])
    expected_tools: list[str] = []
    if isinstance(tool_chain, list):
        for item in tool_chain:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool")
            if item.get("ok") and tool in TOOL_NAMES and tool not in expected_tools:
                expected_tools.append(tool)

    handoff_reason = row.handoff_reason or "other"
    if handoff_reason not in _HANDOFF_REASONS:
        handoff_reason = "other"

    context: dict[str, Any] = {
        "badcase_id": row.id,
        "ticket_id": row.ticket_id,
        "trace_id": row.trace_id,
    }
    if row.order_id and _SEED_ORDER_RE.match(row.order_id):
        # 只有 seed 订单才写进 context:其它订单号会撞官方校验器。
        context["order_id"] = row.order_id

    answer_fact = (row.agent_final or "").strip() or None
    ground_truth: dict[str, Any] = {
        "handoff": True,
        "handoff_reason": handoff_reason,
    }
    if intent:
        ground_truth["intent"] = intent
    if answer_fact:
        ground_truth["answer_fact"] = answer_fact

    notes = (
        "由 promote_badcase.py 从坐席标记的坏例自动生成,仅作草稿,未并入 "
        "cases_v1.json;入集需主会话治理(版本升级 + 配额重排)。"
        f" judge 来源={row.judge_detail or 'unknown'}。"
    )
    if row.diff:
        notes += " 附坐席修改 diff(见 badcase.diff)。"
    if not expected_tools:
        notes += " 工具链中没有成功调用的注册工具,expected_tools 为空。"

    return {
        "case_id": case_id,
        "layer": "regression",
        "scene": f"坐席标记坏例回流 · {handoff_reason}",
        "user_input": user_input,
        "context": context,
        "ground_truth": ground_truth,
        "expected_tools": expected_tools,
        "forbidden_tools": [],
        "expected_numbers": [],
        "forbidden_phrases": [],
        "must_ask_confirmation": False,
        "expected_verdict": {"task_success": True, "handoff": True},
        "weight": 1.0,
        "repeat": 1,
        "rubric_version": "cs-v2",
        "source": f"badcase:{row.id}",
        "notes": notes,
    }


def _canonical_digest(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unique_case_id(case_id: str) -> str:
    """草稿 case_id 不得与已入集用例冲突(极端情况下加后缀)。"""
    if not CASES_V1.exists():
        return case_id
    try:
        existing = json.loads(CASES_V1.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - 已有集损坏时交给主会话
        return case_id
    if not isinstance(existing, list):
        return case_id
    used = {
        str(item.get("case_id"))
        for item in existing
        if isinstance(item, dict) and item.get("case_id")
    }
    if case_id not in used:
        return case_id
    suffix = 1
    while f"{case_id}_{suffix}" in used:
        suffix += 1
    return f"{case_id}_{suffix}"


def write_draft(row: BadCase, case: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """写 ``<case_id>.json`` 与 ``<case_id>.meta.json``(UTF-8,2 空格缩进)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    draft_path = out_dir / f"{case['case_id']}.json"
    meta_path = out_dir / f"{case['case_id']}.meta.json"

    draft_path.write_text(
        json.dumps([case], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    meta = {
        "badcase_id": row.id,
        "ticket_id": row.ticket_id,
        "merchant_id": row.merchant_id,
        "trace_id": row.trace_id,
        "case_id": case["case_id"],
        "generated_at_utc": _iso(utc_now()),
        "generator": "backend/scripts/promote_badcase.py",
        "draft_sha256": _canonical_digest(case),
        "judge_score": row.judge_score,
        "judge_detail": _load_json_text(row.judge_detail, None),
        "governance": (
            "草稿只落 data/eval/drafts/,不并入 cases_v1.json;入集需主会话按"
            "版本升级流程处理配额与必造用例。"
        ),
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return draft_path, meta_path


def mark_promoted(session: Any, row: BadCase, case_id: str) -> None:
    row.status = "promoted"
    row.eval_case_id = case_id
    row.promoted_at = utc_now()
    session.add(row)
    session.commit()


def check_draft(path: Path) -> int:
    """用例级校验单条草稿;集级错误显式隔离后仍可 exit 0(见模块 docstring)。"""
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"JSON 解析失败: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        print("草稿必须是只含一条 case 对象的 JSON 数组", file=sys.stderr)
        return 1

    case = data[0]
    errors = validate_cases(data)
    case_errors = [item for item in errors if not is_set_level_error(item)]
    set_errors = [item for item in errors if is_set_level_error(item)]

    if case_errors:
        print(f"校验失败:草稿用例级错误 {len(case_errors)} 条", file=sys.stderr)
        for item in case_errors:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print(f"OK: 草稿 {case.get('case_id')} 通过官方用例级校验(17 字段/类型/枚举/工具名/配额无关项)")
    print(
        "   集级错误 "
        f"{len(set_errors)} 条已隔离(单条草稿不可能满足 48 条全集约束,草稿不入集):"
    )
    for item in set_errors:
        print(f"     - {item}")
    print("   入集需主会话治理:改 VERSION.txt + 重排分层配额 + 更新 rubric 后另行评审。")
    return 0


def list_badcases(session: Any, limit: int = 20) -> int:
    rows = list(
        session.scalars(
            select(BadCase).order_by(BadCase.created_at.desc()).limit(limit)
        ).all()
    )
    if not rows:
        print("badcase 表为空(先在工作台「标记坏例」)")
        return 0
    print(f"{'badcase_id':<38}{'status':<10}{'trace':<12}{'judge':<7}note")
    for row in rows:
        print(
            f"{row.id:<38}{row.status:<10}{(row.trace_id or '-')[:10]:<12}"
            f"{('- ' if row.judge_score is None else f'{row.judge_score:.2f}'):<7}"
            f"{(row.note or '')[:40]}"
        )
    return 0


def promote(session: Any, selector: str, out_dir: Path) -> int:
    row = find_badcase(session, selector)
    if row.status == "ignored":
        print(f"坏例 {row.id} 已标记 ignored,拒绝导出", file=sys.stderr)
        return 1

    case = build_case(session, row)
    case["case_id"] = _unique_case_id(case["case_id"])

    series = validate_cases([case])
    case_errors = [item for item in series if not is_set_level_error(item)]
    if case_errors:
        print("草稿未通过用例级校验,拒绝落盘:", file=sys.stderr)
        for item in case_errors:
            print(f"  - {item}", file=sys.stderr)
        return 1

    draft_path, meta_path = write_draft(row, case, out_dir)
    already_promoted = row.status == "promoted" and row.eval_case_id == case["case_id"]
    mark_promoted(session, row, case["case_id"])

    print(f"OK: 坏例 {row.id} → 草稿 {draft_path}")
    print(f"   元数据 {meta_path}")
    print(
        f"   case_id={case['case_id']} layer=regression "
        f"expected_tools={case['expected_tools']}"
    )
    if already_promoted:
        print("   提示:该坏例此前已导出过同一 case_id,本次覆盖草稿并按原样回填。")
    print(f"   自检:python backend/scripts/promote_badcase.py --check {draft_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="W12-2 坏例 → eval case 草稿(写 data/eval/drafts/,不入集)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", help="坏例 id 或唯一前缀")
    group.add_argument("--list", action="store_true", help="列出最近坏例")
    group.add_argument("--check", metavar="DRAFT", help="校验草稿(用例级)并退出")
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help=f"草稿输出目录(默认 {DEFAULT_OUT_DIR})",
    )
    parser.add_argument("--limit", type=int, default=20, help="--list 返回条数")
    args = parser.parse_args(argv)

    if args.check:
        return check_draft(Path(args.check))

    out_dir = Path(args.out_dir)
    # 允许 --out-dir 相对仓库根书写(脚本可能在任意 cwd 执行)。
    if not out_dir.is_absolute():
        out_dir = (_REPO_ROOT / out_dir).resolve()

    session = SessionLocal()
    try:
        if args.list:
            return list_badcases(session, limit=max(1, args.limit))
        return promote(session, args.id, out_dir)
    finally:
        session.close()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main(sys.argv[1:]))
