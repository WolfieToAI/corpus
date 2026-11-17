"""Queue primitives: enqueue, claim, heartbeat, complete, fail.

Guarantees
----------
* **At-least-once execution.** A claimed job holds a *lease* (``locked_until``). If its worker
  crashes, the lease expires and another worker reclaims the job. Handlers must therefore be
  idempotent - the ingestion handler replaces a document's chunks atomically.
* **No double processing under concurrency.** Claiming uses ``FOR UPDATE SKIP LOCKED``: N
  workers polling at once each lock a different row, without blocking each other.
* **Fencing.** Every state transition after the claim is conditioned on
  ``locked_by = <this worker>``, so a worker whose lease was taken over cannot overwrite the
  outcome recorded by the new owner.
* **Bounded retries.** Failures are retried with exponential backoff and jitter; after
  ``max_attempts`` (or on a :class:`PermanentJobError`) the job is moved to ``dead``.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import SpanKind
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corpus.observability.tracing import TRACE_CARRIER_KEY, inject_context, tracer


class PermanentJobError(Exception):
    """Raised by a handler for failures that retrying cannot fix (bad input, unsupported file)."""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: int
    tenant_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int  # including the current one
    max_attempts: int


def compute_backoff(
    attempt: int, *, base: float, cap: float, rng: random.Random | None = None
) -> float:
    """Delay before retry number ``attempt`` (1-based): capped exponential with equal jitter.

    Half the delay is fixed and half random, so retries of jobs that failed together (e.g. an
    embedding API outage) spread out instead of hammering the dependency in lockstep.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    exp: float = min(cap, base * 2.0 ** (attempt - 1))
    jitter: float = (rng or random).uniform(0, exp / 2)
    return exp / 2 + jitter


async def enqueue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Insert a job in the caller's transaction (so it commits atomically with the data it
    refers to). Returns the job id, or ``None`` if an identical job is already active.

    The current trace context is stored with the job, so the worker's span continues the
    trace of the request that created it.
    """
    with tracer().start_as_current_span(
        f"enqueue {kind}", kind=SpanKind.PRODUCER, attributes={"job.kind": kind}
    ) as span:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO jobs (tenant_id, kind, payload, idempotency_key, max_attempts)
                    VALUES (:tenant_id, :kind, CAST(:payload AS jsonb), :key, :max_attempts)
                    ON CONFLICT (idempotency_key) WHERE status IN ('queued', 'running')
                    DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "kind": kind,
                    "payload": json.dumps({**payload, TRACE_CARRIER_KEY: inject_context()}),
                    "key": idempotency_key,
                    "max_attempts": max_attempts,
                },
            )
        ).first()
        job_id = int(row[0]) if row else None
        span.set_attribute("job.id", job_id if job_id is not None else -1)
        span.set_attribute("job.deduplicated", job_id is None)
        return job_id


_CLAIM = text(
    """
    UPDATE jobs SET
        status = 'running',
        attempts = attempts + 1,
        locked_by = :worker,
        locked_until = now() + make_interval(secs => :lease),
        updated_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE (status = 'queued' AND run_at <= now())
           OR (status = 'running' AND locked_until < now())
        ORDER BY run_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, tenant_id, kind, payload, attempts, max_attempts
    """
)


async def claim(session: AsyncSession, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
    row = (
        await session.execute(_CLAIM, {"worker": worker_id, "lease": float(lease_seconds)})
    ).first()
    await session.commit()
    if row is None:
        return None
    # Raw text() queries hand jsonb back undecoded with the asyncpg driver.
    payload = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
    return ClaimedJob(
        id=row.id,
        tenant_id=row.tenant_id,
        kind=row.kind,
        payload=dict(payload),
        attempts=row.attempts,
        max_attempts=row.max_attempts,
    )


async def heartbeat(
    session: AsyncSession, *, job_id: int, worker_id: str, lease_seconds: int
) -> bool:
    """Extend the lease. ``False`` means the lease was lost to another worker."""
    result = await session.execute(
        text(
            """
            UPDATE jobs SET locked_until = now() + make_interval(secs => :lease), updated_at = now()
            WHERE id = :id AND status = 'running' AND locked_by = :worker
            """
        ),
        {"id": job_id, "worker": worker_id, "lease": float(lease_seconds)},
    )
    await session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def complete(session: AsyncSession, *, job_id: int, worker_id: str) -> bool:
    result = await session.execute(
        text(
            """
            UPDATE jobs SET status = 'succeeded', locked_by = NULL, locked_until = NULL,
                            last_error = NULL, updated_at = now()
            WHERE id = :id AND status = 'running' AND locked_by = :worker
            """
        ),
        {"id": job_id, "worker": worker_id},
    )
    await session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def fail(
    session: AsyncSession,
    *,
    job: ClaimedJob,
    worker_id: str,
    error: str,
    retry_in_seconds: float | None,
) -> bool:
    """Record a failure: reschedule (``retry_in_seconds``) or bury (``None``) the job."""
    if retry_in_seconds is None:
        sql = """
            UPDATE jobs SET status = 'dead', locked_by = NULL, locked_until = NULL,
                            last_error = :error, updated_at = now()
            WHERE id = :id AND status = 'running' AND locked_by = :worker
        """
        params: dict[str, Any] = {}
    else:
        sql = """
            UPDATE jobs SET status = 'queued', locked_by = NULL, locked_until = NULL,
                            run_at = now() + make_interval(secs => :delay),
                            last_error = :error, updated_at = now()
            WHERE id = :id AND status = 'running' AND locked_by = :worker
        """
        params = {"delay": float(retry_in_seconds)}
    result = await session.execute(
        text(sql), {"id": job.id, "worker": worker_id, "error": error[:4000], **params}
    )
    await session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def purge(session: AsyncSession, *, keep_succeeded_days: int = 7) -> None:
    """Housekeeping: drop old successful jobs and expired rate-limit windows."""
    await session.execute(
        text(
            "DELETE FROM jobs WHERE status = 'succeeded' "
            "AND updated_at < now() - make_interval(days => :days)"
        ),
        {"days": keep_succeeded_days},
    )
    await session.execute(
        text("DELETE FROM rate_limit_counters WHERE window_start < now() - interval '1 hour'")
    )
    await session.commit()


async def oldest_ready_age(session: AsyncSession) -> float:
    """Seconds the oldest ready job has been waiting: the queue lag users actually feel."""
    age = await session.scalar(
        text(
            "SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(run_at)), 0) FROM jobs "
            "WHERE status = 'queued' AND run_at <= now()"
        )
    )
    return float(age or 0)


async def stats(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(text("SELECT status::text, count(*) FROM jobs GROUP BY status"))
    counts = {"queued": 0, "running": 0, "succeeded": 0, "dead": 0}
    counts.update({status: int(n) for status, n in rows.all()})
    return counts
