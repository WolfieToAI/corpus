"""Single-instance demo mode: migrations at startup, embedded worker, self-seeding /demo."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import text

from corpus.config import Settings
from corpus.core.embeddings import HashEmbedder
from corpus.db.session import create_engine
from corpus.main import create_app
from tests.conftest import FakeGenerator

pytestmark = pytest.mark.integration

PUBLIC_URL = "https://corpus-demo.onrender.com"


async def test_demo_mode_boots_an_empty_database_into_a_working_demo(settings: Settings) -> None:
    # Start from a database with no schema at all: AUTO_MIGRATE must build it.
    engine = create_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()

    demo_settings = settings.model_copy(
        update={
            "auto_migrate": True,
            "embedded_worker": True,
            "demo_mode": True,
            "public_url": PUBLIC_URL,
            "worker_poll_interval_seconds": 0.2,
        }
    )
    app = create_app(
        demo_settings, embedder=HashEmbedder(demo_settings.embedding_dim), generator=FakeGenerator()
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            config = None
            for _ in range(100):
                r = await client.get("/demo/config")
                if r.status_code == 200:
                    config = r.json()
                    break
                assert r.status_code == 503
                await asyncio.sleep(0.1)
            assert config is not None, "demo never became ready"

            headers = {"X-API-Key": config["key"], "Origin": PUBLIC_URL}
            path = f"/v1/collections/{config['collection']}/documents"
            # The embedded worker indexes the sample documents in the background.
            docs: list[dict[str, str]] = []
            for _ in range(200):
                # Widget keys cannot list documents: check readiness through search instead.
                r = await client.post(
                    f"/v1/collections/{config['collection']}/search",
                    json={"query": "What does error E-317 mean?", "mode": "keyword"},
                    headers=headers,
                )
                assert r.status_code == 200
                docs = r.json()["sources"]
                if any("E-317" in s["content"] for s in docs):
                    break  # the R7 manual is indexed (documents finish in any order)
                await asyncio.sleep(0.1)
            assert any("E-317" in s["content"] for s in docs)

            # The published widget key is confined to the public origin.
            r = await client.post(
                f"/v1/collections/{config['collection']}/search",
                json={"query": "x"},
                headers={"X-API-Key": config["key"], "Origin": "https://elsewhere.example"},
            )
            assert r.status_code == 403
            assert (await client.get(path, headers=headers)).status_code == 403


async def test_demo_config_is_hidden_outside_demo_mode(settings: Settings) -> None:
    app = create_app(settings, embedder=HashEmbedder(settings.embedding_dim))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.get("/demo/config")).status_code == 404
