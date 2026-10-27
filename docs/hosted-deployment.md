# Hosted Deployment: Dependencies

**Status:** Agreed direction (2026-10-03). **Hosting comes after a working local version** (user decision, 2026-10-03). **Built and deployed on AWS 2026-10-04** (user decision 2026-10-04: AWS instead of Cloud Run); see [What was built](#what-was-built-2026-10-04) and [AWS deployment](#aws-deployment-2026-10-04).
**Companion:** [harness-design.md](harness-design.md).

## The constraint that drives the hosted design

Third-party documentation reports that ClinicalTrials.gov allows about **50 requests per minute per IP** (not confirmed on an official page; verify during the API spike). Once hosted, every user shares the server's IP and therefore that budget. One broad question can need 50+ pages. LLM providers are the second dependency outside our control.

## Infrastructure

The plan as first written (2026-10-03), before AWS was chosen. What was actually built is in "AWS deployment (2026-10-04)" below: ECS Express Mode, ElastiCache Serverless (Valkey), RDS for PostgreSQL and Secrets Manager. None of the other products named in this table (Cloud Run, Fly.io, Render, Upstash, Neon, Supabase, Cloud SQL, S3, R2, arq, RQ) was used.

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

What the project uses (`pyproject.toml`):
- **Core:** `fastapi`, `uvicorn`, `pydantic` v2, `pydantic-settings`, `python-dotenv`, `httpx` (async).
- **Model layer:** `pydantic-ai-slim[openai,anthropic,google]` with `FallbackModel` (OpenAI primary, Anthropic fallback; Gemini wired in but never run), tool-based output mode.
- **Charts:** our own visualization schema (pydantic models exported as JSON Schema); `vl-convert-python` renders Vega-Lite to PNG/SVG; counting is plain Python.
- **Storage and cache (hosted):** `redis` (asyncio), `sqlalchemy` 2.x with `asyncpg`.
- **Observability:** `opentelemetry-sdk` with the FastAPI and httpx instrumentation and the OTLP HTTP exporter; pydantic-ai emits model spans. Logs use the standard `logging` module.
- **Dev and test:** `uv`, `ruff`, `pyright`, `pytest`, `pytest-asyncio`, `respx` (replays saved API responses), `hypothesis` (property tests), `fakeredis` and `aiosqlite` (the hosted mode without servers).

Planned at first but not used: `tenacity` (the client's own retry), `alembic` (one table so far), `boto3` (no object storage yet), `structlog`, `pandas`, `langgraph`, `networkx`, `mypy`.

## Design changes that come with hosting

1. **Cache key:** compiled request + the API data timestamp from `/api/v2/version`, so the cache expires when ClinicalTrials.gov publishes new data.
2. **Per-run page cap** (e.g. 20) in addition to the global rate limit, so one user can't starve others.
3. **Circuit breakers** for ClinicalTrials.gov and each LLM provider: return `upstream_error` immediately during outages.
4. **Abuse protection:** API-key header and a per-user rate limit (every request costs LLM money).
5. **Request handling:** synchronous with a hard ~30s deadline. Add `POST /runs` → `GET /runs/{id}` only if traces show deadline hits.

**Chosen setup (user decision 2026-10-04): AWS.** The earlier plan was Cloud Run + Upstash Redis + Neon Postgres. On AWS, App Runner (the closest match to Cloud Run) closed to new customers on 2026-04-30, so the container runs on **ECS Express Mode**; Redis is **ElastiCache Serverless (Valkey)** and Postgres is **RDS**, both private to the service. Tracing stays OpenTelemetry (decided 2026-10-03; no backend chosen). Object storage only once run bundles are built (deferred). The API serves the web page.

## What was built (2026-10-04)

One codebase; `DEPLOYMENT=hosted` switches on the hosted dependencies and refuses to start without them. The container image sets it already (`Dockerfile`), so anything run from that image needs `REDIS_URL` and `DATABASE_URL` — locally, run the code directly and all of this stays off.

| Decision above | Built as | Where |
|---|---|---|
| API container | Two-stage uv image (ARM64), non-root, fonts for chart text, one worker. ECS Express Mode service: Fargate tasks with 0.5 vCPU and 1 GB, 1–2 tasks, HTTPS load balancer and auto scaling it manages, rolling updates with automatic rollback | `Dockerfile`, `deploy/aws/` |
| Redis: cache + shared rate limiter | ElastiCache Serverless (Valkey, TLS; deployed on 9, though the create call pins no version). GCRA token bucket in one Lua script (all instances share the 40/min budget), compressed page cache keyed by data timestamp, plus Idempotency-Keys and per-user counts. Degrades per instance if Redis fails | `shared_state.py` |
| Postgres: run history | RDS for PostgreSQL (db.t4g.micro, not public; deployed on 18, though the create call pins no version). `runs` table (run ID, time, user, outcome, model calls, planner model, latency, question, full record as JSONB) via SQLAlchemy async + asyncpg; created on startup | `runs.py` |
| Run bundles / R2 | Still deferred | — |
| Secrets | Secrets Manager; ECS reads them into environment variables when a task starts (the task execution role may read only `clinical-trials-viz/*`) | `deploy/aws/lib.sh` |
| Tracing | OpenTelemetry spans, exported when `OTEL_EXPORTER` is `otlp` (endpoint from the standard `OTEL_EXPORTER_OTLP_ENDPOINT`) or `console`. It defaults to `none`, and the AWS task does not set it, so **the deployed service emits no spans**: setting the endpoint alone changes nothing. Connecting X-Ray needs a collector next to the app, because its OTLP endpoint wants SigV4 signing. *Added 2026-10-27:* `TRACES=xray` adds that collector and sets both settings (see "Traces in X-Ray"); built, not yet run on AWS | `telemetry.py`, `deploy/aws/enable-traces.sh` |
| 1. Cache key with data timestamp | Kept (`page_key`) | `ctgov/client.py` |
| 2. Per-run page cap | Kept (`MAX_PAGES=20`) | `config.py` |
| 3. Circuit breakers | One for ClinicalTrials.gov, one per planner model; an open model is skipped so the fallback answers at once | `breaker.py` |
| 4. API key + per-user limit | `X-API-Key` on `POST /v1/query` (401), 30 questions/hour/user (429 + `Retry-After`). *Changed 2026-10-04 (author):* the live demo runs with `OPEN_ACCESS=true` for the review, so reviewers need no key and there is no hourly limit; `API_KEYS` is then optional | `access.py`, `api.py`, `config.py` |
| 5. ~30 s deadline | `RUN_DEADLINE_SECONDS=30` when hosted, from loading a Follow-up's earlier run to verification; unfinished parts end as `run_timeout`. Saving the run has its own 5 s limit and falls back to a warning | `pipeline.py` |

Deviations: no Alembic yet (one table, created with `IF NOT EXISTS`). Tested with fakeredis and SQLite (`tests/test_hosted.py`), end to end with `docker compose` (service + Redis 7 + Postgres 17), and live on AWS with `deploy/smoke-test.sh`.

## AWS deployment (2026-10-04)

Done step by step with the user, who ran each command. Account: "Idea Incubator", a project in AWS's new account experience on the **free plan**. It sits in an AWS-managed organization whose service control policies cannot be changed, which constrains three things:

- **Region:** regional services work only in the project's selected region, **us-east-2**. us-east-1 allows only global services (the first ECR call there failed with "explicit deny in a service control policy").
- **Sign-in:** IAM Identity Center is unavailable. The CLI signs in with `aws login --profile incubator` (an assumed role, `AccountFullAccessRole`, about 12-hour sessions).
- **Cost:** the free plan never charges a card. The account closes when its credits ($100–200) run out or after 6 months.

### Setup and deployment

`deploy/aws/deploy.sh` is the one supported path. It sets up whatever is missing, builds and pushes the image, creates the service or rolls the new image out, waits until the service answers, and runs the smoke test. Every step checks what already exists and skips it, so it is safe to run again. A first deployment takes about 15 minutes, mostly waiting for the database.

```bash
DRY_RUN=1 deploy/aws/deploy.sh                     # read-only: list what it would create or change
deploy/aws/deploy.sh                               # set up what is missing, then ship the current code
SET_ENV="OPEN_ACCESS=true" deploy/aws/deploy.sh    # also add or change settings on the service
TAG=v7 deploy/aws/deploy.sh                        # image tag (default: the current git commit)
UPDATE_MODEL_KEYS=1 deploy/aws/deploy.sh           # replace the stored model keys with those in .env
```

It needs the AWS CLI signed in (`aws login --profile incubator`), Docker running, and `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` in `.env`. Everything runs in us-east-2.

| Step | What it creates |
|---|---|
| 1. Image registry | The ECR repository, scanning images on push |
| 2. Roles | `ecsTaskExecutionRole` (pull the image, write logs, read only this app's secrets), `ecsInfrastructureRoleForExpressServices`, and the service-linked roles for ECS, load balancing and auto scaling — these three first, because the first deployment failed without them (see below) |
| 3. Database and cache | A security group with no inbound rules in the default VPC; RDS for PostgreSQL (db.t4g.micro, 20 GB gp3, not public, one day of backups) and ElastiCache Serverless Valkey (1 GB, 1000 ECPU). Neither call pins an engine version, so a fresh setup gets whatever AWS defaults to that day. It waits out a database or cache still being deleted from a previous teardown |
| 4. Secrets | The two model keys from `.env`, a generated `API_KEYS` (`admin:<random>`, left alone if it already exists) and the two connection strings. **Values reach AWS through owner-only temporary files (`file://`), never as command-line arguments**, so they stay out of the shell history and the process list |
| 5. Image and service | Builds for ARM64 (Graviton, and native on Apple Silicon), pushes, then creates the Express Mode service or rolls the image out to the existing one, opens the database and cache to the service's own security group, and fails if the rollout stalls instead of reporting success |
| 6. Smoke test | `deploy/smoke-test.sh` against the live address, using the stored API key |

**Settings on the service.** The task always gets `DEPLOYMENT=hosted`, `RUN_DEADLINE_SECONDS=30`, `USER_QUERIES_PER_HOUR=30` and `LOG_LEVEL=INFO` (`deploy/aws/lib.sh`), plus the secrets above as environment variables of the same name. Anything else is set with `SET_ENV`, which merges into what the service already has and applies whether the service is being created or updated. `SET_ENV="OPEN_ACCESS=true"` is what puts the review demo in its current state; without it the service requires an API key.

For an image-only rollout, `TAG=vN deploy/aws/update-service.sh` still works: build, push, and a rolling update that changes nothing but the image, rolled back automatically if the new tasks fail their health checks. The `v1`–`v6` series came from those manual rollouts (v2 startup fix, v3 threaded web page, v4 tolerant key check, v5 light and dark themes, v6 the first review's fixes and open access); `deploy.sh` now tags with the current git commit instead, so the running tag is a commit SHA.

### Traces in X-Ray (opt-in; built 2026-10-27, not yet run on AWS)

Off by default: a deploy without `TRACES` changes nothing. With `TRACES=xray`, the app sends its OpenTelemetry spans (one trace per run: stages, ClinicalTrials.gov requests, model calls) over OTLP to an **ADOT collector in the same task** (`public.ecr.aws/aws-observability/aws-otel-collector:v0.50.0`, config in `deploy/aws/otel-collector.yaml`), which batches them and sends them to **AWS X-Ray** with the `awsxray` exporter, signed with the task role's credentials. No app code changed: the app already exports OTLP when `OTEL_EXPORTER=otlp` and `OTEL_EXPORTER_OTLP_ENDPOINT` are set, and still has no vendor SDK.

**Why a sidecar works on Express Mode.** Express Mode has no sidecar setting, but AWS documents adding one: either create the service from your own task definition (`taskDefinitionArn`, which cannot be combined with `primaryContainer`, so it would replace how `create-service.sh` works), or edit the task definition Express Mode manages, which it then keeps ("Express Mode will not overwrite changes unless requested as part of an Express Mode update"; [Updating Resources Outside of Express Mode](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-advanced-customization.html)). The scripts take the second path, so the default deploy stays exactly as it is.

```bash
DRY_RUN=1 deploy/aws/enable-traces.sh      # read-only: say what would change on the running service
deploy/aws/enable-traces.sh                # turn traces on for the running service (one rolling update)
TRACES=xray deploy/aws/deploy.sh           # or: ship the current code with traces on
```

What `TRACES=xray` does (`deploy/aws/lib.sh`, `enable-traces.sh`):

1. Creates the task role `clinical-trials-viz-task` (the app had none) with one inline policy allowing only `xray:PutTraceSegments` and `xray:PutTelemetryRecords` — the calls the exporter makes; narrower than the managed `AWSXrayWriteOnlyAccess`.
2. Passes that role and `OTEL_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` through Express Mode itself (`--task-role-arn`, the container settings), so later Express Mode updates keep them.
3. Copies the running task definition, adds the collector container (not essential: if it stops, the app keeps serving; 64 MiB reserved inside the existing 1 GB; logs in the app's log group under the `otel-collector` prefix), registers the new revision and rolls it out with `aws ecs update-service`, then waits for the health checks. When the running revision already has all of this, it changes nothing.

`update-service.sh` and `deploy.sh` run step 3 again after each rollout when `TRACES=xray` is set; it is a no-op while Express Mode keeps the collector, and re-adds it (one more rolling update) if a rollout dropped it. Ship later code with `TRACES=xray` too. To stop sending spans without removing anything, deploy with `SET_ENV="OTEL_EXPORTER=none"`; `teardown.sh` deletes the task role along with the rest.

**Cost.** The collector runs inside the existing task, so Fargate costs nothing more. X-Ray charges per trace recorded and per trace retrieved or scanned, after a monthly free tier (long-standing figures: the first 100,000 traces recorded free, then about $5 per million). AWS has been folding tracing into CloudWatch pricing, so check [the pricing page](https://aws.amazon.com/xray/pricing/) before turning it on; these numbers were not re-checked for this account. One question is one trace, so the review demo's volume is tiny either way. Spans include the question text and the model's replies (pydantic-ai records them); nothing secret is in them.

**Checking it.** After a question or two, open the CloudWatch console in **us-east-2** → *X-Ray traces* → *Traces* (or *Trace Map*), and look for the service `clinical-trials-viz`; each trace is one `POST /v1/query` with the stages, ClinicalTrials.gov requests and model calls underneath. If nothing arrives, the collector's own log is in the service's log group under the `otel-collector/` stream prefix (an `AccessDenied` there means the task role is missing its policy).

### First deployment: what went wrong and the fixes

1. **Express Mode's first attempt was rolled back** ("the infrastructure role doesn't have enough permission"). CloudTrail showed `CreateLoadBalancer` refused in the same second the load-balancing service-linked role was created. Auto scaling failed the same way. The account had never used either service. A retry succeeded once the roles existed; setup step 2 now creates them first.
2. **Every task took a minute to start, failed its health checks and was replaced,** so the first deployment never shifted traffic and the address answered 503. At startup the app created its table in Postgres before serving, and the database's security group did not yet admit the service (asyncpg waits 60 s by default). Fixed in v2: the table is created in the background, Postgres connections give up after 5 s and Redis after 2 s, and `create-service.sh` opens the security group itself.
3. Web page requests were refused (401) when the key was pasted as the whole stored `name:key` value, or from a notes app that capitalized it or added quotes or invisible characters. The service now accepts the key with or without its name and ignores case, quotes and invisible characters (v4); a key that differs in any real character is still refused.

Verified live: `/health`, a question (melanoma trials by country), a follow-up refined from the Postgres run history ("only phase 3"), and a chart image over HTTPS.

### Costs and teardown

Running costs are roughly $50–60 a month, paid from the free plan's credits: the load balancer (~$16 + usage), one Fargate task with 0.5 vCPU and 1 GB on ARM (~$15), RDS db.t4g.micro with 20 GB (~$14), ElastiCache Serverless at its minimum (~$6), and Secrets Manager (6 secrets, ~$2.40). Every question also costs 2–5 model calls (one split call, two if the primary model fails, then up to three planning calls), which the per-user limit caps.

To delete everything, run `deploy/aws/teardown.sh` (`DRY_RUN=1` first to list what would go). It asks you to type the service name, then works through seven steps, waiting for each and skipping what is already gone:

```bash
DRY_RUN=1 deploy/aws/teardown.sh     # read-only: list what it would delete
deploy/aws/teardown.sh               # asks you to type the service name, then deletes
```

1. The service, its load balancer and its tasks — after revoking the data security group's inbound rule, so nothing holds the group open.
2. The cache.
3. The database, with no final snapshot and without keeping its automated backups. **The run history goes with it.**
4. The security group, now that nothing uses it.
5. The ECR repository and its images, the six secrets, the log groups, and every task definition Express Mode registered (one revision per rollout).
6. The two IAM roles, and the task role if traces were turned on.
7. The empty `default` ECS cluster.

Service-linked roles are kept: the account shares them, they cost nothing, and AWS manages them. Nothing here can be undone — `deploy/aws/deploy.sh` can build it all again, but under a new address.

Deleting by hand instead is the way to leave resources billing: the log groups, task definitions, roles and cluster are all easy to forget, and the security group cannot go until the database and cache have finished deleting.
