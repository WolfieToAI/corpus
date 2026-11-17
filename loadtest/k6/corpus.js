// Load test: concurrent search, chat and ingestion against a running Corpus stack.
//
//   k6 run -e BASE_URL=http://localhost:8000 -e ADMIN_API_KEY=... loadtest/k6/corpus.js
//
// Setup creates a dedicated tenant, indexes the evaluation corpus and waits until it is ready;
// teardown measures how long the workers take to drain the documents uploaded during the test,
// then deletes the tenant. Run the API with LLM_PROVIDER=extractive to measure the platform
// itself (auth, retrieval, streaming, queue) independently of an LLM provider's latency.

import http from "k6/http";
import { check, fail, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const ADMIN = __ENV.ADMIN_API_KEY;
const DURATION = __ENV.DURATION || "60s";
const SEARCH_RPS = Number(__ENV.SEARCH_RPS || 20);
const CHAT_RPS = Number(__ENV.CHAT_RPS || 5);
const INGEST_RPS = Number(__ENV.INGEST_RPS || 1);

const DATASET = "../../eval/dataset";
const QUESTIONS = open(`${DATASET}/questions.jsonl`)
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line))
  .filter((q) => !q.history);
const DOC_NAMES = [
  "fleet-api-reference.md",
  "hr-remote-work-france.md",
  "hr-remote-work-germany.md",
  "pricing-and-sla.md",
  "release-notes.md",
  "security-incident-runbook.md",
  "vacuum-r5-manual.md",
  "vacuum-r7-manual.md",
];
const DOCS = DOC_NAMES.map((name) => ({ name, body: open(`${DATASET}/corpus/${name}`) }));

const chatTimeToFirstByte = new Trend("chat_time_to_first_byte", true);
const searchSources = new Trend("search_sources_returned");
const uploads = new Counter("documents_uploaded");
const drainTime = new Trend("ingest_backlog_drain", true);

