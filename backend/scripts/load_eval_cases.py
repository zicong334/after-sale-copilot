"""load_eval_cases.py — 读 cases JSON → 校验 → upsert 进 eval_case 表(按 case_id 幂等)。

用法:
    python backend/scripts/load_eval_cases.py [data/eval/cases_w1.json]

- 连库走 app.db.get_database_url():默认 backend/dev.db,可用 DATABASE_URL 环境变量覆盖(测试用)。
- 幂等:case_id 已存在 → 更新 17 字段并恢复 deleted_at;不存在 → 插入新行。
- 校验失败(退出码 1)时不写库。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 把 scripts/ 与 backend/ 加入 sys.path,保证 `import validate_eval_cases` 与 `from app...` 可用
_SCRIPTS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPTS_DIR.parent
for _p in (_SCRIPTS_DIR, _BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import create_db_engine
from app.models import EvalCase

import validate_eval_cases as vc  # noqa: E402


def upsert_cases(session: Session, cases: list[dict]) -> dict[str, int]:
    """按 case_id 幂等 upsert,返回 {inserted, updated, total}。"""
    inserted = 0
    updated = 0
    for case in cases:
        existing = session.scalar(
            select(EvalCase).where(EvalCase.case_id == case["case_id"])
        )
        if existing is None:
            session.add(EvalCase(**case))
            inserted += 1
        else:
            for key, value in case.items():
                setattr(existing, key, value)
            # 幂等重载时恢复软删除
            existing.deleted_at = None
            updated += 1
    session.commit()
    total = session.scalar(select(func.count()).select_from(EvalCase))
    return {"inserted": inserted, "updated": updated, "total": total}


def _load_json(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        path = Path(argv[1])
    else:
        path = Path("data/eval/cases_w1.json")

    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    try:
        cases = _load_json(path)
    except json.JSONDecodeError as exc:
        print(f"JSON 解析失败: {exc}", file=sys.stderr)
        return 1

    errors = vc.validate_cases(cases)
    if errors:
        print("校验失败,未写库:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    engine = create_db_engine()
    with Session(engine) as session:
        stats = upsert_cases(session, cases)
    print(
        f"装载完成: inserted={stats['inserted']} updated={stats['updated']} "
        f"eval_case 总行数={stats['total']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
