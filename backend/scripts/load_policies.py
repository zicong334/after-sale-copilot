"""load_policies.py — 解析 data/policies/*.md → upsert knowledge_item + 重建 BM25 索引。

用法:
    python backend/scripts/load_policies.py [data/policies]

- 连库走 app.db.get_database_url():默认 backend/dev.db,可用 DATABASE_URL 覆盖。
- merchant 固定 ``seed-merchant-1``(与 seed.py 一致);不存在则自动创建。
- 每个 md 经 PolicyChunker 解析成 PolicyChunk,条款号(clause_id)从 md 的
  ``## {clause_id} {标题}`` 结构解析,不手写映射。

幂等策略(跑两遍 knowledge_item 行数不变):
- 按 ``clause_id``(含 merchant_id 条件)upsert:已存在 → 更新 title/content/version
  并复位 deleted_at;不存在 → 插入。embedding_model / dim 恒为 NULL(向量扩展点)。

版本号递增策略:
- ``knowledge_item.version`` 承载**政策内容版本**,值来自政策 md 文件头 ``- 版本:``。
- upsert 以 md 为唯一事实源:政策升级时(人工把 md 的 ``- 版本:`` 从 v1 改为 v2),
  重新 load 后同一 clause_id 行的 version 由 v1 更新为 v2,即"版本随政策内容递增"。
- load 脚本自身**不因重复执行而自动 +1**:否则跑两遍 version 会无意义漂移,
  与"跑两遍行数不变"的幂等验收冲突,也让"检索结果带稳定 version 供引用"失去意义。
- 同一 clause_id 只保留一行(单版本活跃),旧版本不堆积,避免检索到过期条款。

BM25 索引重建:
- load 同时用解析出的 chunks 调 BM25Backend.build() 无条件重建内存索引并打印条数;
  实际服务启动时检索层会再从 knowledge_item 读表重建(启动无条件重建)。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 把 scripts/ 与 backend/ 加入 sys.path,保证 `from app...` 可用
_SCRIPTS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPTS_DIR.parent
for _p in (_SCRIPTS_DIR, _BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import create_db_engine
from app.models import KnowledgeItem, Merchant
from app.retrieval.bm25_backend import BM25Backend
from app.retrieval.chunker import PolicyChunk, PolicyChunker

POLICY_MERCHANT_ID = "seed-merchant-1"
DEFAULT_POLICIES_DIR = Path("data/policies")


def _ensure_merchant(session: Session) -> None:
    """确保固定政策商户存在(不存在则创建),返回其 id。"""
    existing = session.get(Merchant, POLICY_MERCHANT_ID)
    if existing is None:
        session.add(
            Merchant(
                id=POLICY_MERCHANT_ID,
                name="seed 测试店铺",
                platform="demo",
                locale="zh-CN",
                currency="CNY",
                tz="UTC",
            )
        )
        session.flush()


def parse_policies(policies_dir: Path) -> list[PolicyChunk]:
    """解析目录下所有 *.md,合并成 PolicyChunk 列表(按文件名排序保证稳定)。"""
    chunker = PolicyChunker()
    chunks: list[PolicyChunk] = []
    for path in sorted(policies_dir.glob("*.md")):
        chunks.extend(chunker.parse(path.read_text(encoding="utf-8")))
    return chunks


def upsert_policies(
    session: Session, chunks: list[PolicyChunk]
) -> dict[str, int]:
    """按 clause_id 幂等 upsert knowledge_item,返回 {inserted, updated, total}。"""
    _ensure_merchant(session)
    inserted = 0
    updated = 0
    for chunk in chunks:
        existing = session.scalar(
            select(KnowledgeItem).where(
                KnowledgeItem.merchant_id == POLICY_MERCHANT_ID,
                KnowledgeItem.clause_id == chunk.clause_id,
            )
        )
        if existing is None:
            session.add(
                KnowledgeItem(
                    merchant_id=POLICY_MERCHANT_ID,
                    clause_id=chunk.clause_id,
                    title=chunk.title,
                    content=chunk.text,
                    version=chunk.version,
                    embedding_model=None,
                    dim=None,
                )
            )
            inserted += 1
        else:
            existing.title = chunk.title
            existing.content = chunk.text
            existing.version = chunk.version
            existing.embedding_model = None
            existing.dim = None
            existing.deleted_at = None
            updated += 1
    session.commit()
    total = session.scalar(
        select(func.count())
        .select_from(KnowledgeItem)
        .where(KnowledgeItem.deleted_at.is_(None))
    )
    return {"inserted": inserted, "updated": updated, "total": total}


def main(argv: list[str]) -> int:
    policies_dir = Path(argv[1]) if len(argv) >= 2 else DEFAULT_POLICIES_DIR
    if not policies_dir.is_dir():
        print(f"目录不存在: {policies_dir}", file=sys.stderr)
        return 1

    chunks = parse_policies(policies_dir)
    if not chunks:
        print("未解析到任何条款,请检查 md 文件", file=sys.stderr)
        return 1

    # 校验 clause_id 全局唯一(避免跨文件/同文件重复条款号导致幂等键冲突)
    seen: set[str] = set()
    for c in chunks:
        if c.clause_id in seen:
            print(f"条款号重复: {c.clause_id}", file=sys.stderr)
            return 1
        seen.add(c.clause_id)

    engine = create_db_engine()
    with Session(engine) as session:
        stats = upsert_policies(session, chunks)

    # 重建 BM25 索引(无条件全量重建;服务启动时检索层会再从表读并重建)
    backend = BM25Backend()
    backend.build(chunks)

    print(
        f"装载完成: 条款数={len(chunks)} inserted={stats['inserted']} "
        f"updated={stats['updated']} knowledge_item 总行数={stats['total']} "
        f"BM25 索引条数={len(chunks)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
