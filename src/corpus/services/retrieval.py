"""Retrieval pipeline.

1. **Candidate generation** - pgvector cosine search and PostgreSQL full-text search, run
   concurrently (``mode`` can restrict to one of them, which the evaluation uses as baselines).
2. **Fusion** - Reciprocal Rank Fusion of the candidate lists.
3. **Re-ranking** (optional) - a cross-encoder re-orders the top ``rerank_candidates``.
4. **Selection** - the best ``top_k`` chunks become numbered sources for the prompt.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from corpus.core.embeddings import Embedder
from corpus.core.fusion import reciprocal_rank_fusion
from corpus.core.prompting import Source
from corpus.core.reranking import Reranker
from corpus.core.text_search import query_terms
from corpus.db.models import Chunk
from corpus.db.session import tenant_session
from corpus.observability.instrument import stage
from corpus.observability.metrics import RETRIEVAL_SOURCES, RETRIEVAL_STAGE, Timer
from corpus.observability.tracing import tracer

RetrievalMode = Literal["hybrid", "vector", "keyword"]


@dataclass(frozen=True, slots=True)
class RetrievalParams:
    mode: RetrievalMode = "hybrid"
    candidates: int = 30
    top_k: int = 6
    rrf_k: int = 60
    rerank_candidates: int = 20


async def _vector_search(
    session: AsyncSession, collection_id: uuid.UUID, query_vector: list[float], limit: int
) -> list[uuid.UUID]:
    distance = Chunk.embedding.cosine_distance(query_vector)
    stmt = (
        select(Chunk.id).where(Chunk.collection_id == collection_id).order_by(distance).limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def _keyword_search(
    session: AsyncSession, collection_id: uuid.UUID, query: str, limit: int
) -> list[uuid.UUID]:
    terms = query_terms(query)
    if not terms:
        return []
    # OR of the content terms. Each term goes through plainto_tsquery, which parses arbitrary
    # text safely (no tsquery syntax injection); `||` is the tsquery OR operator.
    ts_query: ColumnElement[Any] = func.plainto_tsquery("simple", terms[0])
    for term in terms[1:]:
        ts_query = ts_query.op("||")(func.plainto_tsquery("simple", term))
    rank = func.ts_rank_cd(Chunk.tsv, ts_query)
    stmt = (
        select(Chunk.id)
        .where(Chunk.collection_id == collection_id, Chunk.tsv.op("@@")(ts_query))
        .order_by(rank.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def retrieve(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    tenant_id: uuid.UUID,
    collection_id: uuid.UUID,
    query: str,
    params: RetrievalParams,
    reranker: Reranker | None = None,
) -> list[Source]:
    with (
        tracer().start_as_current_span(
            "retrieval",
            attributes={"retrieval.mode": params.mode, "retrieval.rerank": reranker is not None},
        ) as span,
        Timer(RETRIEVAL_STAGE.labels("total")),
    ):
        sources = await _retrieve(
            sessionmaker, embedder, tenant_id, collection_id, query, params, reranker
        )
        span.set_attribute("retrieval.sources", len(sources))
        RETRIEVAL_SOURCES.observe(len(sources))
        return sources


async def _retrieve(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    tenant_id: uuid.UUID,
    collection_id: uuid.UUID,
    query: str,
    params: RetrievalParams,
    reranker: Reranker | None,
) -> list[Source]:
    # Exactly one database connection per query. The keyword search runs on it while the query
    # embedding is computed (CPU, no database), then the vector search and the chunk fetch reuse
    # it. A request never holds a connection while waiting for a second one - the load test
    # showed that pattern deadlocking the pool under load (see docs/OBSERVABILITY.md).
    async with tenant_session(sessionmaker, tenant_id) as session:

        async def embed() -> list[float] | None:
            if params.mode == "keyword":
                return None
            with stage("retrieval.embed_query", RETRIEVAL_STAGE.labels("embed")):
                return await embedder.embed_query(query)

        async def keyword() -> list[uuid.UUID]:
            if params.mode == "vector":
                return []
            with stage("retrieval.keyword_search", RETRIEVAL_STAGE.labels("keyword")):
                return await _keyword_search(session, collection_id, query, params.candidates)

        query_vector, lexical = await asyncio.gather(embed(), keyword())
        rankings: list[list[uuid.UUID]] = []
        if query_vector is not None:
            with stage("retrieval.vector_search", RETRIEVAL_STAGE.labels("vector")):
                rankings.append(
                    await _vector_search(session, collection_id, query_vector, params.candidates)
                )
        if params.mode != "vector":
            rankings.append(lexical)
        fused = reciprocal_rank_fusion(rankings, k=params.rrf_k)

        pool_size = max(params.top_k, params.rerank_candidates) if reranker else params.top_k
        pool = fused[:pool_size]
        if not pool:
            return []

        rows = await session.scalars(
            select(Chunk)
            .where(Chunk.id.in_([chunk_id for chunk_id, _ in pool]))
            .options(joinedload(Chunk.document))
        )
        by_id = {c.id: c for c in rows.unique()}

    # Keep fusion order; drop ids deleted between the queries.
    ranked = [(by_id[cid], score) for cid, score in pool if cid in by_id]
    if reranker is not None and ranked:
        with stage("retrieval.rerank", RETRIEVAL_STAGE.labels("rerank"), candidates=len(ranked)):
            scores = await reranker.rerank(query, [chunk.content for chunk, _ in ranked])
        ranked = sorted(
            ((chunk, score) for (chunk, _), score in zip(ranked, scores, strict=True)),
            key=lambda pair: pair[1],
            reverse=True,
        )

    return [
        Source(
            number=i,
            document_id=str(chunk.document_id),
            filename=chunk.document.filename,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            content=chunk.content,
            score=round(score, 6),
        )
        for i, (chunk, score) in enumerate(ranked[: params.top_k], start=1)
    ]
