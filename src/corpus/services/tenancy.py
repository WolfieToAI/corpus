"""Tenants, API keys and per-tenant rate limiting (all system-level operations)."""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from corpus.core.api_keys import generate_api_key, parse_api_key, verify_secret
from corpus.db.models import ApiKey, Tenant

LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: uuid.UUID
    api_key_id: uuid.UUID
    rate_limit_per_minute: int
    scope: str = "full"
    collection_id: uuid.UUID | None = None
    allowed_origins: tuple[str, ...] = ()

    @property
    def is_widget(self) -> bool:
        return self.scope == "widget"


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int


async def create_tenant(session: AsyncSession, name: str, rate_limit_per_minute: int) -> Tenant:
    tenant = Tenant(name=name, rate_limit_per_minute=rate_limit_per_minute)
    session.add(tenant)
    await session.flush()
    return tenant


async def create_api_key(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    *,
    scope: str = "full",
    collection_id: uuid.UUID | None = None,
    allowed_origins: Sequence[str] = (),
) -> tuple[ApiKey, str]:
    """Create a key and return it with its plaintext, which is never stored nor shown again."""
    generated = generate_api_key()
    key = ApiKey(
        tenant_id=tenant_id,
        name=name,
        prefix=generated.prefix,
        secret_hash=generated.secret_hash,
        scope=scope,
        collection_id=collection_id,
        allowed_origins=list(allowed_origins),
    )
    session.add(key)
    await session.flush()
    return key, generated.plaintext


async def revoke_api_key(session: AsyncSession, key_id: uuid.UUID) -> bool:
    result = await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def authenticate(session: AsyncSession, raw_key: str) -> TenantContext | None:
    parsed = parse_api_key(raw_key)
    if parsed is None:
        return None
    row = (
        await session.execute(
            select(ApiKey, Tenant.rate_limit_per_minute)
            .join(Tenant, Tenant.id == ApiKey.tenant_id)
            .where(ApiKey.prefix == parsed.prefix)
        )
    ).first()
    if row is None:
        return None
    key, rate_limit = row
    if key.revoked_at is not None or not verify_secret(parsed.secret, key.secret_hash):
        return None

    # Bounded write amplification: refresh last_used_at at most once a minute per key.
    now = datetime.now(UTC)
    if key.last_used_at is None or now - key.last_used_at > LAST_USED_RESOLUTION:
        key.last_used_at = now
        await session.commit()
    return TenantContext(
        tenant_id=key.tenant_id,
        api_key_id=key.id,
        rate_limit_per_minute=rate_limit,
        scope=key.scope,
        collection_id=key.collection_id,
        allowed_origins=tuple(key.allowed_origins or ()),
    )


async def consume_rate_limit(session: AsyncSession, ctx: TenantContext) -> RateLimitDecision:
    """Fixed one-minute window, counted in PostgreSQL.

    One atomic upsert per request: correct across any number of API replicas without an extra
    datastore. (A Redis token bucket is the drop-in upgrade for very high request rates.)
    """
    row = (
        await session.execute(
            text(
                """
                INSERT INTO rate_limit_counters (tenant_id, window_start, count)
                VALUES (:tenant_id, date_trunc('minute', now()), 1)
                ON CONFLICT (tenant_id, window_start)
                DO UPDATE SET count = rate_limit_counters.count + 1
                RETURNING count, EXTRACT(EPOCH FROM (window_start + interval '1 minute' - now()))
                """
            ),
            {"tenant_id": ctx.tenant_id},
        )
    ).one()
    await session.commit()
    count, seconds_left = int(row[0]), float(row[1])
    limit = ctx.rate_limit_per_minute
    return RateLimitDecision(
        allowed=count <= limit,
        limit=limit,
        remaining=max(0, limit - count),
        reset_after_seconds=max(1, math.ceil(seconds_left)),
    )
