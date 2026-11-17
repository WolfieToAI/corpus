"""End-to-end API flow against PostgreSQL: upload -> queue -> worker -> search -> chat."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from corpus import __version__
from corpus.db.models import Chunk
from corpus.db.session import system_session
from tests.conftest import Env

pytestmark = pytest.mark.integration

MANUAL = """Coffee machine X200 - user manual

Warranty. The X200 is covered by a two-year warranty from the date of purchase.

Descaling. Descale the machine every three months with the provided descaling agent.

Safety. Never immerse the base of the machine in water.
"""


async def _setup(env: Env) -> tuple[dict[str, str], str]:
    _, key = await env.new_tenant("acme")
    headers = {"X-API-Key": key}
    r = await env.client.post("/v1/collections", json={"name": "manuals"}, headers=headers)
    assert r.status_code == 201
    return headers, r.json()["id"]


async def _upload(env: Env, headers: dict[str, str], cid: str) -> str:
    files = {"file": ("x200.md", MANUAL.encode(), "text/markdown")}
    r = await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=headers)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending"
    return str(r.json()["id"])


async def test_health_is_public(env: Env) -> None:
    r = await env.client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": True, "version": __version__}


@pytest.mark.parametrize("key", [None, "nope", "crp_" + "a" * 12 + "_" + "b" * 43])
async def test_requests_without_a_valid_key_are_rejected(env: Env, key: str | None) -> None:
    headers = {"X-API-Key": key} if key else {}
    r = await env.client.get("/v1/collections", headers=headers)
    assert r.status_code == 401


async def test_full_rag_flow(env: Env) -> None:
    headers, cid = await _setup(env)
    assert (
        await env.client.post("/v1/collections", json={"name": "manuals"}, headers=headers)
    ).status_code == 409

    doc_id = await _upload(env, headers, cid)
    # Nothing is indexed until a worker runs: ingestion is decoupled from the request.
    r = await env.client.post(
        f"/v1/collections/{cid}/search", json={"query": "warranty"}, headers=headers
    )
    assert r.json()["sources"] == []

    assert await env.worker.drain() == 1
    doc = (await env.client.get(f"/v1/documents/{doc_id}", headers=headers)).json()
    assert doc["status"] == "ready", doc
    assert doc["chunk_count"] >= 1

    files = {"file": ("copy.md", MANUAL.encode(), "text/markdown")}
    r = await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=headers)
    assert r.status_code == 409  # same bytes -> deduplicated by content hash

    r = await env.client.post(
        f"/v1/collections/{cid}/search", json={"query": "warranty purchase"}, headers=headers
    )
    assert r.status_code == 200
    sources = r.json()["sources"]
    assert sources and "two-year warranty" in sources[0]["content"]

    async with env.client.stream(
        "POST",
        f"/v1/collections/{cid}/chat",
        json={"question": "How long is the warranty?"},
        headers=headers,
    ) as r:
        assert r.status_code == 200
        body = "".join([chunk async for chunk in r.aiter_text()])

    events = [
        (block.split("\n")[0].removeprefix("event: "), json.loads(block.split("\n")[1][6:]))
        for block in body.strip().split("\n\n")
    ]
    names = [name for name, _ in events]
    assert names[:2] == ["query", "sources"] and names[-1] == "done"
    assert "".join(d["text"] for n, d in events if n == "token").endswith("[1].")
    assert events[-1][1]["cited"] == [1]


async def test_reindex_is_idempotent(env: Env) -> None:
    headers, cid = await _setup(env)
    doc_id = await _upload(env, headers, cid)
    await env.worker.drain()
    first = (await env.client.get(f"/v1/documents/{doc_id}", headers=headers)).json()

    r = await env.client.post(f"/v1/documents/{doc_id}/reindex", headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "pending"
    r = await env.client.post(f"/v1/documents/{doc_id}/reindex", headers=headers)
    assert r.status_code == 409  # a job for this document is already queued

    assert await env.worker.drain() == 1
    second = (await env.client.get(f"/v1/documents/{doc_id}", headers=headers)).json()
    assert second["status"] == "ready"
    assert second["chunk_count"] == first["chunk_count"]
    async with system_session(env.sm) as s:
        stored = await s.scalar(select(func.count()).select_from(Chunk))
    assert stored == first["chunk_count"]  # replaced, not duplicated


async def test_unparseable_document_fails_without_retrying(env: Env) -> None:
    headers, cid = await _setup(env)
    files = {"file": ("broken.pdf", b"%PDF-1.7 not really a pdf", "application/pdf")}
    r = await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=headers)
    doc_id = r.json()["id"]
    assert await env.worker.drain() == 1  # one attempt only: parsing errors are permanent
    doc = (await env.client.get(f"/v1/documents/{doc_id}", headers=headers)).json()
    assert doc["status"] == "failed"
    assert "PermanentJobError" in doc["error"]


async def test_deleted_document_job_is_a_no_op(env: Env) -> None:
    headers, cid = await _setup(env)
    doc_id = await _upload(env, headers, cid)
    assert (await env.client.delete(f"/v1/documents/{doc_id}", headers=headers)).status_code == 204
    assert await env.worker.drain() == 1
    r = await env.client.get("/admin/jobs/stats", headers={"X-Admin-Key": "admin-secret-for-tests"})
    assert r.json()["succeeded"] == 1


async def test_docx_and_html_are_indexed_and_searchable(env: Env) -> None:
    import io
    import zipfile

    headers, cid = await _setup(env)
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>The descaling interval is ninety days.</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
    html = (
        b"<html><body><h1>Returns</h1><p>Returns are accepted within thirty days.</p></body></html>"
    )
    for name, data, ctype in [
        ("care.docx", docx.getvalue(), "application/octet-stream"),  # detected by extension
        ("returns.html", html, "text/html"),
    ]:
        r = await env.client.post(
            f"/v1/collections/{cid}/documents", files={"file": (name, data, ctype)}, headers=headers
        )
        assert r.status_code == 202, r.text
    assert await env.worker.drain() == 2
    docs = (await env.client.get(f"/v1/collections/{cid}/documents", headers=headers)).json()
    assert {d["status"] for d in docs} == {"ready"}

    for query, expected in [
        ("descaling interval", "care.docx"),
        ("returns accepted", "returns.html"),
    ]:
        r = await env.client.post(
            f"/v1/collections/{cid}/search",
            json={"query": query, "mode": "keyword"},
            headers=headers,
        )
        assert r.json()["sources"][0]["filename"] == expected


async def test_rejects_unsupported_files(env: Env) -> None:
    headers, cid = await _setup(env)
    files = {"file": ("image.png", b"\x89PNG", "image/png")}
    r = await env.client.post(f"/v1/collections/{cid}/documents", files=files, headers=headers)
    assert r.status_code == 415


async def test_invalid_history_is_rejected(env: Env) -> None:
    headers, cid = await _setup(env)
    r = await env.client.post(
        f"/v1/collections/{cid}/chat",
        json={"question": "q", "history": [{"role": "assistant", "content": "hi"}]},
        headers=headers,
    )
    assert r.status_code == 422
