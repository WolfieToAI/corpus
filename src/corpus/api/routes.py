"""Public HTTP routes (tenant-scoped, authenticated with an API key)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from corpus import __version__
from corpus.api.deps import (
    CollectionDep,
    EmbedderDep,
    GeneratorDep,
    RerankerDep,
    RewriterDep,
    SessionDep,
    SessionmakerDep,
    SettingsDep,
    SystemSessionDep,
    TenantDep,
)
from corpus.api.schemas import (
    ChatRequest,
    CollectionCreate,
    CollectionOut,
    DocumentOut,
    HealthOut,
    SearchRequest,
    SearchResponse,
    SourceOut,
)
from corpus.core.generation import GenerationResult, RefusalError, Turn
from corpus.core.prompting import Source, extract_citations
from corpus.db.models import Collection, Document, DocumentBlob, DocumentStatus
from corpus.observability.metrics import (
    LLM_DURATION,
    LLM_ERRORS,
    LLM_TIME_TO_FIRST_TOKEN,
    LLM_TOKENS,
)
from corpus.observability.tracing import tracer
from corpus.services.ingestion import enqueue_ingestion
from corpus.services.parsers import SUPPORTED_SUFFIXES, SUPPORTED_TYPES
from corpus.services.retrieval import RetrievalMode, RetrievalParams, retrieve

logger = logging.getLogger(__name__)

health_router = APIRouter(tags=["health"])
router = APIRouter(prefix="/v1")

_ALLOWED_TYPES = SUPPORTED_TYPES
_ALLOWED_SUFFIXES = SUPPORTED_SUFFIXES


# --------------------------------------------------------------------------- health
@health_router.get("/health", response_model=HealthOut)
async def health(session: SystemSessionDep, response: Response) -> HealthOut:
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        logger.warning("health check: database unreachable", exc_info=True)
        db_ok = False
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(status="ok" if db_ok else "degraded", database=db_ok, version=__version__)


# --------------------------------------------------------------------------- collections
@router.post(
    "/collections",
    response_model=CollectionOut,
    status_code=status.HTTP_201_CREATED,
    tags=["collections"],
)
async def create_collection(
    body: CollectionCreate, session: SessionDep, tenant: TenantDep
) -> Collection:
    collection = Collection(
        tenant_id=tenant.tenant_id, name=body.name, description=body.description
    )
    session.add(collection)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "a collection with this name exists") from exc
    return collection


@router.get("/collections", response_model=list[CollectionOut], tags=["collections"])
async def list_collections(session: SessionDep) -> list[Collection]:
    # No tenant filter in the query on purpose: Row-Level Security applies it.
    return list((await session.scalars(select(Collection).order_by(Collection.created_at))).all())


@router.delete(
    "/collections/{collection_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["collections"]
)
async def delete_collection(collection: CollectionDep, session: SessionDep) -> None:
    await session.delete(collection)
    await session.commit()


# --------------------------------------------------------------------------- documents
@router.post(
    "/collections/{collection_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["documents"],
    summary="Upload a document; a worker indexes it asynchronously (poll its status).",
)
async def upload_document(
    file: UploadFile,
    collection: CollectionDep,
    session: SessionDep,
    settings: SettingsDep,
) -> Document:
    filename = file.filename or "document"
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if file.content_type not in _ALLOWED_TYPES and suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "supported formats: PDF, DOCX, HTML, TXT, Markdown",
        )

    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"file exceeds {settings.max_upload_mb} MB"
        )
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty file")

    document = Document(
        id=uuid.uuid4(),
        tenant_id=collection.tenant_id,
        collection_id=collection.id,
        filename=filename[:255],
        content_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    session.add(document)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this exact file is already in the collection"
        ) from exc
    session.add(DocumentBlob(document_id=document.id, tenant_id=document.tenant_id, data=data))
    # Document, content and job commit together: no document is ever left without its job,
    # and no job points at a document that does not exist (transactional enqueue).
    await enqueue_ingestion(session, document, max_attempts=settings.job_max_attempts)
    await session.commit()
    return document


@router.get(
    "/collections/{collection_id}/documents", response_model=list[DocumentOut], tags=["documents"]
)
async def list_documents(collection: CollectionDep, session: SessionDep) -> list[Document]:
    stmt = (
        select(Document)
        .where(Document.collection_id == collection.id)
        .order_by(Document.created_at)
    )
    return list((await session.scalars(stmt)).all())


async def _get_document(document_id: uuid.UUID, session: SessionDep) -> Document:
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return document


@router.get("/documents/{document_id}", response_model=DocumentOut, tags=["documents"])
async def get_document(document_id: uuid.UUID, session: SessionDep) -> Document:
    return await _get_document(document_id, session)


@router.post(
    "/documents/{document_id}/reindex",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["documents"],
    summary="Re-run ingestion (e.g. after changing the chunking or embedding settings).",
)
async def reindex_document(
    document_id: uuid.UUID, session: SessionDep, settings: SettingsDep
) -> Document:
    document = await _get_document(document_id, session)
    job_id = await enqueue_ingestion(session, document, max_attempts=settings.job_max_attempts)
    if job_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "ingestion is already queued or running")
    document.status = DocumentStatus.PENDING
    document.error = None
    await session.commit()
    return document


@router.delete(
    "/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["documents"]
)
async def delete_document(document_id: uuid.UUID, session: SessionDep) -> None:
    await session.delete(await _get_document(document_id, session))
    await session.commit()


# --------------------------------------------------------------------------- search & chat
def _params(
    settings: SettingsDep, top_k: int | None, mode: RetrievalMode = "hybrid"
) -> RetrievalParams:
    k = top_k or settings.retrieval_top_k
    return RetrievalParams(
        mode=mode,
        candidates=max(settings.retrieval_candidates, k),
        top_k=k,
        rrf_k=settings.rrf_k,
        rerank_candidates=settings.rerank_candidates,
    )


def _source_out(s: Source) -> SourceOut:
    return SourceOut(
        number=s.number,
        document_id=s.document_id,
        filename=s.filename,
        page_start=s.page_start,
        page_end=s.page_end,
        content=s.content,
        score=s.score,
    )


@router.post(
    "/collections/{collection_id}/search", response_model=SearchResponse, tags=["retrieval"]
)
async def search(
    body: SearchRequest,
    collection: CollectionDep,
    sessionmaker: SessionmakerDep,
    embedder: EmbedderDep,
    reranker: RerankerDep,
    settings: SettingsDep,
) -> SearchResponse:
    sources = await retrieve(
        sessionmaker,
        embedder,
        collection.tenant_id,
        collection.id,
        body.query,
        _params(settings, body.top_k, body.mode),
        reranker=reranker if body.rerank else None,
    )
    return SearchResponse(query=body.query, sources=[_source_out(s) for s in sources])


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post(
    "/collections/{collection_id}/chat",
    summary="Ask a question; the answer streams back as Server-Sent Events.",
    response_class=StreamingResponse,
    tags=["retrieval"],
)
async def chat(
    body: ChatRequest,
    request: Request,
    collection: CollectionDep,
    sessionmaker: SessionmakerDep,
    embedder: EmbedderDep,
    generator: GeneratorDep,
    reranker: RerankerDep,
    rewriter: RewriterDep,
    settings: SettingsDep,
) -> StreamingResponse:
    history = [Turn(role=t.role, content=t.content) for t in body.history]
    search_query = body.question
    if history and rewriter is not None:
        try:
            search_query = await rewriter.rewrite(body.question, history)
        except Exception:
            logger.warning("query rewriting failed; searching with the raw question", exc_info=True)

    sources = await retrieve(
        sessionmaker,
        embedder,
        collection.tenant_id,
        collection.id,
        search_query,
        _params(settings, body.top_k),
        reranker=reranker,
    )

    async def events() -> AsyncIterator[str]:
        yield _sse("query", {"question": body.question, "search_query": search_query})
        yield _sse("sources", [_source_out(s).model_dump() for s in sources])
        result = GenerationResult()
        answer: list[str] = []
        model = str(getattr(generator, "model", "unknown"))
        started = time.perf_counter()
        with tracer().start_as_current_span(
            "llm.generate", attributes={"llm.model": model, "llm.sources": len(sources)}
        ) as span:
            try:
                async for delta in generator.stream_answer(body.question, sources, history, result):
                    if not answer:
                        ttft = time.perf_counter() - started
                        LLM_TIME_TO_FIRST_TOKEN.labels(model).observe(ttft)
                        span.set_attribute("llm.time_to_first_token_ms", round(ttft * 1000))
                    if await request.is_disconnected():
                        logger.info("client disconnected, stopping generation")
                        LLM_ERRORS.labels("client_disconnected").inc()
                        return
                    answer.append(delta)
                    yield _sse("token", {"text": delta})
            except RefusalError as exc:
                LLM_ERRORS.labels("refusal").inc()
                yield _sse("error", {"code": "refusal", "message": str(exc)})
                return
            except Exception:
                LLM_ERRORS.labels("error").inc()
                logger.exception("generation failed")
                yield _sse("error", {"code": "generation_failed", "message": "generation failed"})
                return
            LLM_DURATION.labels(model).observe(time.perf_counter() - started)
            LLM_TOKENS.labels(model, "input").inc(result.input_tokens)
            LLM_TOKENS.labels(model, "output").inc(result.output_tokens)
            span.set_attribute("llm.input_tokens", result.input_tokens)
            span.set_attribute("llm.output_tokens", result.output_tokens)

        yield _sse(
            "done",
            {
                "cited": extract_citations("".join(answer), len(sources)),
                "model": result.model,
                "stop_reason": result.stop_reason,
                "usage": {
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                },
            },
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
