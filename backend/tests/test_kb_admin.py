"""W9-3 知识库管理后台 API 测试(/api/kb)。

覆盖任务书 W9-3 验收点:
- agent 角色鉴权(沿用 W8 RBAC:无 token→401、buyer→403);
- 条款 CRUD + status 灰度(draft/active/retired)+ 版本自增;
- ``POST /api/kb/reload`` 热更新:改条款 → reload → 检索到新 version,
  retired 条款检索不可见,``restore`` 后恢复可见;
- ``POST /api/kb/upload``:Markdown 自动切片成草稿;
- 租户隔离(别的 merchant 看不到)。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.main import app
from app.models import KB_STATUS_ACTIVE, Merchant
from app.retrieval.hybrid import get_retriever, invalidate_cache

MERCHANT = "m-kb-admin"
OTHER_MERCHANT = "m-kb-other"

AGENT = security.Identity(
    sub="agent-kb", role="agent", merchant_id=MERCHANT, service_id="svc-kb"
)
BUYER = security.Identity(
    sub="buyer-kb", role="buyer", merchant_id=MERCHANT, customer_id="c-kb"
)

MARKDOWN = """# 售后政策补充

- 版本: v1
- 生效日期: 2026-01-01

## RP-90 退款到账补充
退款将在 24 小时内原路退回。

