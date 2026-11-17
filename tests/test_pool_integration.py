"""Regression test for the connection-pool deadlock found by the load test.

Before the fix, a search held the connection that loaded the collection while acquiring two
more for its concurrent vector and keyword searches. With N requests in flight each holding one
connection and waiting for another, the pool starved and every request hung until the 30 s pool
timeout. A two-connection pool and ten concurrent searches made that certain; now each request
holds at most one connection at a time, so they queue and all complete.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from corpus.config import Settings
from corpus.core.embeddings import HashEmbedder
from corpus.main import create_app
from tests.conftest import ADMIN_KEY, FakeGenerator

pytestmark = pytest.mark.integration


async def test_concurrent_searches_cannot_starve_a_tiny_pool(settings: Settings) -> None:
    tiny = settings.model_copy(
        update={"db_pool_size": 2, "db_max_overflow": 0, "db_pool_timeout_seconds": 10.0}
    )
    app = create_app(tiny, embedder=HashEmbedder(tiny.embedding_dim), generator=FakeGenerator())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
            admin = {"X-Admin-Key": ADMIN_KEY}
            tenant = (await c.post("/admin/tenants", json={"name": "pool"}, headers=admin)).json()
            key = (
                await c.post(f"/admin/tenants/{tenant['id']}/api-keys", json={}, headers=admin)
            ).json()["key"]
            headers = {"X-API-Key": key}
            cid = (await c.post("/v1/collections", json={"name": "c"}, headers=headers)).json()[
                "id"
            ]

            async def search(i: int) -> int:
                r = await c.post(
                    f"/v1/collections/{cid}/search",
                    json={"query": f"question {i}"},
                    headers=headers,
                )
                return r.status_code

            statuses = await asyncio.wait_for(
                asyncio.gather(*(search(i) for i in range(10))), timeout=20
            )
    assert statuses == [200] * 10
