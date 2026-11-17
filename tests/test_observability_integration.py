"""Traces and metrics produced by the real request and job paths."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from tests.conftest import SPANS, Env

pytestmark = pytest.mark.integration


def _metric(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def test_one_trace_follows_a_document_from_upload_to_worker(env: Env) -> None:
    _, key = await env.new_tenant("traced")
    headers = {"X-API-Key": key}
    cid = (await env.client.post("/v1/collections", json={"name": "c"}, headers=headers)).json()[
        "id"
    ]
    SPANS.clear()

    files = {"file": ("doc.md", b"Tracing works. It crosses the queue.", "text/markdown")}
    assert (
        await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=headers)
    ).status_code == 202
    jobs_before = _metric("corpus_jobs_total", {"kind": "ingest_document", "outcome": "succeeded"})
    assert await env.worker.drain() == 1

    spans = {s.name: s for s in SPANS.get_finished_spans()}
    enqueue, job = spans["enqueue ingest_document"], spans["job ingest_document"]
    # The worker's span is a child of the span that enqueued the job, in the same trace...
    assert job.context.trace_id == enqueue.context.trace_id
    assert job.parent is not None and job.parent.span_id == enqueue.context.span_id
    assert job.attributes["job.outcome"] == "succeeded"
    # ...and every ingestion stage hangs under the job span.
    for stage in ("parse", "chunk", "embed", "store"):
        s = spans[f"ingest.{stage}"]
        assert s.parent is not None and s.parent.span_id == job.context.span_id
    assert (
        _metric("corpus_jobs_total", {"kind": "ingest_document", "outcome": "succeeded"})
        == jobs_before + 1
    )


async def test_retrieval_and_generation_are_instrumented(env: Env) -> None:
    _, key = await env.new_tenant("metered")
    headers = {"X-API-Key": key}
    cid = (await env.client.post("/v1/collections", json={"name": "c"}, headers=headers)).json()[
        "id"
    ]
    files = {"file": ("doc.md", b"The warranty lasts three years.", "text/markdown")}
    await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=headers)
    await env.worker.drain()
    SPANS.clear()
    vector_before = _metric("corpus_retrieval_stage_seconds_count", {"stage": "vector"})

    async with env.client.stream(
        "POST", f"/v1/collections/{cid}/chat", json={"question": "warranty?"}, headers=headers
    ) as r:
        assert r.headers["x-request-id"]
        _ = [chunk async for chunk in r.aiter_text()]

    names = {s.name for s in SPANS.get_finished_spans()}
    assert {"retrieval", "retrieval.embed_query", "retrieval.vector_search"} <= names
    assert {"retrieval.keyword_search", "llm.generate"} <= names
    assert _metric("corpus_retrieval_stage_seconds_count", {"stage": "vector"}) == vector_before + 1
    assert _metric("corpus_llm_time_to_first_token_seconds_count", {"model": "fake"}) >= 1
    assert _metric("corpus_llm_generation_seconds_count", {"model": "fake"}) >= 1


async def test_security_events_are_counted(env: Env) -> None:
    auth_before = _metric("corpus_auth_failures_total")
    limited_before = _metric("corpus_rate_limited_total")
    await env.client.get("/v1/collections", headers={"X-API-Key": "wrong"})
    _, key = await env.new_tenant("tiny", rate_limit=1)
    for _ in range(2):
        await env.client.get("/v1/collections", headers={"X-API-Key": key})
    assert _metric("corpus_auth_failures_total") == auth_before + 1
    assert _metric("corpus_rate_limited_total") == limited_before + 1


async def test_worker_publishes_queue_depth(env: Env) -> None:
    _, key = await env.new_tenant("queued")
    headers = {"X-API-Key": key}
    cid = (await env.client.post("/v1/collections", json={"name": "c"}, headers=headers)).json()[
        "id"
    ]
    for i in range(3):
        files = {"file": (f"d{i}.md", f"Document number {i}.".encode(), "text/markdown")}
        await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=headers)

    await env.worker.refresh_queue_metrics()
    assert _metric("corpus_queue_jobs", {"status": "queued"}) == 3
    assert _metric("corpus_queue_oldest_ready_seconds") >= 0

    await env.worker.drain()
    await env.worker.refresh_queue_metrics()
    assert _metric("corpus_queue_jobs", {"status": "queued"}) == 0
    assert _metric("corpus_queue_jobs", {"status": "succeeded"}) == 3
    assert _metric("corpus_queue_oldest_ready_seconds") == 0
