"""W9-3 知识库管理后台 API(agent 角色)。

路由一览(prefix ``/api/kb``)
------------------------------
- ``GET    /items``:条款列表(默认排除已删除;可 ``status`` 过滤);
- ``POST   /items``:新建条款(默认 ``draft`` 草稿,不参与检索);
- ``GET    /items/{id}``:条款详情;
- ``PATCH  /items/{id}``:更新标题/正文/条款号/状态;正文变更 → ``version+1``;
- ``DELETE /items/{id}``:退役(``status=retired``,软操作,可 restore);
- ``POST   /items/{id}/retire``:显式退役;
- ``POST   /items/{id}/restore``:回滚退役 → ``active``;
- ``POST   /reload``:热更新(重建 BM25 + 向量索引并落 ``kb_vector``,不重启);
- ``POST   /upload``:上传 Markdown,按 ``## {clause_id} {title}`` 自动切片成草稿。

鉴权(沿用 W8 RBAC)
-------------------
``/api/kb`` 不在 ``security.ROLE_PREFIXES``(W8 只覆盖 ``/api/c``、``/api/b``),
中间件不放行也不拦。本模块自建 ``require_agent`` 依赖:优先读中间件写入的
``request.state.identity``,没有就从 ``Authorization: Bearer <JWT>`` 解析,
校验 ``role == "agent"``;merchant 隔离一律用 token 里的 ``merchant_id``。

检索可见性
----------
只有 ``status == "active"`` 且未删除的条款参与检索(W9-2 的
``get_retriever`` / ``rebuild_index`` 共同保证);退役条款可 ``restore`` 回滚。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.db import utc_now
from app.errors import AppError, NotFoundError
from app.models.kb import (
    KB_STATUS_ACTIVE,
    KB_STATUS_DRAFT,
    KB_STATUS_RETIRED,
    KB_STATUS_VALUES,
    KnowledgeItem,
)
from app.retrieval.chunker import PolicyChunker
from app.retrieval.hybrid import (
    BM25_ROUTE_TOP_K,
    FUSION_THRESHOLD,
    get_retriever,
    invalidate_cache,
    rebuild_index,
)

router = APIRouter(prefix="/api/kb", tags=["kb"])

KBStatus = Literal["draft", "active", "retired"]


# ---------------------------------------------------------------------------
# 鉴权依赖
# ---------------------------------------------------------------------------
def require_agent(request: Request) -> security.Identity:
    """校验 agent 身份;401/403 错误体与 W8 中间件一致。"""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        token = security.bearer_token(request.headers.get("authorization"))
        if token is None:
            raise AppError(
                code="AUTH_REQUIRED",
                message="缺少身份信息(请先登录)",
                status_code=401,
            )
        try:
            identity = security.decode_token(token)
        except security.TokenError as exc:
            raise AppError(
                code=exc.code, message=exc.message, status_code=401
            ) from None
    if identity.role != "agent":
        raise AppError(
            code="FORBIDDEN",
            message=f"该接口需要 agent 角色,当前角色为 {identity.role}",
            status_code=403,
        )
    return identity


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class KbItemCreate(BaseModel):
    """新建条款草稿。"""

    model_config = ConfigDict(extra="forbid")

    clause_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=200)
    content: str = Field(min_length=1)
    status: KBStatus = KB_STATUS_DRAFT
    version: str = Field(default="1", max_length=20)


class KbItemUpdate(BaseModel):
    """局部更新;``content`` 变化会自增 version。"""

    model_config = ConfigDict(extra="forbid")

    clause_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str | None = Field(default=None, max_length=200)
    content: str | None = Field(default=None, min_length=1)
    status: KBStatus | None = None
    version: str | None = Field(default=None, max_length=20)


class KbUploadRequest(BaseModel):
    """Markdown 上传(前端 FileReader 读成文本后 POST,不依赖 multipart 包)。"""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(default="upload.md", max_length=200)
    content: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _item_dict(row: KnowledgeItem) -> dict[str, Any]:
    return {
        "id": row.id,
        "clause_id": row.clause_id,
        "title": row.title,
        "content": row.content,
        "status": row.status,
        "version": row.version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _bump_version(version: str) -> str:
    """版本自增:``1``→``2``、``v1``→``v2``(保留前缀);无法解析退化为 ``2``,不阻塞保存。"""
    raw = (version or "").strip()
    prefix = raw[:1] if raw[:1] in ("v", "V") else ""
    try:
        return f"{prefix}{int(raw[len(prefix):]) + 1}"
    except (TypeError, ValueError):
        return "2"


def _validate_status(value: str) -> str:
    if value not in KB_STATUS_VALUES:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"status 必须是 {KB_STATUS_VALUES} 之一",
            status_code=400,
            param="status",
        )
    return value


def _get_item(db: Session, merchant_id: str, item_id: str) -> KnowledgeItem:
    row = db.get(KnowledgeItem, item_id)
    if row is None or row.merchant_id != merchant_id or row.deleted_at is not None:
        raise NotFoundError(message="条款不存在")
    return row


def _same_clause(
    db: Session, merchant_id: str, clause_id: str, *, exclude: str | None = None
) -> KnowledgeItem | None:
    stmt = select(KnowledgeItem).where(
        KnowledgeItem.merchant_id == merchant_id,
        KnowledgeItem.clause_id == clause_id,
        KnowledgeItem.deleted_at.is_(None),
    )
    if exclude is not None:
        stmt = stmt.where(KnowledgeItem.id != exclude)
    return db.scalar(stmt)


# ---------------------------------------------------------------------------
# 条款 CRUD
# ---------------------------------------------------------------------------
@router.get("/items")
def list_items(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """条款列表(按 clause_id 排序;默认排除已删除)。"""
    stmt = select(KnowledgeItem).where(
        KnowledgeItem.merchant_id == identity.merchant_id,
        KnowledgeItem.deleted_at.is_(None),
    )
    if status is not None:
        stmt = stmt.where(KnowledgeItem.status == _validate_status(status))
    rows = list(
        db.execute(stmt.order_by(KnowledgeItem.clause_id).limit(limit))
        .scalars()
        .all()
    )
    return {"ok": True, "items": [_item_dict(row) for row in rows]}


@router.post("/items", status_code=201)
def create_item(
    payload: KbItemCreate,
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """新建条款;同 clause_id 已存在 → 409(避免检索出现重复条款号)。"""
    if _same_clause(db, identity.merchant_id, payload.clause_id) is not None:
        raise AppError(
            code="STATE_CONFLICT",
            message=f"条款号 {payload.clause_id} 已存在",
            status_code=409,
            param="clause_id",
        )
    row = KnowledgeItem(
        merchant_id=identity.merchant_id,
        clause_id=payload.clause_id,
        title=payload.title or None,
        content=payload.content,
        status=_validate_status(payload.status),
        version=payload.version or "1",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "item": _item_dict(row)}


@router.get("/items/{item_id}")
def read_item(
    item_id: str,
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """条款详情。"""
    return {"ok": True, "item": _item_dict(_get_item(db, identity.merchant_id, item_id))}


@router.patch("/items/{item_id}")
def update_item(
    item_id: str,
    payload: KbItemUpdate,
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """更新条款;正文/标题/条款号变化 → ``version+1``(检索结果带新版本)。"""
    row = _get_item(db, identity.merchant_id, item_id)
    changed = payload.model_dump(exclude_unset=True)

    if "clause_id" in changed and changed["clause_id"] != row.clause_id:
        clash = _same_clause(
            db, identity.merchant_id, changed["clause_id"], exclude=row.id
        )
        if clash is not None:
            raise AppError(
                code="STATE_CONFLICT",
                message=f"条款号 {changed['clause_id']} 已存在",
                status_code=409,
                param="clause_id",
            )
        row.clause_id = changed["clause_id"]

    content_dirty = False
    if "title" in changed and changed["title"] != row.title:
        row.title = changed["title"]
        content_dirty = True
    if "content" in changed and changed["content"] != row.content:
        row.content = changed["content"]
        content_dirty = True
    if "status" in changed:
        row.status = _validate_status(changed["status"])

    if "version" in changed and changed["version"] is not None:
        row.version = changed["version"]
    elif content_dirty:
        row.version = _bump_version(row.version)

    row.updated_at = utc_now()
    db.commit()
    db.refresh(row)
    return {"ok": True, "item": _item_dict(row)}


@router.delete("/items/{item_id}")
def delete_item(
    item_id: str,
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """退役条款(软操作;``status=retired``,检索不可见,可 restore 回滚)。"""
    row = _get_item(db, identity.merchant_id, item_id)
    row.status = KB_STATUS_RETIRED
    row.updated_at = utc_now()
    db.commit()
    db.refresh(row)
    return {"ok": True, "item": _item_dict(row)}


@router.post("/items/{item_id}/retire")
def retire_item(
    item_id: str,
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """显式退役(与 DELETE 同义,便于前端语义化调用)。"""
    return delete_item(item_id, identity, db)


@router.post("/items/{item_id}/restore")
def restore_item(
    item_id: str,
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """回滚退役 → active(重新参与检索)。"""
    row = _get_item(db, identity.merchant_id, item_id)
    row.status = KB_STATUS_ACTIVE
    row.updated_at = utc_now()
    db.commit()
    db.refresh(row)
    return {"ok": True, "item": _item_dict(row)}


# ---------------------------------------------------------------------------
# 热更新 + 上传
# ---------------------------------------------------------------------------
@router.post("/reload")
def reload_index(
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """热更新:重建 BM25 + 向量索引并写 ``kb_vector``,无需重启进程。"""
    invalidate_cache(identity.merchant_id)
    retriever = rebuild_index(db, identity.merchant_id, persist=True)
    db.commit()
    return {
        "ok": True,
        "merchant_id": identity.merchant_id,
        "active_clauses": retriever.clause_count,
        "vector_dim": retriever.vector_dim,
        "vector_rows_written": retriever.persisted_rows,
        "bm25_route_top_k": BM25_ROUTE_TOP_K,
        "fusion_threshold": FUSION_THRESHOLD,
        "reloaded_at": utc_now().isoformat(),
    }


@router.post("/upload")
def upload_markdown(
    payload: KbUploadRequest,
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """上传 Markdown → 自动切片成条款草稿(同 clause_id 覆盖为草稿并升版本)。"""
    chunks = PolicyChunker().parse(payload.content)
    if not chunks:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Markdown 里没有可识别的 '## {clause_id} {title}' 条款块",
            status_code=400,
            param="content",
        )
    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    for chunk in chunks:
        existing = _same_clause(db, identity.merchant_id, chunk.clause_id)
        if existing is None:
            row = KnowledgeItem(
                merchant_id=identity.merchant_id,
                clause_id=chunk.clause_id,
                title=chunk.title or None,
                content=chunk.text,
                status=KB_STATUS_DRAFT,
                version="1",
            )
            db.add(row)
            db.flush()
            created.append(_item_dict(row))
        else:
            existing.title = chunk.title or existing.title
            existing.content = chunk.text
            existing.status = KB_STATUS_DRAFT
            existing.version = _bump_version(existing.version)
            existing.updated_at = utc_now()
            db.flush()
            updated.append(_item_dict(existing))
    db.commit()
    return {
        "ok": True,
        "filename": payload.filename,
        "created": created,
        "updated": updated,
        "created_count": len(created),
        "updated_count": len(updated),
    }


@router.get("/stats")
def kb_stats(
    identity: security.Identity = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """知识库统计(列表页顶部徽标用)。"""
    rows = list(
        db.execute(
            select(KnowledgeItem).where(
                KnowledgeItem.merchant_id == identity.merchant_id,
                KnowledgeItem.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    counts = {value: 0 for value in KB_STATUS_VALUES}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    retriever = get_retriever(db, identity.merchant_id)
    return {
        "ok": True,
        "total": len(rows),
        "by_status": counts,
        "indexed_clauses": retriever.clause_count,
        "vector_dim": retriever.vector_dim,
    }
