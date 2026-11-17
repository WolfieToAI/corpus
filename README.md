# Corpus

**Multi-tenant "chat with your documents" platform - answers grounded in your sources, with
page-level citations.**

Corpus is a production-grade Retrieval-Augmented Generation (RAG) backend. Each tenant uploads
PDFs (scanned ones included, via OCR), Word, HTML, Markdown or text files; background workers
index them; users ask questions in any language - through the API or a widget embedded in one
line on any website - and get a streamed answer where every claim points to the exact document
and pages it came from.

[![CI](https://github.com/WolfieToAI/corpus/actions/workflows/ci.yml/badge.svg)](https://github.com/WolfieToAI/corpus/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12+-blue)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector%20%2B%20RLS-336791)
![License](https://img.shields.io/badge/license-MIT-green)
[![Release](https://img.shields.io/github/v/release/WolfieToAI/corpus)](https://github.com/WolfieToAI/corpus/releases)

**[Live demo](https://corpus-8jf9.onrender.com/demo)** - ask questions about a sample document set
(free hosting: the first visit after a quiet period takes about a minute to wake the server).
· **[Explication technique en français](docs/EXPLICATION-TECHNIQUE.md)**

```text
> How long is the warranty on the X200?

The X200 is covered by a two-year warranty from the date of purchase [1]. Descaling every
three months is required to keep it valid [2].

[1] x200-manual.pdf, p. 4      [2] x200-manual.pdf, p. 11-12
```

## Highlights

**Multi-tenancy, enforced by the database**
- Tenant isolation through **PostgreSQL Row-Level Security**: each tenant-scoped transaction runs
  as a restricted role with the tenant id in its context, so a query that forgets its `WHERE`
  clause still cannot read or write another tenant's rows.
- **Composite foreign keys** `(parent_id, tenant_id)` make cross-tenant references impossible,
  even for code running with full privileges.
- **Hashed API keys** (`crp_<prefix>_<secret>`, 256-bit secret, SHA-256 digest, constant-time
  check), shown once, revocable; **per-tenant rate limiting** that works across API replicas.

**Durable ingestion pipeline**
- A **PostgreSQL job queue**: `FOR UPDATE SKIP LOCKED` claiming, **leases with heartbeats**
  (crashed workers' jobs are recovered), **fencing** against zombie workers, **exponential
  backoff with jitter**, dead-lettering, idempotency keys.
- **Transactional enqueue**: a document, its content and its job commit atomically.
- Workers wake up instantly via **`LISTEN/NOTIFY`**, with polling as a safety net; scale them
  horizontally with `docker compose up --scale worker=N`.

**Retrieval and generation**
- **Hybrid search**: pgvector (HNSW) semantic search and PostgreSQL full-text search (GIN) run
  concurrently and are merged with **Reciprocal Rank Fusion**; optional **cross-encoder
  re-ranking** of the top candidates.
- **Conversational query rewriting**: "And for the R7?" becomes a standalone search query built
  from the conversation before retrieval.
- **Verifiable answers**: numbered sources, inline `[n]` citations validated server-side, page
  ranges preserved from PDF to answer, explicit "not in the documents" behaviour.
- **Streaming** over Server-Sent Events; **prompt-injection hygiene** (document text is escaped
  data, never instructions); pluggable embeddings (local multilingual ONNX model, Voyage AI).
- **Bring your own LLM**: any OpenAI-compatible endpoint, hosted or self-hosted (vLLM, Ollama),
  chosen by configuration - no vendor lock-in, and data can stay on-premises.

**Measured, not assumed**
- A labelled evaluation set (distractor documents, paraphrases, exact codes, multi-part,
  unanswerable and follow-up questions) and `corpus-eval`, which compares retrieval strategies
  with recall@k, MRR and nDCG on every push, and grades end-to-end answers with an LLM judge.

**Operable in production**
- **Prometheus metrics** for every stage (HTTP, retrieval, LLM time to first token, ingestion,
  queue depth and lag), a provisioned **Grafana dashboard** and symptom-based **alert rules**.
- **OpenTelemetry traces** that follow a document from the upload request, *through the queue*,
  into the worker that indexes it; **JSON logs** correlated by request id and trace id.
- **k6 load tests** of the production image in CI, with graceful **load shedding** (`503 +
  Retry-After`) past capacity.

**Engineering**
- Layered architecture (`api -> services -> core`), `mypy --strict`, Alembic migrations tested
  up *and* down in CI, 190 tests including isolation, concurrency, crash-recovery and
  connection-pool regression scenarios against a real PostgreSQL.

## Retrieval quality

Measured by [`corpus-eval`](docs/EVALUATION.md) on 40 labelled questions, real models, CPU only:

| Strategy | Recall@1 | MRR | nDCG@5 | p50 latency |
|---|---:|---:|---:|---:|
| Full-text only | 0.738 | 0.869 | 0.873 | 7 ms |
| Vector only | 0.550 | 0.717 | 0.740 | 17 ms |
| Hybrid (RRF) | 0.838 | 0.929 | 0.918 | 19 ms |
| **Hybrid + cross-encoder** | **0.900** | **0.958** | **0.945** | 870 ms |

Vector search misses exact identifiers, full-text misses paraphrases; fusion beats both, and the
cross-encoder sharpens the top result at a latency cost - trade-offs, the bug the evaluation
caught, and methodology are in [docs/EVALUATION.md](docs/EVALUATION.md).

## Performance under load

k6 against the production image on a 2-vCPU CI runner (API + 2 workers + PostgreSQL), real
embeddings, with 5 chats/s and 1 upload/s running alongside the searches:

| Searches / s | Search p50 | Search p95 | Chat p95 | Errors |
|---:|---:|---:|---:|---:|
| 20 | 37 ms | 144 ms | 180 ms | 0 % |
| 40 | 23 ms | 77 ms | 80 ms | 0 % |
| 60 (overload) | 2.8 s | 6.2 s | 7.9 s | 30 % shed as `503` |

The first overload run exposed a connection-pool deadlock that made the API collapse instead of
degrading; the diagnosis, the fix and the regression test are in
[docs/OBSERVABILITY.md](docs/OBSERVABILITY.md), with the metrics catalogue and tracing design.

## Embed it on any website

```html
<script src="https://YOUR-API/widget.js"
        data-collection="COLLECTION_ID" data-api-key="crp_WIDGET_KEY" defer></script>
```

A dependency-free Web Component (Shadow DOM, streamed answers, clickable citations that open the
source excerpt, keyboard and screen-reader friendly, light/dark). It uses a **publishable widget
key** that can only search and chat, on one collection, from the origins it was issued for.
Try it locally with `docker compose up -d --build && docker compose run --rm api corpus-demo`,
or put a free live demo online in a few clicks (Render + Neon, no credit card) with
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/WolfieToAI/corpus)
- step by step in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Architecture

```mermaid
flowchart LR
    C[Client] -- "X-API-Key" --> API[FastAPI API]
    API -- "auth + rate limit" --> SYS[(system tables)]
    API -- "document + blob + job<br/>(one transaction)" --> PG[(PostgreSQL<br/>RLS · pgvector · tsvector)]
    PG -- "NOTIFY" --> W1[Worker]
    PG -- "NOTIFY" --> W2[Worker]
    W1 -- "SKIP LOCKED claim<br/>parse · chunk · embed" --> PG
    C -- question --> API
    API -- "hybrid search + RRF" --> PG
    API --> LLM[LLM API]
    LLM -- "SSE stream" --> C
```

Design decisions and request flows: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

Requirements: Docker. An LLM API key enables answer generation (search works without it).

```bash
cp .env.example .env        # set ADMIN_API_KEY (and LLM_API_KEY / LLM_MODEL)
docker compose up --build   # db, migrations, API and 2 workers
```

Interactive API docs: <http://localhost:8000/docs>. No LLM key yet? `LLM_PROVIDER=extractive`
answers offline by quoting the best source.

With metrics, traces and dashboards (Grafana on <http://localhost:3000>, Jaeger on
<http://localhost:16686>):

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318 docker compose --profile observability up --build
```

```bash
ADMIN=<your ADMIN_API_KEY>

# 1. Create a tenant and an API key (the key is displayed once)
TENANT=$(curl -s -X POST localhost:8000/admin/tenants -H "X-Admin-Key: $ADMIN" \
  -H 'Content-Type: application/json' -d '{"name": "acme"}' | jq -r .id)
KEY=$(curl -s -X POST localhost:8000/admin/tenants/$TENANT/api-keys -H "X-Admin-Key: $ADMIN" \
  -H 'Content-Type: application/json' -d '{}' | jq -r .key)

# 2. Create a collection and upload a document (202: a worker indexes it)
CID=$(curl -s -X POST localhost:8000/v1/collections -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"name": "manuals"}' | jq -r .id)
curl -s -X POST localhost:8000/v1/collections/$CID/documents -H "X-API-Key: $KEY" \
  -F file=@x200-manual.pdf

# 3. Ask a question (streamed)
curl -N -X POST localhost:8000/v1/collections/$CID/chat -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"question": "How long is the warranty?"}'
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness + database check |
| `POST` `GET` | `/v1/collections` | Create / list the tenant's collections |
| `DELETE` | `/v1/collections/{id}` | Delete a collection and everything in it |
| `POST` | `/v1/collections/{id}/documents` | Upload a PDF, DOCX, HTML, TXT or Markdown file (queued for indexing) |
| `GET` | `/v1/collections/{id}/documents` | List documents and their status |
| `GET` `DELETE` | `/v1/documents/{id}` | Document status / deletion |
| `POST` | `/v1/documents/{id}/reindex` | Re-run ingestion (idempotent) |
| `POST` | `/v1/collections/{id}/search` | Retrieval only (no LLM); `mode` and `rerank` for debugging |
| `POST` | `/v1/collections/{id}/chat` | Grounded answer, streamed as SSE |
| `POST` `GET` | `/admin/tenants` | Create / list tenants |
| `DELETE` | `/admin/tenants/{id}` | Delete a tenant and all its data |
| `POST` `GET` | `/admin/tenants/{id}/api-keys` | Issue / list API keys |
| `DELETE` | `/admin/api-keys/{id}` | Revoke a key |
| `GET` | `/admin/jobs/stats` | Queue depth by status |

Every `/v1` response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and
`X-RateLimit-Reset`; over the limit the API answers `429` with `Retry-After`.

## Configuration

All settings are environment variables (see [.env.example](.env.example)).

| Variable | Default | Purpose |
|----------|---------|---------|
| `ADMIN_API_KEY` | - | Enables `/admin` (`X-Admin-Key`) |
| `DEFAULT_RATE_LIMIT_PER_MINUTE` | `120` | Rate limit for new tenants |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint (hosted or self-hosted) |
| `LLM_API_KEY` / `LLM_MODEL` | - | Enable `/chat` and query rewriting |
| `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` | `1500` / `0.2` | Generation limits |
| `EMBEDDING_PROVIDER` | `fastembed` | `fastembed`, `voyage` or `hash` |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | multilingual MiniLM / `384` | Embedding model and size |
| `WORKER_CONCURRENCY` | `4` | Jobs run in parallel per worker process |
| `LLM_PROVIDER` | `openai-compatible` | `extractive` answers offline (demos, load tests) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | - | OTLP/HTTP collector; enables tracing |
| `LOG_FORMAT` / `METRICS_ENABLED` | `json` / `true` | Structured logs / `/metrics` endpoints |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT_SECONDS` | `10` / `20` / `5` | Pool sizing; timeout = load-shedding threshold |
| `JOB_MAX_ATTEMPTS` / `JOB_LEASE_SECONDS` | `5` / `300` | Retry budget / lease duration |
| `JOB_BACKOFF_BASE_SECONDS` / `JOB_BACKOFF_MAX_SECONDS` | `5` / `600` | Retry backoff |
| `CHUNK_MAX_TOKENS` / `CHUNK_OVERLAP_TOKENS` | `400` / `60` | Chunking budget |
| `RETRIEVAL_CANDIDATES` / `RETRIEVAL_TOP_K` | `30` / `6` | Candidates per search / sources sent to the LLM |
| `RERANKER` / `RERANKER_MODEL` | `none` / `Xenova/ms-marco-MiniLM-L-6-v2` | `cross-encoder` enables re-ranking |
| `RERANK_CANDIDATES` | `20` | Fused candidates passed to the cross-encoder |
| `QUERY_REWRITING` | `true` | Rewrite follow-up questions (needs an LLM) |

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,local-embeddings]"
docker compose up -d db
alembic upgrade head
uvicorn corpus.main:app --reload      # API
corpus-worker                         # worker, in another terminal

pytest                                # unit tests (integration tests skip without a DB)
TEST_DATABASE_URL=postgresql+asyncpg://corpus:corpus@localhost:5432/corpus pytest
ruff check . && ruff format --check . && mypy src
```

The integration suite drops and rebuilds the schema of the database it is given: point it at a
dedicated database.

## Project status

**1.2.0** - feature-complete for its scope; see the [changelog](CHANGELOG.md).

- [x] Multi-tenancy with Row-Level Security, hashed API keys, rate limiting
- [x] Durable job queue, leases, retries, horizontal workers, Alembic migrations
- [x] Retrieval evaluation harness (recall@k, MRR, nDCG, LLM-judged faithfulness), published in CI
- [x] Cross-encoder re-ranking and conversational query rewriting
- [x] OpenTelemetry tracing, Prometheus metrics, Grafana dashboard, alerts, k6 load tests in CI
- [x] Embeddable chat widget, publishable widget keys, demo page, Render Blueprint
- [x] PDF (with OCR for scans), DOCX, HTML, Markdown and text ingestion

Deliberate scope decisions: original files are stored in PostgreSQL (one transactional store,
cascade deletes, one backup) rather than object storage - the right call up to millions of
documents; an S3 `BlobStore` would be the next step beyond that. The evaluation set is
deliberately small and hand-labelled; growing it is the most valuable next investment.

## License

[MIT](LICENSE)
