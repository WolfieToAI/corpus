# Changelog

All notable changes to Corpus. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-11-17

First public release: a multi-tenant RAG platform that can be deployed, embedded, measured and
operated.

### Retrieval and generation
- Hybrid retrieval: pgvector (HNSW) and PostgreSQL full-text search (GIN) run concurrently and
  are merged with Reciprocal Rank Fusion; optional cross-encoder re-ranking.
- Conversational query rewriting for follow-up questions.
- Grounded answers streamed over SSE, with numbered sources and inline citations validated
  server-side; explicit "not in the documents" behaviour.
- Provider-agnostic LLM layer: any OpenAI-compatible endpoint, hosted or self-hosted (vLLM,
  Ollama...), configured with `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. Extractive answers
  without an LLM for demos and load tests.

### Ingestion
- PDF, DOCX, HTML, Markdown and text files; OCR with Tesseract (English, French, German) for
  scanned PDF pages, only on pages without a text layer.
- Structure-aware chunking with overlap and page tracking; pluggable embeddings (local
  multilingual ONNX model, Voyage AI).

### Multi-tenancy and security
- Tenant isolation enforced by PostgreSQL Row-Level Security and composite tenant foreign keys.
- Hashed, scoped API keys; publishable widget keys limited to one collection and allowed
  origins; per-tenant rate limiting; admin API.

### Background processing
- Durable PostgreSQL job queue: `SKIP LOCKED` claiming, leases with heartbeats, fencing,
  exponential backoff with jitter, dead-lettering, idempotency keys, `LISTEN/NOTIFY` wake-ups;
  horizontally scalable workers. Alembic migrations.

### Product
- Embeddable chat widget (`/widget.js`): dependency-free Web Component with streamed answers,
  clickable citations, keyboard and screen-reader support, light and dark themes.
- Demo page (`/demo`) and `corpus-demo`, which indexes sample documents.

### Quality and operations
- Labelled evaluation dataset and `corpus-eval` (recall@k, MRR, nDCG, latency; LLM-judged
  faithfulness, correctness, citations and abstention), published by CI.
- Prometheus metrics, Grafana dashboard and alert rules; OpenTelemetry traces from the upload
  request through the queue into the worker; JSON logs correlated by request and trace id.
- k6 load tests of the production image in CI, with load shedding (`503` + `Retry-After`) past
  capacity; a connection-pool regression test.
- Docker image and Compose setup; Render Blueprints for a production deployment and for a free
  single-instance demo (Render + Neon PostgreSQL).

[1.2.0]: https://github.com/WolfieToAI/corpus/releases/tag/v1.2.0
