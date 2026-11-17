"""Application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from corpus import __version__
from corpus.api import admin
from corpus.api.routes import health_router, router
from corpus.config import Settings, get_settings
from corpus.core.embeddings import Embedder, build_embedder
from corpus.core.generation import AnswerGenerator, ChatCompletionsGenerator, ExtractiveGenerator
from corpus.core.reranking import Reranker, build_reranker
from corpus.core.rewriting import LLMQueryRewriter, QueryRewriter
from corpus.db.session import create_engine, create_sessionmaker, run_migrations
from corpus.demo import DEFAULT_DATASET as DEMO_DATASET
from corpus.demo import ensure_demo
from corpus.jobs.worker import Worker, WorkerConfig
from corpus.llm import build_llm_client
from corpus.observability.logging import RequestIdMiddleware, setup_logging
from corpus.observability.metrics import LOAD_SHED, MetricsMiddleware, metrics_endpoint
from corpus.observability.tracing import configure_tracing, instrument_engine
from corpus.services.ingestion import INGEST_DOCUMENT, IngestDocumentHandler
from corpus.services.ocr import build_ocr
from corpus.web import STATIC_DIR

logger = logging.getLogger("corpus")


def _build_generator(settings: Settings) -> AnswerGenerator | None:
    if settings.llm_provider == "extractive":
        return ExtractiveGenerator(token_delay=settings.extractive_token_delay_ms / 1000)
    client = build_llm_client(settings)
    if client is None or settings.llm_model is None:
        logger.warning("LLM_API_KEY / LLM_MODEL not set: /chat is disabled, /search still works")
        return None
    return ChatCompletionsGenerator(
        client,
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
    )


def _build_rewriter(settings: Settings) -> QueryRewriter | None:
    client = build_llm_client(settings)
    if client is None or settings.llm_model is None or not settings.query_rewriting:
        return None
    return LLMQueryRewriter(client, model=settings.llm_model)


async def _prepare_demo(app: FastAPI, settings: Settings) -> None:
    """Seed the sample documents and expose a widget key for /demo (DEMO_MODE)."""
    origin = (settings.public_url or "http://localhost:8000").rstrip("/")
    try:
        app.state.demo = await ensure_demo(app.state.sessionmaker, settings, DEMO_DATASET, [origin])
        logger.info(
            "demo ready",
            extra={"origin": origin, "documents_enqueued": app.state.demo.enqueued},
        )
    except Exception:
        logger.exception("demo seeding failed")


_UNSET: Any = object()


def create_app(
    settings: Settings | None = None,
    *,
    embedder: Embedder | None = None,
    generator: AnswerGenerator | None = None,
    reranker: Reranker | None = _UNSET,
    rewriter: QueryRewriter | None = _UNSET,
) -> FastAPI:
    """Build the app. Tests inject fakes; ``None`` explicitly disables reranker / rewriter."""
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_format)
    tracing = configure_tracing(
        settings.otel_exporter_otlp_endpoint, settings.otel_service_name or "corpus-api"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The schema is managed by Alembic. Normally a release step runs `alembic upgrade head`;
        # single-instance hosting without release commands opts into AUTO_MIGRATE instead.
        if settings.auto_migrate:
            await run_migrations(settings.database_url)
        engine = create_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
        )
        if tracing:
            instrument_engine(engine)
        app.state.settings = settings
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.embedder = embedder or build_embedder(settings)
        app.state.generator = generator or _build_generator(settings)
        app.state.reranker = build_reranker(settings) if reranker is _UNSET else reranker
        app.state.rewriter = _build_rewriter(settings) if rewriter is _UNSET else rewriter
        logger.info(
            "corpus %s ready",
            __version__,
            extra={
                "embeddings": settings.embedding_provider,
                "reranker": settings.reranker if app.state.reranker else "none",
                "query_rewriting": app.state.rewriter is not None,
                "llm": getattr(app.state.generator, "model", None) or settings.llm_model
                if app.state.generator
                else "disabled",
                "tracing": tracing,
            },
        )
        stop = asyncio.Event()
        background: list[asyncio.Task[None]] = []
        app.state.demo = None
        if settings.embedded_worker:
            worker = Worker(
                app.state.sessionmaker,
                {
                    INGEST_DOCUMENT: IngestDocumentHandler(
                        app.state.sessionmaker, app.state.embedder, settings, build_ocr(settings)
                    )
                },
                WorkerConfig(
                    concurrency=settings.worker_concurrency,
                    lease_seconds=settings.job_lease_seconds,
                    poll_interval=settings.worker_poll_interval_seconds,
                    backoff_base=settings.job_backoff_base_seconds,
                    backoff_max=settings.job_backoff_max_seconds,
                ),
            )
            background.append(
                asyncio.create_task(worker.run(stop, listen_dsn=settings.database_url))
            )
        demo_task = (
            asyncio.create_task(_prepare_demo(app, settings), name="demo")
            if settings.demo_mode
            else None
        )
        try:
            yield
        finally:
            stop.set()  # the worker finishes its running jobs, then exits
            if demo_task is not None:
                demo_task.cancel()
                background.append(demo_task)
            await asyncio.gather(*background, return_exceptions=True)
            await engine.dispose()

    app = FastAPI(
        title="Corpus",
        version=__version__,
        summary="Multi-tenant Retrieval-Augmented Generation API with cited, streamed answers.",
        lifespan=lifespan,
    )
    # Starlette wraps middleware in reverse order of registration: the request id is minted
    # first (outermost), then metrics time everything below, including CORS handling.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "Retry-After"],
    )
    if settings.metrics_enabled:
        app.add_middleware(MetricsMiddleware)
        app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(PoolTimeoutError)
    async def shed_load(request: Request, exc: PoolTimeoutError) -> JSONResponse:
        # Overload: every connection is busy. Fail fast and tell clients when to come back.
        LOAD_SHED.inc()
        logger.warning(
            "database pool exhausted, shedding request", extra={"path": request.url.path}
        )
        return JSONResponse(
            {"detail": "service overloaded, retry shortly"},
            status_code=503,
            headers={"Retry-After": "1"},
        )

    @app.get("/widget.js", include_in_schema=False)
    async def widget() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "widget.js",
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/demo", include_in_schema=False)
    async def demo() -> FileResponse:
        return FileResponse(STATIC_DIR / "demo.html", media_type="text/html")

    @app.get("/demo/config", include_in_schema=False)
    async def demo_config(request: Request) -> JSONResponse:
        # Only in DEMO_MODE. The widget key is public by design: search and chat only, on the
        # demo collection, from the public URL's origin, under the tenant's rate limit.
        config = getattr(request.app.state, "demo", None)
        if not settings.demo_mode:
            return JSONResponse({"detail": "demo mode is off"}, status_code=404)
        if config is None:
            return JSONResponse(
                {"detail": "demo is starting"}, status_code=503, headers={"Retry-After": "5"}
            )
        return JSONResponse({"collection": str(config.collection_id), "key": config.key})

    @app.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse("/demo")

    app.include_router(health_router)
    app.include_router(router)
    app.include_router(admin.router)

    if tracing:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    return app


app = create_app()