export const options = {
  scenarios: {
    search: {
      executor: "constant-arrival-rate",
      exec: "search",
      rate: SEARCH_RPS,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: 20,
      maxVUs: 200,
    },
    chat: {
      executor: "constant-arrival-rate",
      exec: "chat",
      rate: CHAT_RPS,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: 10,
      maxVUs: 100,
    },
    ingest: {
      executor: "constant-arrival-rate",
      exec: "ingest",
      rate: INGEST_RPS,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: 2,
      maxVUs: 20,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{scenario:search}": ["p(95)<1000"],
    "http_req_duration{scenario:chat}": ["p(95)<2000"],
    "http_req_duration{scenario:ingest}": ["p(95)<1000"],
    checks: ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

function adminHeaders() {
  return { "X-Admin-Key": ADMIN, "Content-Type": "application/json" };
}

function upload(ctx, name, body) {
  return http.post(
    `${BASE}/v1/collections/${ctx.cid}/documents`,
    { file: http.file(body, name, "text/markdown") },
    { headers: { "X-API-Key": ctx.key }, tags: { name: "upload" } },
  );
}

function waitUntilIndexed(ctx, timeoutSeconds) {
  const started = Date.now();
  while ((Date.now() - started) / 1000 < timeoutSeconds) {
    const res = http.get(`${BASE}/v1/collections/${ctx.cid}/documents`, {
      headers: { "X-API-Key": ctx.key },
      tags: { name: "poll" },
    });
    const docs = res.json();
    const pending = docs.filter((d) => d.status === "pending" || d.status === "processing");
    const failed = docs.filter((d) => d.status === "failed");
    if (failed.length) fail(`ingestion failed: ${failed.map((d) => d.filename).join(", ")}`);
    if (!pending.length) return { seconds: (Date.now() - started) / 1000, count: docs.length };
    sleep(1);
  }
  fail(`documents not indexed within ${timeoutSeconds}s`);
}

export function setup() {
  if (!ADMIN) fail("ADMIN_API_KEY is required");
  const tenant = http.post(
    `${BASE}/admin/tenants`,
    JSON.stringify({ name: `loadtest-${Date.now()}`, rate_limit_per_minute: 100000 }),
    { headers: adminHeaders() },
  );
  check(tenant, { "tenant created": (r) => r.status === 201 }) || fail(tenant.body);
  const tenantId = tenant.json("id");
  const key = http
    .post(`${BASE}/admin/tenants/${tenantId}/api-keys`, "{}", { headers: adminHeaders() })
    .json("key");
  const cid = http
    .post(`${BASE}/v1/collections`, JSON.stringify({ name: "loadtest" }), {
      headers: { "X-API-Key": key, "Content-Type": "application/json" },
    })
    .json("id");
  const ctx = { tenantId, key, cid };
  for (const doc of DOCS) {
    check(upload(ctx, doc.name, doc.body), { "corpus uploaded": (r) => r.status === 202 });
  }
  const indexed = waitUntilIndexed(ctx, 600);
  console.log(`corpus indexed in ${indexed.seconds.toFixed(1)}s`);
  return ctx;
}

function pick() {
  return QUESTIONS[Math.floor(Math.random() * QUESTIONS.length)].question;
}

export function search(ctx) {
  const res = http.post(
    `${BASE}/v1/collections/${ctx.cid}/search`,
    JSON.stringify({ query: pick() }),
    { headers: { "X-API-Key": ctx.key, "Content-Type": "application/json" }, tags: { name: "search" } },
  );
  const ok = check(res, { "search 200": (r) => r.status === 200 });
  if (ok) searchSources.add(res.json("sources").length);
}

export function chat(ctx) {
  const res = http.post(
    `${BASE}/v1/collections/${ctx.cid}/chat`,
    JSON.stringify({ question: pick() }),
    { headers: { "X-API-Key": ctx.key, "Content-Type": "application/json" }, tags: { name: "chat" } },
  );
  check(res, {
    "chat 200": (r) => r.status === 200,
    "chat completed": (r) => r.body.includes("event: done"),
  });
  chatTimeToFirstByte.add(res.timings.waiting);
}

export function ingest(ctx) {
  // Unique content, so that content-hash deduplication does not short-circuit ingestion.
  const doc = DOCS[Math.floor(Math.random() * DOCS.length)];
  const nonce = `${__VU}-${__ITER}-${Date.now()}`;
  const res = upload(ctx, `load-${nonce}.md`, `${doc.body}\n\nRevision ${nonce}.\n`);
  if (check(res, { "upload 202": (r) => r.status === 202 })) uploads.add(1);
}

export function teardown(ctx) {
  // How long do the workers need to absorb everything uploaded during the test?
  const drained = waitUntilIndexed(ctx, 900);
  drainTime.add(drained.seconds * 1000);
  console.log(`ingestion backlog drained ${drained.seconds.toFixed(1)}s after the load stopped`);
  http.del(`${BASE}/admin/tenants/${ctx.tenantId}`, null, { headers: adminHeaders() });
}

function fmt(ms) {
  return ms === undefined ? "-" : `${ms.toFixed(0)} ms`;
}

export function handleSummary(data) {
  const m = data.metrics;
  const row = (label, metric) => {
    const v = (m[metric] || {}).values || {};
    return `| ${label} | ${fmt(v.med)} | ${fmt(v["p(95)"])} | ${fmt(v["p(99)"])} |`;
  };
  const durationSeconds = data.state.testRunDurationMs / 1000;
  const failedRate = ((m.http_req_failed || {}).values || {}).rate || 0;
  const md = [
    "## Load test",
    "",
    `Search ${SEARCH_RPS} req/s + chat ${CHAT_RPS} req/s + ingestion ${INGEST_RPS} doc/s for ${DURATION}` +
      ` · ${((m.http_reqs || {}).values || {}).count ?? 0} HTTP requests in ` +
      `${durationSeconds.toFixed(0)} s · dropped iterations: ` +
      `${((m.dropped_iterations || {}).values || {}).count ?? 0}`,
    "",
    "| Request | p50 | p95 | p99 |",
    "|---|---:|---:|---:|",
    row("Search", "http_req_duration{scenario:search}"),
    row("Chat (full streamed answer)", "http_req_duration{scenario:chat}"),
    row("Chat time to first byte", "chat_time_to_first_byte"),
    row("Upload (202 Accepted)", "http_req_duration{scenario:ingest}"),
    "",
    `HTTP error rate: ${(failedRate * 100).toFixed(2)} % · documents uploaded under load: ` +
      `${((m.documents_uploaded || {}).values || {}).count ?? 0} · backlog drained ` +
      `${fmt(((m.ingest_backlog_drain || {}).values || {}).max)} after the load stopped`,
    "",
    `Thresholds: ${Object.entries(m)
      .filter(([, v]) => v.thresholds)
      .map(([k, v]) => `${k} ${Object.values(v.thresholds).every((t) => t.ok) ? "✅" : "❌"}`)
      .join(" · ")}`,
    "",
  ].join("\n");
  return {
    stdout: md,
    "../results/summary.md": md,
    "../results/summary.json": JSON.stringify(data, null, 2),
  };
}
