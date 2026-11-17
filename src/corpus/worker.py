"""Worker process entrypoint: ``corpus-worker`` (or ``python -m corpus.worker``)."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from prometheus_client import start_http_server

from corpus.config import get_settings
from corpus.core.embeddings import build_embedder
from corpus.db.session import create_engine, create_sessionmaker
from corpus.jobs.worker import Worker, WorkerConfig
from corpus.observability.logging import setup_logging
from corpus.observability.tracing import configure_tracing, instrument_engine
from corpus.services.ingestion import INGEST_DOCUMENT, IngestDocumentHandler
from corpus.services.ocr import build_ocr


async def _main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    tracing = configure_tracing(
        settings.otel_exporter_otlp_endpoint, settings.otel_service_name or "corpus-worker"
    )
    if settings.metrics_enabled:
        # Each worker serves its own /metrics; Prometheus discovers every replica via DNS.
        start_http_server(settings.worker_metrics_port)
    engine = create_engine(settings.database_url)
    if tracing:
        instrument_engine(engine)
    sm = create_sessionmaker(engine)
    embedder = build_embedder(settings)

    worker = Worker(
        sm,
        {INGEST_DOCUMENT: IngestDocumentHandler(sm, embedder, settings, build_ocr(settings))},
        WorkerConfig(
            concurrency=settings.worker_concurrency,
            lease_seconds=settings.job_lease_seconds,
            poll_interval=settings.worker_poll_interval_seconds,
            backoff_base=settings.job_backoff_base_seconds,
            backoff_max=settings.job_backoff_max_seconds,
        ),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Graceful shutdown: stop claiming, let running jobs finish. (Not supported on Windows
        # event loops; Ctrl+C still interrupts there.)
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await worker.run(stop, listen_dsn=settings.database_url)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
