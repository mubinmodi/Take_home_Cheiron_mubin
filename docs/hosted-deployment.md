# Hosted Deployment: Dependencies

**Status:** Agreed direction (2026-10-03). **Hosting comes after a working local version** (user decision, 2026-10-03). **Built and deployed on AWS 2026-10-04** (user decision 2026-10-04: AWS instead of Cloud Run); see [What was built](#what-was-built-2026-10-04) and [AWS deployment](#aws-deployment-2026-10-04).
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
- **Model layer:** `pydantic-ai-slim[openai,anthropic,google]` `FallbackModel` (OpenAI primary, Anthropic fallback by default, Gemini available; chosen by configuration), tool-based output mode.
- **Chart and analysis:** our own visualization schema (pydantic models exported as JSON Schema); `vl-convert-python` to render Vega-Lite to PNG/SVG; plain Python for counting (`pandas` optional).
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

**Chosen setup (user decision 2026-10-04): AWS.** The earlier plan was Cloud Run + Upstash Redis + Neon Postgres. On AWS, App Runner (the closest match to Cloud Run) closed to new customers on 2026-04-30, so the container runs on **ECS Express Mode**; Redis is **ElastiCache Serverless (Valkey)** and Postgres is **RDS**, both private to the service. Tracing stays OpenTelemetry (decided 2026-10-03; no backend chosen). Object storage only once run bundles are built (deferred). The API serves the web page.

## What was built (2026-10-04)

One codebase; `DEPLOYMENT=hosted` switches on the hosted dependencies and refuses to start without them.

| Decision above | Built as | Where |
|---|---|---|
| API container | Two-stage uv image (ARM64), non-root, fonts for chart text, one worker. ECS Express Mode service: Fargate tasks with 0.5 vCPU and 1 GB, 1–2 tasks, HTTPS load balancer and auto scaling it manages, rolling updates with automatic rollback | `Dockerfile`, `deploy/aws/` |
| Redis: cache + shared rate limiter | ElastiCache Serverless (Valkey 9, TLS). GCRA token bucket in one Lua script (all instances share the 40/min budget), compressed page cache keyed by data timestamp, plus Idempotency-Keys and per-user counts. Degrades per instance if Redis fails | `shared_state.py` |
| Postgres: run history | RDS for PostgreSQL 18 (db.t4g.micro, not public). `runs` table (run ID, time, user, outcome, model calls, planner model, latency, question, full record as JSONB) via SQLAlchemy async + asyncpg; created on startup | `runs.py` |
| Run bundles / R2 | Still deferred | — |
| Secrets | Secrets Manager; ECS reads them into environment variables when a task starts (the task execution role may read only `clinical-trials-viz/*`) | `deploy/aws/lib.sh` |
| Tracing | OpenTelemetry spans, exported over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Not yet connected on AWS: X-Ray's OTLP endpoint needs SigV4 signing, so it needs a collector next to the app | `telemetry.py` |
| 1. Cache key with data timestamp | Kept (`page_key`) | `ctgov/client.py` |
| 2. Per-run page cap | Kept (`MAX_PAGES=20`) | `config.py` |
| 3. Circuit breakers | One for ClinicalTrials.gov, one per planner model; an open model is skipped so the fallback answers at once | `breaker.py` |
| 4. API key + per-user limit | `X-API-Key` on `POST /v1/query` (401), 30 questions/hour/user (429 + `Retry-After`) | `access.py`, `api.py` |
| 5. ~30 s deadline | `RUN_DEADLINE_SECONDS=30` when hosted, from loading a Follow-up's earlier run to verification; unfinished parts end as `run_timeout`. Saving the run has its own 5 s limit and falls back to a warning | `pipeline.py` |

Deviations: no Alembic yet (one table, created with `IF NOT EXISTS`). Tested with fakeredis and SQLite (`tests/test_hosted.py`), end to end with `docker compose` (service + Redis 7 + Postgres 17), and live on AWS with `deploy/smoke-test.sh`.

## AWS deployment (2026-10-04)

Done step by step with the user, who ran each command. Account: "Idea Incubator", a project in AWS's new account experience on the **free plan**. It sits in an AWS-managed organization whose service control policies cannot be changed, which constrains three things:

- **Region:** regional services work only in the project's selected region, **us-east-2**. us-east-1 allows only global services (the first ECR call there failed with "explicit deny in a service control policy").
- **Sign-in:** IAM Identity Center is unavailable. The CLI signs in with `aws login --profile incubator` (an assumed role, `AccountFullAccessRole`, about 12-hour sessions).
- **Cost:** the free plan never charges a card. The account closes when its credits ($100–200) run out or after 6 months.

### One-time setup

All commands run with `--profile incubator` and region us-east-2 (`aws configure set region us-east-2 --profile incubator`).

```bash
# 1. Image registry, and the image (ARM64: Graviton, and native on Apple Silicon)
aws ecr create-repository --repository-name clinical-trials-viz --image-scanning-configuration scanOnPush=true
aws ecr get-login-password | docker login --username AWS --password-stdin ACCOUNT.dkr.ecr.us-east-2.amazonaws.com
docker build --platform linux/arm64 -t ACCOUNT.dkr.ecr.us-east-2.amazonaws.com/clinical-trials-viz:v1 .
docker push ACCOUNT.dkr.ecr.us-east-2.amazonaws.com/clinical-trials-viz:v1

# 2. Roles: one for ECS to start tasks (pull the image, write logs, read this app's secrets), one for Express Mode
aws iam create-role --role-name ecsTaskExecutionRole --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name ecsTaskExecutionRole --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name ecsTaskExecutionRole --policy-name read-clinical-trials-viz-secrets --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"arn:aws:secretsmanager:us-east-2:*:secret:clinical-trials-viz/*"}]}'
aws iam create-role --role-name ecsInfrastructureRoleForExpressServices --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"AllowAccessInfrastructureForECSExpressServices","Effect":"Allow","Principal":{"Service":"ecs.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name ecsInfrastructureRoleForExpressServices --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com
# Create these two as well before the first service (see "First deployment" below)
aws iam create-service-linked-role --aws-service-name elasticloadbalancing.amazonaws.com
aws iam create-service-linked-role --aws-service-name ecs.application-autoscaling.amazonaws.com

# 3. Database and cache in the default VPC, behind a security group that starts with no inbound rules
aws ec2 create-security-group --group-name clinical-trials-viz-data --description "Postgres and Valkey for clinical-trials-viz" --vpc-id DEFAULT_VPC_ID
aws secretsmanager create-secret --name clinical-trials-viz/db-password --secret-string "$(openssl rand -hex 24)"
aws rds create-db-instance --db-instance-identifier clinical-trials-viz --engine postgres --db-instance-class db.t4g.micro --allocated-storage 20 --storage-type gp3 --db-name trials --master-username trials --master-user-password "$(aws secretsmanager get-secret-value --secret-id clinical-trials-viz/db-password --query SecretString --output text)" --vpc-security-group-ids DATA_SG_ID --no-publicly-accessible --backup-retention-period 1
aws elasticache create-serverless-cache --serverless-cache-name clinical-trials-viz --engine valkey --security-group-ids DATA_SG_ID --subnet-ids DEFAULT_SUBNET_IDS --cache-usage-limits 'DataStorage={Maximum=1,Unit=GB},ECPUPerSecond={Maximum=1000}'

# 4. Secrets (values never printed): model keys from .env, a generated API key, the two connection strings
aws secretsmanager create-secret --name clinical-trials-viz/OPENAI_API_KEY --secret-string "$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2-)"
aws secretsmanager create-secret --name clinical-trials-viz/ANTHROPIC_API_KEY --secret-string "$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)"
aws secretsmanager create-secret --name clinical-trials-viz/API_KEYS --secret-string "mubin:$(openssl rand -hex 20)"
aws secretsmanager create-secret --name clinical-trials-viz/DATABASE_URL --secret-string "postgresql+asyncpg://trials:DB_PASSWORD@RDS_ENDPOINT:5432/trials?ssl=require"
aws secretsmanager create-secret --name clinical-trials-viz/REDIS_URL --secret-string "rediss://CACHE_ENDPOINT:6379/0"

# 5. The service; it then opens the database and cache to its own security group and waits until it answers
deploy/aws/create-service.sh
deploy/smoke-test.sh https://SERVICE_ENDPOINT
```

New code goes out with `TAG=vN deploy/aws/update-service.sh`: build, push, and a rolling update that changes only the image (rolled back automatically if the new tasks fail their health checks). Live as of 2026-10-04: v4 (v2 startup fix, v3 threaded web page, v4 tolerant key check).

### First deployment: what went wrong and the fixes

1. **Express Mode's first attempt was rolled back** ("the infrastructure role doesn't have enough permission"). CloudTrail showed `CreateLoadBalancer` refused in the same second the load-balancing service-linked role was created. Auto scaling failed the same way. The account had never used either service. A retry succeeded once the roles existed; setup step 2 now creates them first.
2. **Every task took a minute to start, failed its health checks and was replaced,** so the first deployment never shifted traffic and the address answered 503. At startup the app created its table in Postgres before serving, and the database's security group did not yet admit the service (asyncpg waits 60 s by default). Fixed in v2: the table is created in the background, Postgres connections give up after 5 s and Redis after 2 s, and `create-service.sh` opens the security group itself.
3. Web page requests were refused (401) when the key was pasted as the stored `mubin:...` value, or from a notes app that capitalized it or added quotes or invisible characters. The service now accepts the key with or without its name and ignores case, quotes and invisible characters (v4); a key that differs in any real character is still refused.

Verified live: `/health`, a question (melanoma trials by country), a follow-up refined from the Postgres run history ("only phase 3"), and a chart image over HTTPS.

### Costs and teardown

Running costs are roughly $50–60 a month, paid from the free plan's credits: the load balancer (~$16 + usage), one Fargate task with 0.5 vCPU and 1 GB on ARM (~$15), RDS db.t4g.micro with 20 GB (~$14), ElastiCache Serverless at its minimum (~$6), and Secrets Manager (6 secrets, ~$2.40). Every question also costs 1–3 model calls, which the per-user limit caps.

To delete everything, in this order:

```bash
aws ecs delete-express-gateway-service --service-arn arn:aws:ecs:us-east-2:ACCOUNT:service/default/clinical-trials-viz
aws elasticache delete-serverless-cache --serverless-cache-name clinical-trials-viz
aws rds delete-db-instance --db-instance-identifier clinical-trials-viz --skip-final-snapshot
aws ecr delete-repository --repository-name clinical-trials-viz --force
for s in OPENAI_API_KEY ANTHROPIC_API_KEY API_KEYS DATABASE_URL REDIS_URL db-password; do aws secretsmanager delete-secret --secret-id clinical-trials-viz/$s --force-delete-without-recovery; done
aws ec2 delete-security-group --group-name clinical-trials-viz-data   # after the database and cache are gone
```
