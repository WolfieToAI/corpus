"""Retrieval modes, re-ranking and conversational rewriting, end to end."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import pytest

from corpus.core.generation import Turn
from corpus.services.retrieval import RetrievalParams, retrieve
from tests.conftest import Env

pytestmark = pytest.mark.integration

DOCS = {
    "warranty.md": "Warranty. The R7 robot vacuum has a three year warranty on its body.",
    "errors.md": "Error codes. E-317 means that a drive wheel is stuck under furniture.",
    "battery.md": "Battery. A full charge takes about four hours on the standard dock.",
}


class KeywordReranker:
    """Scores passages by whether they mention a chosen word: makes re-ranking observable."""

    def __init__(self, word: str) -> None:
        self.word = word
        self.calls = 0

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        self.calls += 1
        return [1.0 if self.word in p else 0.0 for p in passages]


class FixedRewriter:
    def __init__(self, output: str) -> None:
        self.output = output
        self.seen: list[tuple[str, int]] = []

    async def rewrite(self, question: str, history: Sequence[Turn]) -> str:
        self.seen.append((question, len(history)))
        return self.output


async def _indexed(env: Env) -> tuple[dict[str, str], uuid.UUID, str]:
    tenant_id, key = await env.new_tenant("acme")
    headers = {"X-API-Key": key}
    cid = (await env.client.post("/v1/collections", json={"name": "kb"}, headers=headers)).json()[
        "id"
    ]
    for name, text in DOCS.items():
        files = {"file": (name, text.encode(), "text/markdown")}
        r = await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=headers)
        assert r.status_code == 202
    assert await env.worker.drain() == len(DOCS)
    return headers, tenant_id, cid


async def test_keyword_mode_finds_exact_codes(env: Env) -> None:
    headers, _, cid = await _indexed(env)
    r = await env.client.post(
        f"/v1/collections/{cid}/search", json={"query": "E-317", "mode": "keyword"}, headers=headers
    )
    sources = r.json()["sources"]
    assert [s["filename"] for s in sources] == ["errors.md"]  # full-text only returns matches


async def test_vector_mode_returns_nearest_neighbours(env: Env) -> None:
    headers, _, cid = await _indexed(env)
    r = await env.client.post(
        f"/v1/collections/{cid}/search",
        json={"query": "warranty body", "mode": "vector", "top_k": 3},
        headers=headers,
    )
    sources = r.json()["sources"]
    assert len(sources) == 3  # vector search always ranks every chunk
    assert sources[0]["filename"] == "warranty.md"


async def test_reranker_reorders_candidates(env: Env) -> None:
    headers, tenant_id, cid = await _indexed(env)
    reranker = KeywordReranker("charge")
    env.app.state.reranker = reranker

    r = await env.client.post(
        f"/v1/collections/{cid}/search",
        json={"query": "warranty", "mode": "vector", "top_k": 3},
        headers=headers,
    )
    assert r.json()["sources"][0]["filename"] == "battery.md"
    assert reranker.calls == 1

    r = await env.client.post(
        f"/v1/collections/{cid}/search",
        json={"query": "warranty", "mode": "vector", "top_k": 3, "rerank": False},
        headers=headers,
    )
    assert r.json()["sources"][0]["filename"] == "warranty.md"
    assert reranker.calls == 1  # opting out skips the cross-encoder

    # Service level: re-ranking widens the pool to rerank_candidates before cutting to top_k.
    sources = await retrieve(
        env.sm,
        env.app.state.embedder,
        tenant_id,
        uuid.UUID(cid),
        "warranty",
        RetrievalParams(mode="vector", top_k=1, rerank_candidates=10),
        reranker=reranker,
    )
    assert [s.filename for s in sources] == ["battery.md"]


async def _chat(env: Env, headers: dict[str, str], cid: str, body: dict[str, object]) -> list:  # type: ignore[type-arg]
    async with env.client.stream(
        "POST", f"/v1/collections/{cid}/chat", json=body, headers=headers
    ) as r:
        assert r.status_code == 200
        raw = "".join([chunk async for chunk in r.aiter_text()])
    return [
        (block.split("\n")[0].removeprefix("event: "), json.loads(block.split("\n")[1][6:]))
        for block in raw.strip().split("\n\n")
    ]


async def test_follow_up_questions_are_rewritten_before_retrieval(env: Env) -> None:
    headers, _, cid = await _indexed(env)
    rewriter = FixedRewriter("What does error code E-317 mean?")
    env.app.state.rewriter = rewriter

    events = await _chat(
        env,
        headers,
        cid,
        {
            "question": "And that one?",
            "history": [
                {"role": "user", "content": "What is the warranty?"},
                {"role": "assistant", "content": "Three years [1]."},
            ],
        },
    )
    assert events[0] == (
        "query",
        {"question": "And that one?", "search_query": "What does error code E-317 mean?"},
    )
    assert events[1][0] == "sources" and events[1][1][0]["filename"] == "errors.md"
    assert rewriter.seen == [("And that one?", 2)]


async def test_standalone_questions_skip_rewriting(env: Env) -> None:
    headers, _, cid = await _indexed(env)
    rewriter = FixedRewriter("should not be used")
    env.app.state.rewriter = rewriter
    events = await _chat(env, headers, cid, {"question": "How long does a full charge take?"})
    assert events[0][1]["search_query"] == "How long does a full charge take?"
    assert rewriter.seen == []
