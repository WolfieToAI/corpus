"""Index the evaluation corpus with the real ingestion pipeline, in a throwaway tenant."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.config import Settings
from corpus.core.embeddings import Embedder
from corpus.db.models import Collection, Document, DocumentBlob, DocumentStatus, Tenant
from corpus.db.session import system_session, tenant_session
from corpus.evaluation.dataset import Dataset
from corpus.jobs.worker import Worker, WorkerConfig
from corpus.services.ingestion import INGEST_DOCUMENT, IngestDocumentHandler, enqueue_ingestion
from corpus.services.tenancy import create_tenant


@dataclass(frozen=True, slots=True)
class IndexedCorpus:
    tenant_id: uuid.UUID
    collection_id: uuid.UUID
    chunk_count: int


@asynccontextmanager
async def indexed_corpus(
    sm: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
    dataset: Dataset,
) -> AsyncIterator[IndexedCorpus]:
    """Ingest every corpus document through the queue + worker, then delete the tenant."""
    async with system_session(sm) as s:
        tenant = await create_tenant(s, f"eval-{uuid.uuid4().hex[:12]}", 1_000_000)
        await s.commit()
        tenant_id = tenant.id

    try:
        async with tenant_session(sm, tenant_id) as s:
            collection = Collection(tenant_id=tenant_id, name="eval")
            s.add(collection)
            await s.flush()
            for doc in dataset.documents:
                data = doc.text.encode()
                document = Document(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    collection_id=collection.id,
                    filename=doc.filename,
                    content_type="text/markdown",
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
                s.add(document)
                await s.flush()
                s.add(DocumentBlob(document_id=document.id, tenant_id=tenant_id, data=data))
                await enqueue_ingestion(s, document, max_attempts=1)
            await s.commit()
            collection_id = collection.id

        worker = Worker(
            sm,
            {INGEST_DOCUMENT: IngestDocumentHandler(sm, embedder, settings)},
            WorkerConfig(backoff_base=0),
        )
        await worker.drain()

        async with tenant_session(sm, tenant_id) as s:
            docs = (await s.scalars(select(Document))).all()
            failed = [d.filename for d in docs if d.status != DocumentStatus.READY]
            if failed:
                raise RuntimeError(f"ingestion failed for {failed}")
            chunk_count = sum(d.chunk_count or 0 for d in docs)

        yield IndexedCorpus(tenant_id, collection_id, chunk_count)
    finally:
        async with system_session(sm) as s:
            tenant_row = await s.get(Tenant, tenant_id)
            if tenant_row is not None:
                await s.delete(tenant_row)
                await s.commit()
