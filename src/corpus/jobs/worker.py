"""Worker runtime: claims jobs, runs handlers concurrently, keeps leases alive, retries.

Wake-up is push-based: an ``AFTER INSERT`` trigger on ``jobs`` calls ``pg_notify``, and the
worker ``LISTEN``s on a dedicated connection, so new work starts within milliseconds. Polling
every ``poll_interval`` remains as a safety net (missed notifications, retries coming due).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.db.session import system_session
from corpus.jobs import queue
from corpus.jobs.queue import ClaimedJob, PermanentJobError
from corpus.observability.metrics import (
    JOB_DURATION,
    JOBS,
    QUEUE_DEPTH,
    QUEUE_OLDEST_READY,
)
from corpus.observability.tracing import extract_context, tracer

logger = logging.getLogger(__name__)

NOTIFY_CHANNEL = "corpus_jobs"


class JobHandler(Protocol):
    async def run(self, job: ClaimedJob) -> None: ...

    async def on_dead(self, job: ClaimedJob, error: str) -> None:
        """Called once when the job is given up on (permanent error or retries exhausted)."""
        ...


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    concurrency: int = 4
    lease_seconds: int = 300
    poll_interval: float = 5.0
    backoff_base: float = 5.0
    backoff_max: float = 600.0
    maintenance_interval: float = 600.0
    metrics_interval: float = 15.0


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        handlers: Mapping[str, JobHandler],
        config: WorkerConfig | None = None,
        *,
        worker_id: str | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._handlers = dict(handlers)
        self.config = config or WorkerConfig()
        self.worker_id = worker_id or default_worker_id()

    # ------------------------------------------------------------------ single steps
    async def claim(self) -> ClaimedJob | None:
        async with system_session(self._sm) as session:
            return await queue.claim(
                session, worker_id=self.worker_id, lease_seconds=self.config.lease_seconds
            )

    async def run_once(self) -> bool:
        """Claim and execute one job. Returns ``False`` when no job is ready."""
        job = await self.claim()
        if job is None:
            return False
        await self.execute(job)
        return True

    async def drain(self, max_jobs: int = 10_000) -> int:
        """Process ready jobs sequentially until none is left. Handy in tests and scripts."""
        processed = 0
        while processed < max_jobs and await self.run_once():
            processed += 1
        return processed

    async def execute(self, job: ClaimedJob) -> None:
        # Continue the trace of the request that enqueued the job (W3C traceparent).
        with tracer().start_as_current_span(
            f"job {job.kind}",
            context=extract_context(job.payload),
            kind=SpanKind.CONSUMER,
            attributes={
                "job.id": job.id,
                "job.kind": job.kind,
                "job.attempt": job.attempts,
                "job.max_attempts": job.max_attempts,
                "worker.id": self.worker_id,
            },
        ) as span:
            outcome = await self._execute(job)
            span.set_attribute("job.outcome", outcome)
            if outcome != "succeeded":
                span.set_status(Status(StatusCode.ERROR, outcome))

    async def _execute(self, job: ClaimedJob) -> str:
        handler = self._handlers.get(job.kind)
        beat = asyncio.create_task(self._heartbeat(job))
        started = time.perf_counter()
        outcome = "succeeded"
        try:
            if handler is None:
                raise PermanentJobError(f"no handler registered for job kind {job.kind!r}")
            await handler.run(job)
        except PermanentJobError as exc:
            outcome = "dead"
            logger.warning("job %s (%s) failed permanently: %s", job.id, job.kind, exc)
            await self._bury(job, handler, f"PermanentJobError: {exc}")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if job.attempts >= job.max_attempts:
                outcome = "dead"
                logger.error("job %s (%s) exhausted %d attempts", job.id, job.kind, job.attempts)
                await self._bury(job, handler, error)
            else:
                outcome = "retried"
                delay = queue.compute_backoff(
                    job.attempts, base=self.config.backoff_base, cap=self.config.backoff_max
                )
                logger.warning(
                    "job %s (%s) attempt %d/%d failed (%s); retrying in %.1fs",
                    job.id, job.kind, job.attempts, job.max_attempts, error, delay,
                )  # fmt: skip
                async with system_session(self._sm) as session:
                    await queue.fail(
                        session, job=job, worker_id=self.worker_id, error=error,
                        retry_in_seconds=delay,
                    )  # fmt: skip
        else:
            async with system_session(self._sm) as session:
                if not await queue.complete(session, job_id=job.id, worker_id=self.worker_id):
                    logger.warning("job %s finished after its lease was taken over", job.id)
            logger.info(
                "job %s (%s) done in %.2fs", job.id, job.kind, time.perf_counter() - started
            )
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            JOBS.labels(job.kind, outcome).inc()
            JOB_DURATION.labels(job.kind).observe(time.perf_counter() - started)
        return outcome

    async def _bury(self, job: ClaimedJob, handler: JobHandler | None, error: str) -> None:
        async with system_session(self._sm) as session:
            buried = await queue.fail(
                session, job=job, worker_id=self.worker_id, error=error, retry_in_seconds=None
            )
        if buried and handler is not None:
            try:
                await handler.on_dead(job, error)
            except Exception:
                logger.exception("on_dead hook failed for job %s", job.id)

    async def _heartbeat(self, job: ClaimedJob) -> None:
        interval = self.config.lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            try:
                async with system_session(self._sm) as session:
                    alive = await queue.heartbeat(
                        session,
                        job_id=job.id,
                        worker_id=self.worker_id,
                        lease_seconds=self.config.lease_seconds,
                    )
            except Exception:
                logger.exception("heartbeat failed for job %s", job.id)
                continue
            if not alive:
                logger.warning("lost the lease on job %s", job.id)
                return

    # ------------------------------------------------------------------ long-running loop
    async def run(self, stop: asyncio.Event, *, listen_dsn: str | None = None) -> None:
        wake = asyncio.Event()
        listener = await self._listen(listen_dsn, wake.set) if listen_dsn else None
        slots = asyncio.Semaphore(self.config.concurrency)
        running: set[asyncio.Task[None]] = set()
        next_maintenance = next_metrics = 0.0
        logger.info(
            "worker %s started (concurrency=%d, kinds=%s)",
            self.worker_id, self.config.concurrency, sorted(self._handlers),
        )  # fmt: skip
        try:
            while not stop.is_set():
                if time.monotonic() >= next_maintenance:
                    await self._maintenance()
                    next_maintenance = time.monotonic() + self.config.maintenance_interval
                if time.monotonic() >= next_metrics:
                    await self.refresh_queue_metrics()
                    next_metrics = time.monotonic() + self.config.metrics_interval

                await slots.acquire()
                wake.clear()  # clear *before* claiming: a NOTIFY racing the claim is not lost
                try:
                    job = await self.claim()
                except Exception:
                    logger.exception("claim failed")
                    job = None
                if job is None:
                    slots.release()
                    await _wait_any(wake, stop, max_wait=self.config.poll_interval)
                    continue

                task = asyncio.create_task(self.execute(job), name=f"job-{job.id}")
                running.add(task)
                task.add_done_callback(running.discard)
                task.add_done_callback(lambda _: slots.release())
        finally:
            if running:
                logger.info("waiting for %d running job(s) to finish", len(running))
                await asyncio.gather(*running, return_exceptions=True)
            if listener is not None:
                await listener.close()
            logger.info("worker %s stopped", self.worker_id)

    async def _maintenance(self) -> None:
        try:
            async with system_session(self._sm) as session:
                await queue.purge(session)
        except Exception:
            logger.exception("maintenance failed")

    async def refresh_queue_metrics(self) -> None:
        """Publish queue depth and lag as gauges (every worker reports the same global view;
        dashboards take the max across instances)."""
        try:
            async with system_session(self._sm) as session:
                for status, count in (await queue.stats(session)).items():
                    QUEUE_DEPTH.labels(status).set(count)
                QUEUE_OLDEST_READY.set(await queue.oldest_ready_age(session))
        except Exception:
            logger.warning("could not refresh queue metrics", exc_info=True)

    @staticmethod
    async def _listen(dsn: str, on_notify: Callable[[], None]) -> Any:
        import asyncpg

        try:
            url = make_url(dsn).set(drivername="postgresql")
            # A raw asyncpg DSN speaks libpq: SQLAlchemy's ``ssl`` is ``sslmode`` here.
            query = dict(url.query)
            if "ssl" in query:
                query["sslmode"] = query.pop("ssl")
            url = url.set(query=query)
            conn = await asyncpg.connect(url.render_as_string(hide_password=False))
            await conn.add_listener(NOTIFY_CHANNEL, lambda *_: on_notify())
        except Exception:
            logger.warning("LISTEN unavailable, falling back to polling", exc_info=True)
            return None
        return conn


async def _wait_any(*events: asyncio.Event, max_wait: float) -> None:
    """Return as soon as any event is set, or after ``max_wait`` seconds."""
    waiters = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, timeout=max_wait, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
