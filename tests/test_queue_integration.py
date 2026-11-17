"""Job queue semantics: retries, dead-lettering, idempotency, leases, concurrency, LISTEN/NOTIFY."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.db.session import system_session
from corpus.jobs import queue
from corpus.jobs.queue import ClaimedJob, PermanentJobError
from corpus.jobs.worker import Worker, WorkerConfig
from corpus.services.tenancy import create_tenant

pytestmark = pytest.mark.integration

FAST = WorkerConfig(backoff_base=0, lease_seconds=30, poll_interval=30)


class RecordingHandler:
    def __init__(self, failures: int = 0, error: type[Exception] = RuntimeError) -> None:
        self.failures = failures
        self.error = error
        self.calls: list[int] = []
        self.dead: list[tuple[int, str]] = []
        self.ran = asyncio.Event()

    async def run(self, job: ClaimedJob) -> None:
        self.calls.append(job.attempts)
        if len(self.calls) <= self.failures:
            raise self.error(f"boom #{len(self.calls)}")
        self.ran.set()

    async def on_dead(self, job: ClaimedJob, error: str) -> None:
        self.dead.append((job.id, error))


async def _tenant(sm: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with system_session(sm) as s:
        tenant = await create_tenant(s, f"t-{uuid.uuid4().hex[:8]}", 100)
        await s.commit()
        return tenant.id


async def _enqueue(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    kind: str = "test",
    key: str | None = None,
    max_attempts: int = 5,
) -> int | None:
    async with system_session(sm) as s:
        job_id = await queue.enqueue(
            s, tenant_id=tenant_id, kind=kind, payload={"n": 1}, idempotency_key=key,
            max_attempts=max_attempts,
        )  # fmt: skip
        await s.commit()
        return job_id


async def _job(sm: async_sessionmaker[AsyncSession], job_id: int) -> dict[str, object]:
    async with system_session(sm) as s:
        row = (await s.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id})).one()
        return dict(row._mapping)


async def test_transient_failures_are_retried_until_success(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    handler = RecordingHandler(failures=2)
    job_id = await _enqueue(sm, await _tenant(sm))
    assert job_id is not None

    assert await Worker(sm, {"test": handler}, FAST).drain() == 3
    assert handler.calls == [1, 2, 3]
    job = await _job(sm, job_id)
    assert job["status"] == "succeeded" and job["attempts"] == 3 and job["last_error"] is None


async def test_exhausted_retries_dead_letter_the_job(sm: async_sessionmaker[AsyncSession]) -> None:
    handler = RecordingHandler(failures=10)
    job_id = await _enqueue(sm, await _tenant(sm), max_attempts=3)

    assert await Worker(sm, {"test": handler}, FAST).drain() == 3
    job = await _job(sm, job_id)  # type: ignore[arg-type]
    assert job["status"] == "dead" and job["attempts"] == 3
    assert "boom #3" in str(job["last_error"])
    assert [jid for jid, _ in handler.dead] == [job_id]  # on_dead fires exactly once


async def test_permanent_errors_are_not_retried(sm: async_sessionmaker[AsyncSession]) -> None:
    handler = RecordingHandler(failures=1, error=PermanentJobError)
    job_id = await _enqueue(sm, await _tenant(sm))

    assert await Worker(sm, {"test": handler}, FAST).drain() == 1
    assert (await _job(sm, job_id))["status"] == "dead"  # type: ignore[arg-type]
    assert len(handler.dead) == 1


async def test_unknown_job_kind_is_dead_lettered(sm: async_sessionmaker[AsyncSession]) -> None:
    job_id = await _enqueue(sm, await _tenant(sm), kind="nobody-handles-this")
    assert await Worker(sm, {}, FAST).drain() == 1
    job = await _job(sm, job_id)  # type: ignore[arg-type]
    assert job["status"] == "dead" and "no handler" in str(job["last_error"])


async def test_idempotency_key_dedupes_only_active_jobs(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _tenant(sm)
    first = await _enqueue(sm, tenant, key="ingest:42")
    assert first is not None
    assert await _enqueue(sm, tenant, key="ingest:42") is None  # still queued -> deduplicated

    await Worker(sm, {"test": RecordingHandler()}, FAST).drain()
    again = await _enqueue(sm, tenant, key="ingest:42")  # finished -> may run again
    assert again is not None and again != first


async def test_expired_lease_is_reclaimed_and_fenced(sm: async_sessionmaker[AsyncSession]) -> None:
    job_id = await _enqueue(sm, await _tenant(sm))
    crashed = Worker(sm, {}, FAST, worker_id="crashed")
    rescuer = Worker(sm, {}, FAST, worker_id="rescuer")

    claimed = await crashed.claim()
    assert claimed is not None and claimed.id == job_id
    assert await rescuer.claim() is None  # leased: invisible to other workers

    async with system_session(sm) as s:  # simulate the crashed worker's lease running out
        await s.execute(
            text("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
        await s.commit()

    reclaimed = await rescuer.claim()
    assert reclaimed is not None and reclaimed.id == job_id and reclaimed.attempts == 2

    # The original worker comes back to life: its stale completion must not win.
    async with system_session(sm) as s:
        assert not await queue.complete(s, job_id=job_id, worker_id="crashed")  # type: ignore[arg-type]
        assert await queue.complete(s, job_id=job_id, worker_id="rescuer")  # type: ignore[arg-type]


async def test_concurrent_workers_never_claim_the_same_job(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _tenant(sm)
    expected = {await _enqueue(sm, tenant) for _ in range(30)}
    workers = [Worker(sm, {}, FAST, worker_id=f"w{i}") for i in range(6)]

    async def claim_all(worker: Worker) -> list[int]:
        ids = []
        while (job := await worker.claim()) is not None:
            ids.append(job.id)
        return ids

    results = await asyncio.gather(*(claim_all(w) for w in workers))
    claimed = [job_id for ids in results for job_id in ids]
    assert len(claimed) == len(set(claimed)) == 30  # every job exactly once
    assert set(claimed) == expected


async def test_listen_notify_wakes_an_idle_worker(
    sm: async_sessionmaker[AsyncSession], database: str
) -> None:
    handler = RecordingHandler()
    # Polling every 30s: only a NOTIFY can make the job run within the timeout below.
    worker = Worker(sm, {"test": handler}, FAST)
    stop = asyncio.Event()
    loop = asyncio.create_task(worker.run(stop, listen_dsn=database))
    try:
        await asyncio.sleep(1.0)  # let the worker reach its idle wait
        await _enqueue(sm, await _tenant(sm))
        await asyncio.wait_for(handler.ran.wait(), timeout=5)
    finally:
        stop.set()
        await asyncio.wait_for(loop, timeout=10)
