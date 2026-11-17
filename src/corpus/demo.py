"""``corpus-demo``: index the sample documents and print a ready-to-use demo link.

    corpus-demo [--origin http://localhost:8000] [--public-url http://localhost:8000]

Idempotent: it reuses the ``demo`` tenant and its collection if they exist, indexes the sample
corpus through the real queue and worker, and issues a fresh *widget* key restricted to the
given origins.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.config import Settings, get_settings
from corpus.core.embeddings import Embedder, build_embedder
from corpus.db.models import ApiKey, Collection, Document, DocumentBlob, Tenant
from corpus.db.session import (
    create_engine,
    create_sessionmaker,
    run_migrations,
    system_session,
    tenant_session,
)
from corpus.evaluation.dataset import load_dataset
from corpus.jobs.worker import Worker, WorkerConfig
from corpus.services.ingestion import INGEST_DOCUMENT, IngestDocumentHandler, enqueue_ingestion
from corpus.services.tenancy import create_api_key, create_tenant

DEFAULT_DATASET = Path("eval/dataset")  # relative to the repo root, or /app in the image
TENANT_NAME = "demo"


DEMO_KEY_NAME = "demo widget"


@dataclass(frozen=True, slots=True)
class DemoConfig:
    collection_id: uuid.UUID
    key: str  # widget key plaintext: public by design (search/chat, one collection, origins)
    enqueued: int


async def ensure_demo(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    dataset_dir: Path,
    origins: list[str],
) -> DemoConfig:
    """Create (or reuse) the demo tenant and collection, enqueue missing sample documents and
    issue a fresh widget key, revoking the previous demo keys. Indexing is left to a worker."""
    dataset = load_dataset(dataset_dir)
    async with system_session(sm) as s:
        tenant = await s.scalar(select(Tenant).where(Tenant.name == TENANT_NAME))
        if tenant is None:
            tenant = await create_tenant(s, TENANT_NAME, settings.default_rate_limit_per_minute)
        await s.commit()
        tenant_id = tenant.id

    enqueued = 0
    async with tenant_session(sm, tenant_id) as s:
        collection = await s.scalar(select(Collection).where(Collection.name == "northwind"))
        if collection is None:
            collection = Collection(
                tenant_id=tenant_id, name="northwind", description="Sample documents"
            )
            s.add(collection)
            await s.flush()
        known = set((await s.scalars(select(Document.sha256))).all())
        for doc in dataset.documents:
            data = doc.text.encode()
            digest = hashlib.sha256(data).hexdigest()
            if digest in known:
                continue
            document = Document(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                collection_id=collection.id,
                filename=doc.filename,
                content_type="text/markdown",
                size_bytes=len(data),
                sha256=digest,
            )
            s.add(document)
            await s.flush()
            s.add(DocumentBlob(document_id=document.id, tenant_id=tenant_id, data=data))
            await enqueue_ingestion(s, document, max_attempts=settings.job_max_attempts)
            enqueued += 1
        await s.commit()
        collection_id = collection.id

    async with system_session(sm) as s:
        await s.execute(
            update(ApiKey)
            .where(
                ApiKey.tenant_id == tenant_id,
                ApiKey.name == DEMO_KEY_NAME,
                ApiKey.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
        _, key = await create_api_key(
            s,
            tenant_id,
            DEMO_KEY_NAME,
            scope="widget",
            collection_id=collection_id,
            allowed_origins=origins,
        )
        await s.commit()
    return DemoConfig(collection_id=collection_id, key=key, enqueued=enqueued)


async def seed(
    settings: Settings,
    embedder: Embedder,
    dataset_dir: Path,
    origins: list[str],
    public_url: str,
) -> str:
    """CLI path: seed, index right away (no worker needed) and return the demo URL."""
    await run_migrations(settings.database_url)
    engine = create_engine(settings.database_url)
    sm = create_sessionmaker(engine)
    try:
        demo = await ensure_demo(sm, settings, dataset_dir, origins)
        handler = IngestDocumentHandler(sm, embedder, settings)
        processed = await Worker(sm, {INGEST_DOCUMENT: handler}, WorkerConfig()).drain()
    finally:
        await engine.dispose()

    url = f"{public_url.rstrip('/')}/demo?collection={demo.collection_id}&key={demo.key}"
    print(f"Indexed {processed} new document(s). Widget key allowed on: {', '.join(origins)}")
    print(f"\nOpen the demo:\n  {url}\n")
    return url


def main() -> None:
    parser = argparse.ArgumentParser(prog="corpus-demo", description="Seed the demo tenant.")
    parser.add_argument(
        "--origin",
        action="append",
        dest="origins",
        help="Origin allowed to use the widget key (repeatable). Default: the public URL.",
    )
    parser.add_argument("--public-url", default="http://localhost:8000")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    origins = [o.rstrip("/") for o in (args.origins or [args.public_url])]
    try:
        settings = get_settings()
        asyncio.run(
            seed(settings, build_embedder(settings), args.dataset, origins, args.public_url)
        )
    except FileNotFoundError:
        print(f"dataset not found at {args.dataset} (pass --dataset)", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
