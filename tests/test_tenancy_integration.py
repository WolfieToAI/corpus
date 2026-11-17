"""Tenant isolation, API-key lifecycle, rate limiting and admin access control."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from corpus.db.models import Collection, Document
from corpus.db.session import system_session, tenant_session
from tests.conftest import ADMIN_KEY, Env

pytestmark = pytest.mark.integration


async def _collection(env: Env, key: str, name: str = "docs") -> str:
    r = await env.client.post("/v1/collections", json={"name": name}, headers={"X-API-Key": key})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def test_tenants_cannot_see_each_other_through_the_api(env: Env) -> None:
    _, key_a = await env.new_tenant("tenant-a")
    _, key_b = await env.new_tenant("tenant-b")
    a, b = {"X-API-Key": key_a}, {"X-API-Key": key_b}
    cid = await _collection(env, key_a)
    files = {"file": ("note.txt", b"Secret roadmap of tenant A.", "text/plain")}
    doc_id = (
        await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=a)
    ).json()["id"]
    await env.worker.drain()

    assert (await env.client.get("/v1/collections", headers=b)).json() == []
    for method, path, kwargs in [
        ("GET", f"/v1/collections/{cid}/documents", {}),
        ("POST", f"/v1/collections/{cid}/search", {"json": {"query": "roadmap"}}),
        ("DELETE", f"/v1/collections/{cid}", {}),
        ("GET", f"/v1/documents/{doc_id}", {}),
        ("DELETE", f"/v1/documents/{doc_id}", {}),
    ]:
        r = await env.client.request(method, path, headers=b, **kwargs)
        assert r.status_code == 404, (method, path, r.status_code)

    # Both tenants may use the same collection name.
    await _collection(env, key_b)
    # ...and tenant A's data is untouched.
    r = await env.client.post(f"/v1/collections/{cid}/search", json={"query": "roadmap"}, headers=a)
    assert len(r.json()["sources"]) == 1


async def test_row_level_security_is_enforced_by_postgres(env: Env) -> None:
    tenant_a, key_a = await env.new_tenant("tenant-a")
    tenant_b, _ = await env.new_tenant("tenant-b")
    await _collection(env, key_a)

    # A query with *no* tenant filter still only sees the session's tenant rows.
    async with tenant_session(env.sm, tenant_a) as s:
        assert len((await s.scalars(select(Collection))).all()) == 1
    async with tenant_session(env.sm, tenant_b) as s:
        assert (await s.scalars(select(Collection))).all() == []

    # Writing a row for another tenant is rejected by the policy's WITH CHECK clause.
    async with tenant_session(env.sm, tenant_b) as s:
        s.add(Collection(tenant_id=tenant_a, name="smuggled"))
        with pytest.raises(DBAPIError, match="row-level security"):
            await s.commit()


async def test_composite_foreign_keys_block_cross_tenant_references(env: Env) -> None:
    _, key_a = await env.new_tenant("tenant-a")
    tenant_b, _ = await env.new_tenant("tenant-b")
    cid = uuid.UUID(await _collection(env, key_a))

    # Even a system session (no RLS) cannot attach tenant B's document to tenant A's collection.
    async with system_session(env.sm) as s:
        s.add(
            Document(
                tenant_id=tenant_b,
                collection_id=cid,
                filename="x.txt",
                content_type="text/plain",
                size_bytes=1,
                sha256="0" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_revoked_key_stops_working_immediately(env: Env) -> None:
    tenant_id, key = await env.new_tenant("tenant-a")
    headers = {"X-API-Key": key}
    assert (await env.client.get("/v1/collections", headers=headers)).status_code == 200

    admin = {"X-Admin-Key": ADMIN_KEY}
    keys = (await env.client.get(f"/admin/tenants/{tenant_id}/api-keys", headers=admin)).json()
    assert len(keys) == 1 and "key" not in keys[0]  # the secret is never returned again
    assert (
        await env.client.delete(f"/admin/api-keys/{keys[0]['id']}", headers=admin)
    ).status_code == 204

    assert (await env.client.get("/v1/collections", headers=headers)).status_code == 401


async def test_rate_limit_returns_429_with_retry_after(env: Env) -> None:
    _, key = await env.new_tenant("tiny", rate_limit=3)
    headers = {"X-API-Key": key}
    for remaining in (2, 1, 0):
        r = await env.client.get("/v1/collections", headers=headers)
        assert r.status_code == 200
        assert r.headers["X-RateLimit-Remaining"] == str(remaining)
    r = await env.client.get("/v1/collections", headers=headers)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60


async def test_admin_routes_require_the_admin_key(env: Env) -> None:
    for headers in ({}, {"X-Admin-Key": "wrong"}):
        r = await env.client.post("/admin/tenants", json={"name": "x"}, headers=headers)
        assert r.status_code == 401


async def test_deleting_a_tenant_removes_everything(env: Env) -> None:
    tenant_id, key = await env.new_tenant("short-lived")
    await _collection(env, key)
    admin = {"X-Admin-Key": ADMIN_KEY}
    assert (
        await env.client.delete(f"/admin/tenants/{tenant_id}", headers=admin)
    ).status_code == 204
    assert (await env.client.get("/v1/collections", headers={"X-API-Key": key})).status_code == 401
    async with system_session(env.sm) as s:
        assert (await s.scalars(select(Collection))).all() == []
