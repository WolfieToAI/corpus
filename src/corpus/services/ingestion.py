"""Ingestion: parse -> chunk -> embed -> store, run by the job queue.

The handler is idempotent, as at-least-once delivery requires: chunks are replaced (delete +
insert) in a single transaction, so a retry after a crash never duplicates them.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from itertools import batched
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.config import Settings
from corpus.core.chunking import TextChunk, chunk_pages
from corpus.core.embeddings import Embedder
from corpus.db.models import Chunk, Document, DocumentBlob, DocumentStatus
from corpus.db.session import tenant_session
from corpus.jobs import queue
from corpus.jobs.queue import ClaimedJob, PermanentJobError
from corpus.observability.instrument import stage
from corpus.observability.metrics import INGEST_CHUNKS, INGEST_OCR_PAGES, INGEST_STAGE
from corpus.services.parsers import OcrFn, parse_document_with_stats

logger = logging.getLogger(__name__)

INGEST_DOCUMENT = "ingest_document"


@contextmanager
def _stage(name: str, **attributes: int) -> Iterator[None]:
    with stage(f"ingest.{name}", INGEST_STAGE.labels(name), **attributes):
        yield


async def enqueue_ingestion(
    session: AsyncSession, document: Document, *, max_attempts: int
) -> int | None:
    """Schedule ingestion in the caller's transaction (atomic with the document row)."""
    return await queue.enqueue(
        session,
        tenant_id=document.tenant_id,
        kind=INGEST_DOCUMENT,
        payload={"document_id": str(document.id)},
        idempotency_key=f"{INGEST_DOCUMENT}:{document.id}",
        max_attempts=max_attempts,
    )


class IngestDocumentHandler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        settings: Settings,
        ocr: OcrFn | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._embedder = embedder
        self._settings = settings
        self._ocr = ocr

    async def run(self, job: ClaimedJob) -> None:
        try:
            await self._ingest(job)
        except PermanentJobError:
            raise
        except Exception as exc:
            if job.attempts < job.max_attempts:
                await self._set_status(
                    job,
                    DocumentStatus.PENDING,
                    f"attempt {job.attempts}/{job.max_attempts} failed, retrying: "
                    f"{type(exc).__name__}: {exc}",
                )
            raise

    async def on_dead(self, job: ClaimedJob, error: str) -> None:
        await self._set_status(job, DocumentStatus.FAILED, error)

    async def _ingest(self, job: ClaimedJob) -> None:
        document_id = uuid.UUID(job.payload["document_id"])

        async with tenant_session(self._sm, job.tenant_id) as session:
            document = await session.get(Document, document_id)
            if document is None:
                logger.info("document %s was deleted before ingestion; skipping", document_id)
                return
            blob = await session.get(DocumentBlob, document_id)
            if blob is None:
                raise PermanentJobError("document content is missing")
            document.status = DocumentStatus.PROCESSING
            document.error = None
            data, filename, content_type = blob.data, document.filename, document.content_type
            await session.commit()

        # Parsing is deterministic: if it fails once it will always fail - do not retry.
        with _stage("parse"):
            try:
                pages, ocr_pages = await asyncio.to_thread(
                    partial(parse_document_with_stats, ocr=self._ocr),
                    data,
                    filename,
                    content_type,
                )
            except Exception as exc:
                raise PermanentJobError(f"{type(exc).__name__}: {exc}") from exc
        INGEST_OCR_PAGES.inc(ocr_pages)

        with _stage("chunk"):
            text_chunks = chunk_pages(
                pages,
                max_tokens=self._settings.chunk_max_tokens,
                overlap_tokens=self._settings.chunk_overlap_tokens,
            )
        # Network/model errors here are transient: let them propagate so the job is retried.
        vectors: list[list[float]] = []
        with _stage("embed", chunks=len(text_chunks)):
            batch_size = self._settings.embedding_batch_size
            for batch in batched((c.content for c in text_chunks), batch_size):
                vectors.extend(await self._embedder.embed_documents(batch))

        with _stage("store"):
            await self._store(
                job, document_id, pages_count=len(pages), chunks=text_chunks, vectors=vectors
            )
        INGEST_CHUNKS.inc(len(text_chunks))
        logger.info(
            "document ingested",
            extra={
                "document_id": str(document_id),
                "pages": len(pages),
                "chunks": len(text_chunks),
                "ocr_pages": ocr_pages,
            },
        )

    async def _store(
        self,
        job: ClaimedJob,
        document_id: uuid.UUID,
        *,
        pages_count: int,
        chunks: list[TextChunk],
        vectors: list[list[float]],
    ) -> None:
        """Replace the document's chunks atomically (idempotent under retries)."""
        async with tenant_session(self._sm, job.tenant_id) as session:
            document = await session.get(Document, document_id)
            if document is None:
                return
            await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
            session.add_all(
                Chunk(
                    tenant_id=document.tenant_id,
                    document_id=document.id,
                    collection_id=document.collection_id,
                    ordinal=c.ordinal,
                    content=c.content,
                    page_start=c.page_start,
                    page_end=c.page_end,
                    token_count=c.token_count,
                    embedding=v,
                )
                for c, v in zip(chunks, vectors, strict=True)
            )
            document.page_count = pages_count
            document.chunk_count = len(chunks)
            document.status = DocumentStatus.READY
            document.error = None
            await session.commit()

    async def _set_status(self, job: ClaimedJob, status: DocumentStatus, error: str) -> None:
        values: dict[str, Any] = {"status": status, "error": error[:2000]}
        async with tenant_session(self._sm, job.tenant_id) as session:
            document = await session.get(Document, uuid.UUID(job.payload["document_id"]))
            if document is not None:
                for key, value in values.items():
                    setattr(document, key, value)
                await session.commit()
