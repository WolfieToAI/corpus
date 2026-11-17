"""Observability building blocks, without a database."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse
from prometheus_client import REGISTRY
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from corpus.config import Settings
from corpus.core.generation import ExtractiveGenerator, GenerationResult
from corpus.core.prompting import Source
from corpus.main import create_app
from corpus.observability.logging import JsonFormatter, RequestIdMiddleware, request_id_var
from corpus.observability.metrics import MetricsMiddleware
from corpus.observability.tracing import current_trace_id, extract_context, inject_context, tracer


def _toy_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/v1")

    @router.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @router.get("/stream")
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            for part in (b"a", b"b", b"c"):
                yield part

        return StreamingResponse(body())

    app.include_router(router)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


def _count(route: str, status: str = "200") -> float:
    labels = {"method": "GET", "route": route, "status": status}
    return REGISTRY.get_sample_value("corpus_http_requests_total", labels) or 0.0


async def test_http_metrics_use_route_templates_not_raw_paths() -> None:
    before = _count("/v1/items/{item_id}")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_toy_app()), base_url="http://t"
    ) as c:
        for i in range(3):
            assert (await c.get(f"/v1/items/{i}")).status_code == 200
        assert (await c.get("/nope")).status_code == 404
        assert (await c.get("/v1/stream")).text == "abc"
    assert _count("/v1/items/{item_id}") == before + 3  # one series, whatever the id
    assert _count("unmatched", "404") >= 1
    assert _count("/v1/stream") >= 1  # streaming responses are recorded once complete
    duration = REGISTRY.get_sample_value(
        "corpus_http_request_duration_seconds_count", {"method": "GET", "route": "/v1/stream"}
    )
    assert duration is not None and duration >= 1


async def test_request_id_is_minted_or_propagated() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_toy_app()), base_url="http://t"
    ) as c:
        minted = (await c.get("/v1/items/1")).headers["x-request-id"]
        assert len(minted) == 32
        kept = await c.get("/v1/items/1", headers={"X-Request-ID": "client-abc.123"})
        assert kept.headers["x-request-id"] == "client-abc.123"
        rejected = await c.get("/v1/items/1", headers={"X-Request-ID": "bad id\nwith newline"})
        assert rejected.headers["x-request-id"] != "bad id\nwith newline"


def test_json_logs_carry_request_and_trace_ids() -> None:
    record = logging.LogRecord(
        "corpus.test", logging.INFO, __file__, 1, "hello %s", ("world",), None
    )
    record.job_id = 42
    token = request_id_var.set("req-1")
    try:
        with tracer().start_as_current_span("unit-test"):
            entry = json.loads(JsonFormatter().format(record))
            trace_id = current_trace_id()
    finally:
        request_id_var.reset(token)
    assert entry["message"] == "hello world"
    assert entry["level"] == "INFO" and entry["logger"] == "corpus.test"
    assert entry["request_id"] == "req-1"
    assert entry["job_id"] == 42  # `extra=` fields become top-level keys
    assert entry["trace_id"] == trace_id and len(entry["span_id"]) == 16


def test_trace_context_round_trips_through_a_job_payload() -> None:
    with tracer().start_as_current_span("producer") as producer:
        carrier = inject_context()
    assert "traceparent" in carrier
    ctx = extract_context({"_trace": carrier})
    with tracer().start_as_current_span("consumer", context=ctx) as consumer:
        assert consumer.get_span_context().trace_id == producer.get_span_context().trace_id
    # Garbage in the payload must not break the worker.
    extract_context({"_trace": "not-a-dict"})


async def test_extractive_generator_quotes_and_cites() -> None:
    src = Source(
        1,
        "d",
        "manual.md",
        1,
        1,
        "## Battery\nThe robot uses a lithium battery. A full charge takes about 4 hours on the "
        "dock. Do not leave it unplugged for months.",
        1.0,
    )
    other = Source(2, "d", "faq.md", 1, 1, "Charging the dock overnight is safe.", 0.5)
    result = GenerationResult()
    gen = ExtractiveGenerator(max_sentences=1)
    text = "".join(
        [
            t
            async for t in gen.stream_answer(
                "How long does a full charge take?", [src, other], [], result
            )
        ]
    )
    # The sentence that answers the question, not the first one or the heading.
    assert text == "A full charge takes about 4 hours on the dock. [1]"
    assert result.model == "extractive" and result.output_tokens == len(text.split(" "))

    # A weakly related second sentence is left out: an identifier match outweighs a common word.
    codes = Source(
        1, "d", "m.md", 1, 1, "Error codes are shown in the app. E-317: wheel stuck.", 1.0
    )
    text = "".join(
        [
            t
            async for t in ExtractiveGenerator().stream_answer(
                "What does error E-317 mean?", [codes], [], result
            )
        ]
    )
    assert text == "E-317: wheel stuck. [1]"
    empty = "".join([t async for t in ExtractiveGenerator().stream_answer("q", [], [], result)])
    assert "do not contain" in empty


async def test_pool_exhaustion_is_shed_as_503() -> None:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]

    async def overloaded() -> None:
        raise PoolTimeoutError("QueuePool limit reached")

    app.add_api_route("/boom", overloaded)
    before = REGISTRY.get_sample_value("corpus_load_shed_total") or 0.0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/boom")
    assert r.status_code == 503 and r.headers["retry-after"] == "1"
    assert REGISTRY.get_sample_value("corpus_load_shed_total") == before + 1


async def test_widget_and_demo_are_served() -> None:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        js = await c.get("/widget.js")
        page = await c.get("/demo")
        home = await c.get("/", follow_redirects=False)
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "customElements.define" in js.text
    assert page.status_code == 200 and "<corpus-chat" not in page.text.split("<script")[0]
    assert '<script src="/widget.js">' in page.text
    assert home.status_code == 307 and home.headers["location"] == "/demo"


async def test_metrics_endpoint_is_exposed() -> None:
    app = create_app(Settings(_env_file=None), embedder=None)  # type: ignore[call-arg]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert "corpus_http_requests_total" in r.text and "corpus_retrieval_stage_seconds" in r.text
