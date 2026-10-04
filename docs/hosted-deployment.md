# Hosted Deployment: Dependencies

**Status:** Agreed direction (2026-10-03). The hosting platform is not chosen yet. **Hosting comes after a working local version** (user decision, 2026-10-03).
**Companion:** [harness-design.md](harness-design.md).

## The constraint that drives the hosted design

Third-party documentation reports that ClinicalTrials.gov allows about **50 requests per minute per IP** (not confirmed on an official page; verify during the API spike). Once hosted, every user shares the server's IP and therefore that budget. One broad question can need 50+ pages. LLM providers are the second dependency outside our control.

## Infrastructure

| Need | Recommendation | Why | Required? |
|---|---|---|---|
| API container | Cloud Run, Fly.io or Render (Docker) | Scales to zero, HTTPS included | Yes |
| Cache + shared rate limiter | Redis (Upstash or managed) | Cache API pages; one token bucket keeps all instances under the API limit | Yes |
| Run history | Postgres (Neon, Supabase, Cloud SQL) | Run ID, plan, outcome, cost, latency per user | Recommended |
| Run bundles | S3 or Cloudflare R2 | Large write-once raw responses and evidence; Postgres keeps a pointer | Not now (deferred 2026-10-03) |
| Secrets | Platform secret manager | LLM API keys | Yes |
| Tracing | OpenTelemetry (decided 2026-10-03) | One trace per run | Yes |
| Frontend | Static images rendered from our spec via Vega-Lite (`vl-convert`); optional interactive page with `vega-embed`, served from the API | UI bonus; clicking a datum shows its citations | Optional |
| CI | — | Not needed (decided 2026-10-03); lint, tests and evals run locally | No |
| Job queue | arq or RQ on the same Redis | Only if large questions regularly exceed the request deadline | Later |

Not needed: vector database, run checkpointer, Kubernetes, separate API gateway.

## Python packages

- **Core:** `fastapi`, `uvicorn`, `pydantic` v2, `pydantic-settings`, `httpx` (async), `tenacity`.
- **Model layer:** `pydantic-ai` `FallbackModel` (OpenAI primary, Anthropic fallback, chosen by configuration), tool-based output mode.
- **Chart and analysis:** our own visualization schema (pydantic models exported as JSON Schema); `vl-convert-python` to render Vega-Lite to PNG/SVG; plain Python for counting (`pandas` optional); `networkx` only for network metrics.
- **Storage and cache:** `redis` (asyncio), `sqlalchemy` 2.x, `asyncpg`, `alembic`, `boto3`/`aioboto3`.
- **Observability:** `opentelemetry-sdk` with FastAPI and httpx instrumentation (pydantic-ai emits OpenTelemetry spans too); `structlog`.
- **Optional:** `langgraph`.
- **Dev and test:** `uv`, `ruff`, `pyright` or `mypy`, `pytest`, `pytest-asyncio`, `respx` (replay saved API responses), `hypothesis` (property tests: input order and duplicate sites don't change counts).

## Design changes that come with hosting

1. **Cache key:** compiled request + the API data timestamp from `/api/v2/version`, so the cache expires when ClinicalTrials.gov publishes new data.
2. **Per-run page cap** (e.g. 20) in addition to the global rate limit, so one user can't starve others.
3. **Circuit breakers** for ClinicalTrials.gov and each LLM provider: return `upstream_error` immediately during outages.
4. **Abuse protection:** API-key header and a per-user rate limit (every request costs LLM money).
5. **Request handling:** synchronous with a hard ~30s deadline. Add `POST /runs` → `GET /runs/{id}` only if traces show deadline hits.

**Smallest credible hosted setup:** Cloud Run + Upstash Redis + Neon Postgres + R2 + Langfuse cloud + a static frontend (all have free tiers).
