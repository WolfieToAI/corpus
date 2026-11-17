"""Engine, sessions and the tenant context that drives Row-Level Security.

A *tenant session* is an ``AsyncSession`` whose ``info["tenant_id"]`` is set. Every time such a
session begins a transaction - including the implicit new one after a ``commit()`` - the
``after_begin`` hook below runs::

    SET LOCAL ROLE corpus_tenant;
    SELECT set_config('app.tenant_id', '<uuid>', true);

Both are transaction-scoped (``LOCAL`` / ``is_local=true``), so a pooled connection never leaks
one tenant's context into the next request. Sessions without a tenant are *system sessions*,
used for authentication, the job queue and administration.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

TENANT_ROLE = "corpus_tenant"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class TenantAwareSession(Session):
    """Sync session class behind every ``AsyncSession`` created by :func:`create_sessionmaker`."""


@event.listens_for(TenantAwareSession, "after_begin")
def _apply_tenant_context(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    tenant_id = session.info.get("tenant_id")
    if tenant_id is None:
        return
    connection.execute(text(f"SET LOCAL ROLE {TENANT_ROLE}"))
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


def create_engine(
    database_url: str, *, pool_size: int = 10, max_overflow: int = 20, pool_timeout: float = 5.0
) -> AsyncEngine:
    """``pool_timeout`` is deliberately short: under overload it is better to shed a request
    quickly (the API answers 503 + Retry-After) than to queue it for 30 seconds."""
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, sync_session_class=TenantAwareSession)


def tenant_session(sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> AsyncSession:
    """A session whose every transaction runs under RLS for ``tenant_id``."""
    return sm(info={"tenant_id": tenant_id})


def system_session(sm: async_sessionmaker[AsyncSession]) -> AsyncSession:
    return sm()


def alembic_ini_path() -> Path:
    """``alembic.ini`` in the working directory (``/app`` in the Docker image, where the package
    lives in site-packages), else at the root of a source checkout."""
    for candidate in (Path.cwd() / "alembic.ini", _PROJECT_ROOT / "alembic.ini"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("alembic.ini not found: run from the project root (or /app)")


async def run_migrations(
    database_url: str, revision: str = "head", *, downgrade: bool = False
) -> None:
    """Apply Alembic migrations programmatically (tests, AUTO_MIGRATE, corpus-demo)."""
    from alembic import command
    from alembic.config import Config

    ini = alembic_ini_path()
    cfg = Config(str(ini))
    # Explicit, so it never depends on how the package was installed.
    cfg.set_main_option("script_location", str(ini.parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.attributes["configure_logger"] = False
    action: Any = command.downgrade if downgrade else command.upgrade
    # env.py drives its own event loop: run it in a worker thread.
    await asyncio.to_thread(action, cfg, revision)
