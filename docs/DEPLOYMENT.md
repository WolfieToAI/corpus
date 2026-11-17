# Deployment and the chat widget

## Try it locally in two commands

```bash
docker compose up -d --build
docker compose run --rm api corpus-demo
```

`corpus-demo` runs the migrations, indexes the sample documents (robot manuals, HR policies, an
API reference, an SLA) in a `demo` tenant and prints a link such as
`http://localhost:8000/demo?collection=...&key=crp_...`. No LLM key? Add `LLM_PROVIDER=extractive`
to `.env`: answers then quote the best passage, citations included.

## Embed the widget on a website

1. Create a **widget key** for the collection, restricted to the site's origin(s):

   ```bash
   curl -X POST https://YOUR-API/admin/tenants/$TENANT/api-keys \
     -H "X-Admin-Key: $ADMIN" -H "Content-Type: application/json" \
     -d '{"name": "website", "scope": "widget", "collection_id": "'$COLLECTION'",
          "allowed_origins": ["https://www.example.com"]}'
   ```

2. Add one tag to the site:

   ```html
   <script src="https://YOUR-API/widget.js"
           data-collection="COLLECTION_ID" data-api-key="crp_..."
           data-heading="Help center" data-accent="#0f766e" defer></script>
   ```

Options (`data-*` on the script tag, or attributes on `<corpus-chat>`): `heading`, `placeholder`,
`accent` (any CSS colour), `theme` (`light` / `dark`, default follows the OS), `suggestions`
(questions separated by `|`). `<corpus-chat mode="inline">` renders the panel inside the page
instead of a floating launcher.

### Why a widget key is safe in page source

A key embedded in HTML is public: anyone can read it. A **widget key** is therefore bound to:

| Restriction | Enforced by |
|-------------|-------------|
| Only `search` and `chat` | route allow-list in the auth dependency (403 otherwise) |
| One collection | checked before the database is queried (404 for any other collection) |
| Listed origins only | the request's `Origin` header must match exactly (403 otherwise) |
| The tenant's rate limit | same per-tenant limiter as every key |
| A valid configuration | a database `CHECK`: a widget key without collection or origin cannot exist |

Server-to-server integrations use **full** keys, which never leave the backend.

The widget itself has no dependencies and no build step, isolates its styles in a Shadow DOM,
streams answers over SSE, escapes model output before rendering (citations are the only markup
it adds), works with keyboard and screen readers (`aria-live`), follows the OS colour scheme and
honours `prefers-reduced-motion`.

## Free live demo: Render + Neon

A free Render web service runs the API, the ingestion worker and the demo on one instance; a free
[Neon](https://neon.tech) project hosts PostgreSQL. Neon's free tier does not expire, supports
pgvector and lets the database user create the `corpus_tenant` role that Row-Level Security relies
on. (Render's own free database expires after 30 days.)

1. **Neon** - create a project (PostgreSQL 17), then copy the **direct** connection string
   (*Connection details*, pooling **off**): `postgresql://...neon.tech/neondb?sslmode=require`.
   The pooled endpoint goes through PgBouncer in transaction mode, which breaks `LISTEN/NOTIFY`.
2. **Render** - open `https://render.com/deploy?repo=https://github.com/WolfieToAI/corpus`,
   approve the Blueprint ([`render.yaml`](../render.yaml)) and paste the Neon string into
   `DATABASE_URL`. Leave `LLM_API_KEY` and `LLM_MODEL` empty for key-free extractive answers, or fill them
   (and `LLM_BASE_URL` for a provider other than the default) and set
   `LLM_PROVIDER=openai-compatible`.
3. Open `https://<service>.onrender.com/demo`. On startup the service migrates the database, seeds
   the sample documents and indexes them; the page configures itself from `/demo/config`.

What makes this work on one small instance:

| Setting | Effect |
|---------|--------|
| `AUTO_MIGRATE=true` | Alembic runs at startup (free services have no release command) |
| `EMBEDDED_WORKER=true` | the queue worker runs inside the API process |
| `DEMO_MODE=true` | sample documents are seeded and a widget key for the public URL is issued at each start (older demo keys are revoked); `/demo/config` hands it to the demo page |
| `RENDER_EXTERNAL_URL` | provided by Render, used as the widget key's allowed origin |
| `EMBEDDING_MODEL=BAAI/bge-small-en-v1.5` | compact model that fits in 512 MB |

Free-tier trade-offs: the service sleeps after 15 minutes without traffic, so the first visit
afterwards takes about a minute (container start and embedding model download); Neon suspends its
compute when idle and resumes in about a second. The app turns Neon's `sslmode` /
`channel_binding` parameters into what asyncpg expects.

## Production on Render

[`deploy/render.production.yaml`](../deploy/render.production.yaml) runs the same image as a
dedicated API service and a separate worker service on paid instances, with a managed PostgreSQL
and migrations as a pre-deploy step; create the Blueprint from that file path in the Render
dashboard.

Any container platform works the same way: run the image as `alembic upgrade head` (once per
release), the default command (API, listens on `$PORT`) and `corpus-worker`.
