"""Migrations must apply cleanly, roll back cleanly, and re-apply (CI guards the schema)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from corpus.db.session import create_engine, run_migrations

pytestmark = pytest.mark.integration

TABLES = {
    "tenants", "api_keys", "jobs", "rate_limit_counters",
    "collections", "documents", "document_blobs", "chunks",
}  # fmt: skip


async def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        names = {r[0] for r in rows}
    await engine.dispose()
    return names - {"alembic_version"}


async def _rls_tables(url: str) -> set[str]:
    engine = create_engine(url)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT relname FROM pg_class WHERE relrowsecurity AND relkind = 'r'")
        )
        names = {r[0] for r in rows}
    await engine.dispose()
    return names


async def test_upgrade_downgrade_upgrade(database: str) -> None:
    assert await _tables(database) == TABLES
    assert await _rls_tables(database) == {
        "collections", "documents", "document_blobs", "chunks", "jobs",
    }  # fmt: skip

    await run_migrations(database, "base", downgrade=True)
    assert await _tables(database) == set()

    await run_migrations(database)
    assert await _tables(database) == TABLES
