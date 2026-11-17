"""Initial multi-tenant schema: tenants, API keys, job queue, rate limits, RLS-protected data.

Revision ID: 0001
Revises:
Create Date: 2026-11-15

Security model
--------------
* ``corpus_tenant`` is a NOLOGIN role. The application switches to it (``SET LOCAL ROLE``) at
  the start of every tenant-scoped transaction and sets ``app.tenant_id``.
* Row-Level Security policies on every tenant table only expose rows whose ``tenant_id``
  matches ``app.tenant_id``, for reads *and* writes (``WITH CHECK``).
* Composite foreign keys ``(parent_id, tenant_id)`` make it impossible, even for buggy code
  running as the owner, to attach a document to another tenant's collection.
"""

from collections.abc import Sequence

from alembic import op

from corpus.config import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("collections", "documents", "document_blobs", "chunks", "jobs")
TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _exec(*statements: str) -> None:
    # asyncpg prepares statements: one SQL command per call.
    for sql in statements:
        op.execute(sql)


def upgrade() -> None:
    dim = get_settings().embedding_dim

    _exec(
        "CREATE EXTENSION IF NOT EXISTS vector",
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'corpus_tenant') THEN
                CREATE ROLE corpus_tenant NOLOGIN;
            END IF;
        END $$
        """,
        "GRANT corpus_tenant TO CURRENT_USER",
    )

    # ------------------------------------------------------------------ system tables
    _exec(
        """
        CREATE TABLE tenants (
            id uuid PRIMARY KEY,
            name varchar(120) NOT NULL UNIQUE,
            rate_limit_per_minute integer NOT NULL CHECK (rate_limit_per_minute > 0),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE api_keys (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            name varchar(120) NOT NULL,
            prefix varchar(16) NOT NULL UNIQUE,
            secret_hash varchar(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_used_at timestamptz,
            revoked_at timestamptz
        )
        """,
        "CREATE INDEX ix_api_keys_tenant ON api_keys (tenant_id)",
        "CREATE TYPE job_status AS ENUM ('queued', 'running', 'succeeded', 'dead')",
        """
        CREATE TABLE jobs (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            kind varchar(64) NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(200),
            status job_status NOT NULL DEFAULT 'queued',
            attempts integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
            run_at timestamptz NOT NULL DEFAULT now(),
            locked_by varchar(200),
            locked_until timestamptz,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        # Partial indexes: the claim query only ever touches ready or leased jobs, so its cost
        # stays flat however much finished history accumulates.
        "CREATE INDEX ix_jobs_ready ON jobs (run_at, id) WHERE status = 'queued'",
        "CREATE INDEX ix_jobs_leases ON jobs (locked_until) WHERE status = 'running'",
        """
        CREATE UNIQUE INDEX uq_jobs_active_idempotency ON jobs (idempotency_key)
        WHERE status IN ('queued', 'running')
        """,
        """
        CREATE FUNCTION notify_job_enqueued() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('corpus_jobs', NEW.kind);
            RETURN NEW;
        END $$
        """,
        """
        CREATE TRIGGER jobs_notify AFTER INSERT ON jobs
        FOR EACH ROW EXECUTE FUNCTION notify_job_enqueued()
        """,
        """
        CREATE TABLE rate_limit_counters (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            window_start timestamptz NOT NULL,
            count integer NOT NULL,
            PRIMARY KEY (tenant_id, window_start)
        )
        """,
    )

    # ------------------------------------------------------------------ tenant data
    _exec(
        "CREATE TYPE document_status AS ENUM ('pending', 'processing', 'ready', 'failed')",
        """
        CREATE TABLE collections (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            name varchar(120) NOT NULL,
            description text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_collection_name UNIQUE (tenant_id, name),
            CONSTRAINT uq_collection_tenant UNIQUE (id, tenant_id)
        )
        """,
        """
        CREATE TABLE documents (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            collection_id uuid NOT NULL,
            filename varchar(255) NOT NULL,
            content_type varchar(100) NOT NULL,
            size_bytes integer NOT NULL,
            sha256 varchar(64) NOT NULL,
            status document_status NOT NULL DEFAULT 'pending',
            error text,
            page_count integer,
            chunk_count integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_document_content UNIQUE (collection_id, sha256),
            CONSTRAINT uq_document_tenant UNIQUE (id, tenant_id),
            CONSTRAINT fk_document_collection FOREIGN KEY (collection_id, tenant_id)
                REFERENCES collections (id, tenant_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE document_blobs (
            document_id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            data bytea NOT NULL,
            CONSTRAINT fk_blob_document FOREIGN KEY (document_id, tenant_id)
                REFERENCES documents (id, tenant_id) ON DELETE CASCADE
        )
        """,
        f"""
        CREATE TABLE chunks (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            document_id uuid NOT NULL,
            collection_id uuid NOT NULL,
            ordinal integer NOT NULL,
            content text NOT NULL,
            page_start integer NOT NULL,
            page_end integer NOT NULL,
            token_count integer NOT NULL,
            embedding vector({dim}) NOT NULL,
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
            CONSTRAINT fk_chunk_document FOREIGN KEY (document_id, tenant_id)
                REFERENCES documents (id, tenant_id) ON DELETE CASCADE,
            CONSTRAINT fk_chunk_collection FOREIGN KEY (collection_id, tenant_id)
                REFERENCES collections (id, tenant_id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX ix_documents_collection ON documents (collection_id)",
        "CREATE INDEX ix_chunks_collection ON chunks (collection_id)",
        "CREATE INDEX ix_chunks_document ON chunks (document_id)",
        """
        CREATE INDEX ix_chunks_embedding_hnsw ON chunks
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
        """,
        "CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)",
    )

    # ------------------------------------------------------------------ row-level security
    for table in TENANT_TABLES:
        _exec(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"""
            CREATE POLICY tenant_isolation ON {table} TO corpus_tenant
            USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})
            """,
        )

    _exec(
        "GRANT USAGE ON SCHEMA public TO corpus_tenant",
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON collections, documents, document_blobs, chunks TO corpus_tenant
        """,
        "GRANT SELECT, INSERT ON jobs TO corpus_tenant",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO corpus_tenant",
    )


def downgrade() -> None:
    # The corpus_tenant role is cluster-wide and may serve other databases: it is kept.
    _exec(
        "DROP TABLE IF EXISTS chunks, document_blobs, documents, collections",
        "DROP TABLE IF EXISTS rate_limit_counters, jobs, api_keys, tenants",
        "DROP FUNCTION IF EXISTS notify_job_enqueued()",
        "DROP TYPE IF EXISTS document_status",
        "DROP TYPE IF EXISTS job_status",
    )