## RP-91 换货补充
换货需在签收后 7 天内申请。
"""


def _auth(identity: security.Identity) -> dict[str, str]:
    return {"Authorization": f"Bearer {security.create_token(identity)}"}


@pytest.fixture()
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with Session(engine) as session:
        for merchant_id, name in ((MERCHANT, "KB 测试店"), (OTHER_MERCHANT, "别家店")):
            if session.get(Merchant, merchant_id) is None:
                session.add(Merchant(id=merchant_id, name=name))
        session.commit()
    invalidate_cache()
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()
    invalidate_cache()


def _create(client, clause_id: str, content: str, *, status: str = "active", **kw):
    body = {"clause_id": clause_id, "title": clause_id, "content": content, "status": status}
    body.update(kw)
    return client.post("/api/kb/items", json=body, headers=_auth(AGENT))


def _search(client, engine, query: str):
    with Session(engine) as session:
        return get_retriever(session, MERCHANT).search(query, top_k=5)


# ---------------------------------------------------------------------------
# 1) 鉴权
# ---------------------------------------------------------------------------
def test_requires_bearer_token(client):
    resp = client.get("/api/kb/items")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"


def test_buyer_role_is_forbidden(client):
    resp = client.get("/api/kb/items", headers=_auth(BUYER))
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "FORBIDDEN"
    assert "agent" in body["error"]["message"]


def test_invalid_token_is_rejected(client):
    resp = client.get("/api/kb/items", headers={"Authorization": "Bearer not.a.token"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"].startswith("AUTH_")


# ---------------------------------------------------------------------------
# 2) CRUD + 版本自增
# ---------------------------------------------------------------------------
def test_create_list_and_read_item(client):
    resp = _create(client, "RP-30", "退款按实付金额计算。")
    assert resp.status_code == 201
    item = resp.json()["item"]
    assert item["status"] == "active" and item["version"] == "1"

    listed = client.get("/api/kb/items", headers=_auth(AGENT)).json()
    assert [row["clause_id"] for row in listed["items"]] == ["RP-30"]

    read = client.get(f"/api/kb/items/{item['id']}", headers=_auth(AGENT))
    assert read.status_code == 200 and read.json()["item"]["content"] == "退款按实付金额计算。"


def test_duplicate_clause_id_conflicts(client):
    assert _create(client, "RP-31", "第一条。").status_code == 201
    resp = _create(client, "RP-31", "重复条款号。")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "STATE_CONFLICT"


def test_patch_content_bumps_version(client):
    item = _create(client, "RP-32", "退款 48 小时到账。").json()["item"]
    resp = client.patch(
        f"/api/kb/items/{item['id']}",
        json={"content": "退款 24 小时到账(修订)。"},
        headers=_auth(AGENT),
    )
    assert resp.status_code == 200
    updated = resp.json()["item"]
    assert updated["version"] == "2"
    assert updated["content"] == "退款 24 小时到账(修订)。"

    # version 已是 2,PATCH 同内容不自增(只在正文/标题变化时升版)
    again = client.patch(
        f"/api/kb/items/{item['id']}", json={"status": "active"}, headers=_auth(AGENT)
    )
    assert again.json()["item"]["version"] == "2"


def test_patch_clause_id_bumps_version(client):
    """条款号是检索结果的身份字段,变更同样升版本(docstring 承诺)。"""
    item = _create(client, "RP-34", "改号测试正文。").json()["item"]
    resp = client.patch(
        f"/api/kb/items/{item['id']}", json={"clause_id": "RP-35"}, headers=_auth(AGENT)
    )
    assert resp.status_code == 200
    updated = resp.json()["item"]
    assert updated["clause_id"] == "RP-35"
    assert updated["version"] == "2"


def test_patch_rejects_unknown_status_via_validation(client):
    item = _create(client, "RP-33", "测试正文。").json()["item"]
    resp = client.patch(
        f"/api/kb/items/{item['id']}", json={"status": "pending"}, headers=_auth(AGENT)
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_filters_by_status_and_rejects_unknown(client):
    _create(client, "RP-34", "草稿条款。", status="draft")
    _create(client, "RP-35", "生效条款。", status="active")
    drafts = client.get("/api/kb/items?status=draft", headers=_auth(AGENT)).json()
    assert [row["clause_id"] for row in drafts["items"]] == ["RP-34"]
    bad = client.get("/api/kb/items?status=pending", headers=_auth(AGENT))
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# 3) 热更新三步:改条款 → reload → 新版本可见 / retired 不可见 / restore 回滚
# ---------------------------------------------------------------------------
def test_reload_hot_updates_version_and_hides_retired(client, engine):
    keep = _create(client, "RP-40", "退款 48 小时内原路退回。").json()["item"]
    drop = _create(client, "RP-41", "换货需在签收后 7 天内申请。").json()["item"]

    assert _search(client, engine, "原路退回").results[0].clause_id == "RP-40"
    assert _search(client, engine, "签收后 7 天").results[0].clause_id == "RP-41"

    # 第二步:改条款正文 → version 自增;再 POST /reload 重建索引
    patched = client.patch(
        f"/api/kb/items/{keep['id']}",
        json={"content": "退款 24 小时内原路退回(热更新版)。"},
        headers=_auth(AGENT),
    )
    assert patched.json()["item"]["version"] == "2"
    retired = client.request(
        "DELETE", f"/api/kb/items/{drop['id']}", headers=_auth(AGENT)
    )
    assert retired.status_code == 200 and retired.json()["item"]["status"] == "retired"

    reload_resp = client.post("/api/kb/reload", headers=_auth(AGENT))
    assert reload_resp.status_code == 200
    payload = reload_resp.json()
    assert payload["ok"] is True
    assert payload["active_clauses"] == 1
    assert payload["vector_rows_written"] == 1
    assert payload["bm25_route_top_k"] == 20 and payload["fusion_threshold"] == 0.005

    # 第三步:检索到新 version;retired 条款不可见
    hits = _search(client, engine, "原路退回")
    assert not hits.no_hit
    assert hits.results[0].version == "2"
    assert "24 小时" in hits.results[0].text
    assert all(r.clause_id != "RP-41" for r in _search(client, engine, "签收后 7 天").results)

    # 回滚:restore → reload → 重新可见
    restored = client.post(f"/api/kb/items/{drop['id']}/restore", headers=_auth(AGENT))
    assert restored.json()["item"]["status"] == "active"
    assert client.post("/api/kb/reload", headers=_auth(AGENT)).json()["active_clauses"] == 2
    assert _search(client, engine, "签收后 7 天").results[0].clause_id == "RP-41"


def test_retire_endpoint_is_idempotent(client):
    item = _create(client, "RP-42", "退役测试。").json()["item"]
    first = client.post(f"/api/kb/items/{item['id']}/retire", headers=_auth(AGENT))
    second = client.post(f"/api/kb/items/{item['id']}/retire", headers=_auth(AGENT))
    assert first.status_code == second.status_code == 200
    assert second.json()["item"]["status"] == "retired"


def test_version_bump_keeps_v_prefix(client):
    """政策 md 的版本号是 "v1";自增应保留前缀 → "v2",不变成裸数字 "2"。"""
    item = _create(client, "RP-43", "退款 48 小时内原路退回。", version="v1").json()["item"]
    assert item["version"] == "v1"
    patched = client.patch(
        f"/api/kb/items/{item['id']}",
        json={"content": "退款 24 小时内原路退回(保留 v 前缀)。"},
        headers=_auth(AGENT),
    )
    assert patched.status_code == 200
    assert patched.json()["item"]["version"] == "v2"


# ---------------------------------------------------------------------------
# 4) 上传:Markdown → 自动切片成草稿
# ---------------------------------------------------------------------------
def test_upload_slices_markdown_into_drafts(client):
    resp = client.post(
        "/api/kb/upload",
        json={"filename": "supplement.md", "content": MARKDOWN},
        headers=_auth(AGENT),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["created_count"] == 2
    created = payload["created"]
    assert {row["clause_id"] for row in created} == {"RP-90", "RP-91"}
    assert all(row["status"] == "draft" for row in created), "上传只生成草稿,需人工启用"

    # 草稿不参与检索(reload 后仍只有 active 进索引)
    assert client.post("/api/kb/reload", headers=_auth(AGENT)).json()["active_clauses"] == 0
    drafts = client.get("/api/kb/items?status=draft", headers=_auth(AGENT)).json()
    assert {row["clause_id"] for row in drafts["items"]} == {"RP-90", "RP-91"}


def test_upload_overwrites_same_clause_as_new_draft(client):
    client.post("/api/kb/items", json={
        "clause_id": "RP-90", "title": "旧标题", "content": "旧正文。", "status": "active",
    }, headers=_auth(AGENT))
    resp = client.post(
        "/api/kb/upload",
        json={"filename": "supplement.md", "content": MARKDOWN},
        headers=_auth(AGENT),
    )
    payload = resp.json()
    assert payload["updated_count"] == 1 and payload["created_count"] == 1
    updated = payload["updated"][0]
    assert updated["clause_id"] == "RP-90"
    assert updated["version"] == "2"
    assert updated["status"] == "draft"


def test_upload_rejects_markdown_without_clause_blocks(client):
    resp = client.post(
        "/api/kb/upload",
        json={"filename": "bad.md", "content": "这里没有任何二级标题条款。"},
        headers=_auth(AGENT),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 5) 统计与租户隔离
# ---------------------------------------------------------------------------
def test_stats_counts_by_status(client):
    _create(client, "RP-50", "生效一。", status="active")
    _create(client, "RP-51", "草稿一。", status="draft")
    _create(client, "RP-52", "退役一。", status="retired")
    stats = client.get("/api/kb/stats", headers=_auth(AGENT)).json()
    assert stats["total"] == 3
    assert stats["by_status"] == {"draft": 1, "active": 1, "retired": 1}
    assert stats["indexed_clauses"] == 1
    assert stats["vector_dim"] >= 1


def test_tenant_isolation(client, engine):
    item = _create(client, "RP-53", "本店条款。").json()["item"]
    other = security.Identity(sub="agent-other", role="agent", merchant_id=OTHER_MERCHANT)
    resp = client.get(f"/api/kb/items/{item['id']}", headers=_auth(other))
    assert resp.status_code == 404
    assert client.get("/api/kb/items", headers=_auth(other)).json()["items"] == []
