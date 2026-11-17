"""Shared fixtures. Integration tests need TEST_DATABASE_URL (PostgreSQL + pgvector).

Each integration test gets a pristine schema built by the real Alembic migrations, so the tests
exercise exactly what production runs - RLS policies and grants included.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.config import Settings
from corpus.core.embeddings import HashEmbedder
from corpus.core.generation import GenerationResult, Turn
from corpus.core.prompting import Source
from corpus.db.session import create_engine, create_sessionmaker, run_migrations
from corpus.jobs.worker import Worker, WorkerConfig
from corpus.main import create_app
from corpus.services.ingestion import INGEST_DOCUMENT, IngestDocumentHandler

DB_URL = os.environ.get("TEST_DATABASE_URL")
ADMIN_KEY = "admin-secret-for-tests"

# One in-memory tracer provider for the whole session (OpenTelemetry allows setting the global
# provider only once). Tests read finished spans from SPANS.
SPANS = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_provider)


class FakeGenerator:
    """Answers by citing the first source, so the citation plumbing is exercised."""

    model = "fake"

    async def stream_answer(
        self,
        question: str,
        sources: Sequence[Source],
        history: Sequence[Turn],
        result: GenerationResult,
    ) -> AsyncIterator[str]:
        for token in ["According ", "to the manual ", "[1]."]:
            yield token
        result.model, result.stop_reason = "fake", "end_turn"


@pytest.fixture
async def database() -> str:
    if DB_URL is None:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    await run_migrations(DB_URL)
    return DB_URL


@pytest.fixture
def settings(database: str) -> Settings:
    return Settings(
        database_url=database,
        embedding_provider="hash",
        admin_api_key=ADMIN_KEY,
        job_backoff_base_seconds=0,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
async def sm(database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(database)
    yield create_sessionmaker(engine)
    await engine.dispose()


@dataclass
class Env:
    app: FastAPI
    client: httpx.AsyncClient
    sm: async_sessionmaker[AsyncSession]
    settings: Settings
    worker: Worker

    async def new_tenant(self, name: str, rate_limit: int | None = None) -> tuple[uuid.UUID, str]:
        """Create a tenant and an API key through the admin API; returns (tenant_id, key)."""
        admin = {"X-Admin-Key": ADMIN_KEY}
        body: dict[str, object] = {"name": name}
        if rate_limit is not None:
            body["rate_limit_per_minute"] = rate_limit
        r = await self.client.post("/admin/tenants", json=body, headers=admin)
        assert r.status_code == 201, r.text
        tenant_id = r.json()["id"]
        r = await self.client.post(f"/admin/tenants/{tenant_id}/api-keys", json={}, headers=admin)
        assert r.status_code == 201, r.text
        return uuid.UUID(tenant_id), r.json()["key"]


@pytest.fixture
async def env(settings: Settings) -> AsyncIterator[Env]:
    embedder = HashEmbedder(settings.embedding_dim)
    app = create_app(settings, embedder=embedder, generator=FakeGenerator())
    async with app.router.lifespan_context(app):
        sm = app.state.sessionmaker
        worker = Worker(
            sm,
            {INGEST_DOCUMENT: IngestDocumentHandler(sm, embedder, settings)},
            WorkerConfig(backoff_base=0, lease_seconds=60),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Env(app=app, client=client, sm=sm, settings=settings, worker=worker)
