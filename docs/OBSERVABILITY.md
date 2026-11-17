# Observability and load testing

Corpus exposes the three signals an operator needs, correlated by a single id:

| Signal | Tool | Answers |
|--------|------|---------|
| Metrics | Prometheus + Grafana | *Is it healthy? Since when? How fast?* |
| Traces | OpenTelemetry -> Jaeger | *Where did this request spend its time?* |
| Logs | JSON on stdout | *What exactly happened in this request?* |

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318 docker compose --profile observability up --build
```

| UI | URL |
|----|-----|
| Grafana (dashboard "Corpus - overview", anonymous access) | <http://localhost:3000> |
| Prometheus (targets, alerts) | <http://localhost:9090> |
| Jaeger (traces) | <http://localhost:16686> |
| API metrics / worker metrics | <http://localhost:8000/metrics> / port 9100 of each worker |

## Metrics

Labels are low-cardinality on purpose: route *templates* (`/v1/collections/{collection_id}/search`,
never raw ids) and no tenant id - one time series per customer does not scale with the customer
base. Per-tenant detail lives in traces (`tenant.id` span attribute) and logs.

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `corpus_http_requests_total` | counter | method, route, status | requests served |
| `corpus_http_request_duration_seconds` | histogram | method, route | until the **last byte**: a streamed chat answer counts in full |
| `corpus_http_requests_in_flight` | gauge | | concurrency |
| `corpus_retrieval_stage_seconds` | histogram | stage = embed, vector, keyword, rerank, total | where retrieval time goes |
| `corpus_retrieval_sources` | histogram | | sources returned per query |
| `corpus_llm_time_to_first_token_seconds` | histogram | model | perceived chat responsiveness |
| `corpus_llm_generation_seconds` | histogram | model | full answer time |
| `corpus_llm_tokens_total` | counter | model, direction | cost driver |
| `corpus_llm_errors_total` | counter | reason = refusal, error, client_disconnected | |
| `corpus_ingest_stage_seconds` | histogram | stage = parse, chunk, embed, store | ingestion profile |
| `corpus_ingest_chunks_total` | counter | | chunks written |
| `corpus_jobs_total` | counter | kind, outcome = succeeded, retried, dead | queue throughput and health |
| `corpus_job_duration_seconds` | histogram | kind | |
| `corpus_queue_jobs` | gauge | status | queue depth (published by every worker) |
| `corpus_queue_oldest_ready_seconds` | gauge | | **ingestion lag**: how long the oldest ready job has waited |
| `corpus_auth_failures_total` / `corpus_rate_limited_total` | counter | | security events |
| `corpus_load_shed_total` | counter | | requests answered 503 because the DB pool was exhausted |

Each worker replica serves its own `/metrics`; Prometheus discovers all of them through DNS
(`dns_sd_configs`), so `docker compose up --scale worker=5` needs no configuration change.

### Alerts

[`observability/alerts.yml`](../observability/alerts.yml) alerts on **symptoms users feel**, not on
internal causes: 5xx ratio above 2 %, search p95 above 500 ms, a document waiting more than five
minutes for a worker, dead-lettered jobs, failing or refused generations.

## Traces

Tracing is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set (the instrumentation then costs
nothing). When on, FastAPI requests, every SQL statement, outgoing HTTP calls (LLM, Voyage) and
each pipeline stage are spans.

**One trace follows a document across the queue.** When the API enqueues an ingestion job, the
W3C `traceparent` of the enqueuing span is stored in the job payload; the worker that claims the
job - possibly minutes later, on another machine - starts its span as a child of it:

```
POST /v1/collections/{collection_id}/documents      (API)
 └─ enqueue ingest_document                          PRODUCER span
     └─ job ingest_document                          CONSUMER span (worker), job.attempt=1
         ├─ ingest.parse
         ├─ ingest.chunk
         ├─ ingest.embed                             chunks=12
         └─ ingest.store
