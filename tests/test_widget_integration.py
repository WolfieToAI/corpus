"""Publishable widget keys and the demo seeding command."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from corpus.config import Settings
from corpus.core.embeddings import HashEmbedder
from corpus.demo import seed
from tests.conftest import ADMIN_KEY, Env

pytestmark = pytest.mark.integration

ORIGIN = "https://shop.example.com"
DATASET = Path(__file__).resolve().parents[1] / "eval" / "dataset"


async def _widget_setup(env: Env) -> tuple[str, str, str, str]:
    """Returns (full key, widget key, widget collection id, other collection id)."""
    tenant_id, full_key = await env.new_tenant("shop")
    headers = {"X-API-Key": full_key}
    cid = (await env.client.post("/v1/collections", json={"name": "faq"}, headers=headers)).json()[
        "id"
    ]
    other = (
        await env.client.post("/v1/collections", json={"name": "internal"}, headers=headers)
    ).json()["id"]
    r = await env.client.post(
        f"/admin/tenants/{tenant_id}/api-keys",
        json={"name": "site", "scope": "widget", "collection_id": cid, "allowed_origins": [ORIGIN]},
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    assert r.status_code == 201, r.text
    assert r.json()["scope"] == "widget" and r.json()["allowed_origins"] == [ORIGIN]
    return full_key, r.json()["key"], cid, other


async def test_widget_key_can_search_and_chat_from_its_origin(env: Env) -> None:
    _, key, cid, _ = await _widget_setup(env)
    headers = {"X-API-Key": key, "Origin": ORIGIN}
    r = await env.client.post(f"/v1/collections/{cid}/search", json={"query": "x"}, headers=headers)
    assert r.status_code == 200
    async with env.client.stream(
        "POST", f"/v1/collections/{cid}/chat", json={"question": "x"}, headers=headers
    ) as r:
        assert r.status_code == 200


@pytest.mark.parametrize("origin", [None, "https://evil.example.com", "http://shop.example.com"])
async def test_widget_key_rejects_other_origins(env: Env, origin: str | None) -> None:
    _, key, cid, _ = await _widget_setup(env)
    headers = {"X-API-Key": key} | ({"Origin": origin} if origin else {})
    r = await env.client.post(f"/v1/collections/{cid}/search", json={"query": "x"}, headers=headers)
    assert r.status_code == 403


async def test_widget_key_is_confined_to_search_chat_and_one_collection(env: Env) -> None:
    _, key, cid, other = await _widget_setup(env)
    headers = {"X-API-Key": key, "Origin": ORIGIN}
    # Another collection of the same tenant is invisible to the widget key.
    r = await env.client.post(
        f"/v1/collections/{other}/search", json={"query": "x"}, headers=headers
    )
    assert r.status_code == 404
    # Everything but search and chat is forbidden, reads included.
    for method, path in [
        ("GET", "/v1/collections"),
        ("GET", f"/v1/collections/{cid}/documents"),
        ("DELETE", f"/v1/collections/{cid}"),
    ]:
        r = await env.client.request(method, path, headers=headers)
        assert r.status_code == 403, (method, path)
    files = {"file": ("x.md", b"hello", "text/markdown")}
    r = await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=headers)
    assert r.status_code == 403


async def test_widget_key_validation(env: Env) -> None:
    tenant_id, _ = await env.new_tenant("strict")
    admin = {"X-Admin-Key": ADMIN_KEY}
    for body in (
        {"scope": "widget"},  # no collection, no origin
        {"scope": "widget", "collection_id": str(tenant_id), "allowed_origins": ["not an origin"]},
        {"scope": "full", "allowed_origins": [ORIGIN]},
    ):
        r = await env.client.post(f"/admin/tenants/{tenant_id}/api-keys", json=body, headers=admin)
        assert r.status_code == 422, body
    # A collection of another tenant cannot be bound to this tenant's widget key.
    _, _, foreign_cid, _ = await _widget_setup(env)
    r = await env.client.post(
        f"/admin/tenants/{tenant_id}/api-keys",
        json={"scope": "widget", "collection_id": foreign_cid, "allowed_origins": [ORIGIN]},
        headers=admin,
    )
    assert r.status_code == 404


async def test_demo_seed_is_idempotent_and_returns_a_working_link(
    env: Env, settings: Settings
) -> None:
    embedder = HashEmbedder(settings.embedding_dim)
    url = await seed(
        settings, embedder, DATASET, ["http://localhost:8000"], "http://localhost:8000"
    )
    again = await seed(
        settings, embedder, DATASET, ["http://localhost:8000"], "http://localhost:8000"
    )
    first, second = parse_qs(urlparse(url).query), parse_qs(urlparse(again).query)
    assert second["collection"] == first["collection"]  # same tenant and collection reused
    search = f"/v1/collections/{second['collection'][0]}/search"
    body = {"query": "What does error E-317 mean?"}

    # Re-seeding rotates the demo key: the previous one is revoked.
    stale = {"X-API-Key": first["key"][0], "Origin": "http://localhost:8000"}
    assert (await env.client.post(search, json=body, headers=stale)).status_code == 401

    fresh = {"X-API-Key": second["key"][0], "Origin": "http://localhost:8000"}
    r = await env.client.post(search, json=body, headers=fresh)
    assert r.status_code == 200
    assert any("E-317" in s["content"] for s in r.json()["sources"])
