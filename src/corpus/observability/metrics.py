"""Prometheus metrics.

Naming follows Prometheus conventions (``corpus_`` prefix, base units, ``_total`` counters).
Labels are kept low-cardinality on purpose: route *templates* rather than raw paths, and no
tenant id or document id (one time series per tenant would not scale with the customer base -
per-tenant analytics belong in logs and traces).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_SLOW = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

# --------------------------------------------------------------------------- HTTP
HTTP_REQUESTS = Counter(
    "corpus_http_requests_total", "HTTP requests.", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "corpus_http_request_duration_seconds",
    "HTTP request duration, until the last byte of the body (streams included).",
    ["method", "route"],
    buckets=(*_FAST, 30.0, 60.0),
)
HTTP_IN_FLIGHT = Gauge("corpus_http_requests_in_flight", "HTTP requests being served.")

# --------------------------------------------------------------------------- auth & limits
AUTH_FAILURES = Counter("corpus_auth_failures_total", "Rejected API keys.")
RATE_LIMITED = Counter("corpus_rate_limited_total", "Requests rejected by the rate limiter.")
LOAD_SHED = Counter(
    "corpus_load_shed_total", "Requests answered 503 because the database pool was exhausted."
)

# --------------------------------------------------------------------------- retrieval
RETRIEVAL_STAGE = Histogram(
    "corpus_retrieval_stage_seconds",
    "Duration of each retrieval stage.",
    ["stage"],  # embed | vector | keyword | rerank | total
    buckets=_FAST,
)
RETRIEVAL_SOURCES = Histogram(
    "corpus_retrieval_sources", "Sources returned per query.", buckets=(0, 1, 2, 3, 4, 6, 8, 10)
)

# --------------------------------------------------------------------------- generation
LLM_TIME_TO_FIRST_TOKEN = Histogram(
    "corpus_llm_time_to_first_token_seconds",
    "Delay between the request reaching the LLM and its first token.",
    ["model"],
    buckets=_SLOW,
)
LLM_DURATION = Histogram(
    "corpus_llm_generation_seconds", "Full answer generation time.", ["model"], buckets=_SLOW
)
LLM_TOKENS = Counter("corpus_llm_tokens_total", "LLM tokens.", ["model", "direction"])
LLM_ERRORS = Counter("corpus_llm_errors_total", "Failed or refused generations.", ["reason"])

# --------------------------------------------------------------------------- ingestion & jobs
INGEST_STAGE = Histogram(
    "corpus_ingest_stage_seconds",
    "Duration of each ingestion stage.",
    ["stage"],  # parse | chunk | embed | store
    buckets=_SLOW,
)
INGEST_CHUNKS = Counter("corpus_ingest_chunks_total", "Chunks written by ingestion.")
INGEST_OCR_PAGES = Counter("corpus_ingest_ocr_pages_total", "Scanned PDF pages read with OCR.")
JOBS = Counter("corpus_jobs_total", "Jobs executed, by outcome.", ["kind", "outcome"])
JOB_DURATION = Histogram("corpus_job_duration_seconds", "Job run time.", ["kind"], buckets=_SLOW)
QUEUE_DEPTH = Gauge("corpus_queue_jobs", "Jobs in the queue, by status.", ["status"])
QUEUE_OLDEST_READY = Gauge(
    "corpus_queue_oldest_ready_seconds",
    "Age of the oldest job that is ready to run (queue lag). 0 when the queue is drained.",
)


class Timer:
    """``with Timer(RETRIEVAL_STAGE.labels("vector")):`` - observes elapsed seconds."""

    def __init__(self, histogram: Any) -> None:
        self._histogram = histogram
        self.elapsed = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed = time.perf_counter() - self._start
        self._histogram.observe(self.elapsed)


class MetricsMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware): it measures streaming responses until the
    final body chunk is sent, which is what a client actually waits for."""

    def __init__(self, app: ASGIApp, exclude: frozenset[str] = frozenset({"/metrics"})) -> None:
        self.app = app
        self.exclude = exclude

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exclude:
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status = 500
        recorded = False

        def record() -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            method = scope["method"]
            HTTP_DURATION.labels(method, template).observe(time.perf_counter() - start)
            HTTP_REQUESTS.labels(method, template, str(status)).inc()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                record()

        HTTP_IN_FLIGHT.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            HTTP_IN_FLIGHT.dec()
            record()  # no-op if already recorded; catches exceptions and disconnects


async def metrics_endpoint(_: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