```

A chat request shows `retrieval.embed_query` overlapping `retrieval.keyword_search`, then
`retrieval.vector_search`, the optional `retrieval.rerank`, and `llm.generate` with the time to
first token and token counts as attributes. Integration tests assert this structure.

## Logs

JSON, one object per line, with `request_id` and - when tracing is on - `trace_id` / `span_id`.
`X-Request-ID` is accepted from the client when well-formed (or minted) and returned on every
response, so the id in a customer's bug report leads straight to the logs and the trace. Extra
fields passed with `logger.info(..., extra={...})` become top-level keys.

```json
{"ts": "2026-11-15T22:31:07.412+00:00", "level": "INFO", "logger": "corpus.services.ingestion",
 "message": "document ingested", "request_id": null, "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
 "span_id": "00f067aa0ba902b7", "document_id": "9d1c...", "pages": 1, "chunks": 12}
```

## Load testing

[`loadtest/k6/corpus.js`](../loadtest/k6/corpus.js) drives three concurrent scenarios at constant
arrival rates - searches, chats and document uploads - against the production Docker image (API,
two workers, PostgreSQL), after indexing the evaluation corpus in a dedicated tenant. Answers use
`LLM_PROVIDER=extractive`, so the numbers measure the platform (auth, rate limiting, retrieval with
real embeddings, streaming, queue) rather than an LLM provider's latency. The *Load test* workflow
runs it on every push to `main`; other rates can be launched from the Actions tab.

### Results

GitHub Actions runner (2 vCPU shared by PostgreSQL, the API, two workers and k6 itself), local
multilingual embeddings, chat at 5 req/s and one upload per second running alongside the searches.
Shared CI machines are noisy: expect run-to-run variation of tens of milliseconds.

| Searches / s | Search p50 | Search p95 | Chat p95 (full answer) | Upload p95 | Errors |
|---:|---:|---:|---:|---:|---:|
| 20 | 37 ms | 144 ms | 180 ms | 56 ms | 0 % |
| 40 | 23 ms | 77 ms | 80 ms | 27 ms | 0 % |
| 60 (overload) | 2.8 s | 6.2 s | 7.9 s | 8.7 s | 30 % shed as `503` |

All 60 documents uploaded during each nominal run were indexed by the time the load stopped:
the two workers keep up with one document per second while searches run.

**Capacity on this machine is about 40 searches/s** (plus the chat and ingestion traffic). The
limit is CPU - query embeddings, PostgreSQL and the workers share two cores. Past it, the API
degrades gracefully: requests that cannot get a database connection within 5 s are answered
`503` with `Retry-After`, and latency returns to normal as soon as the load drops. Scaling out is
horizontal: the API is stateless (run more replicas behind a load balancer), workers scale with
`--scale worker=N`, and moving embeddings to a GPU or a hosted provider removes the main CPU cost.

### What the load test caught

The first 60 req/s run did not degrade - it **collapsed**: 97 % of requests failed after hanging for
exactly 30 seconds, and only 6.6 req/s got through. Thirty seconds is SQLAlchemy's pool timeout,
which pointed at the cause: **a connection-pool deadlock**.

A search held the connection that had loaded its collection (an idle transaction) while acquiring
two more for its concurrent vector and keyword searches. Once the pool was full of requests each
holding one connection and waiting for another, no request could progress until the timeout.
Tests never saw it: the bug only appears when concurrency exceeds the pool size.

The fix:

1. **One connection per request.** The keyword search runs while the query embedding is computed
   (CPU only), then the vector search and the chunk fetch reuse the same connection.
2. **Release early.** The collection lookup commits its read transaction immediately; the
   session's `after_begin` hook re-applies the tenant context on the next transaction.
3. **Fail fast.** A 5 s pool timeout mapped to `503 + Retry-After` (`corpus_load_shed_total`)
   instead of 30 s of queueing.
4. **Regression test.** Ten concurrent searches against a two-connection pool - guaranteed to
   deadlock before the fix - now all succeed.

Side effect: lower latency at every load level, since requests no longer wait for connections
held by idle transactions (search p95 at 20 req/s went from 233 ms to 144 ms).
