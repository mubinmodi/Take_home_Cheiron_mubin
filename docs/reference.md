# Reference

Details behind the [README](../README.md): the web page, the hosted mode, safe retries and API keys, one real Datum of each chart type, every failure point, and the full list of what the tests cover. The README sections they belong to are named in each heading.

## Web page (README §1)

**Web page.** Open http://127.0.0.1:8000 after `serve`. Served by the API itself; nothing extra to install.
- Ask a question, optionally pinning filters (phase and status lists come from `/v1/schema`).
- See the chart interactively (Vega-Lite in the browser, from the same translation as the PNG).
- **Click any bar, point, node, link or table row to see its cited trials**, with links and the source values that placed each one there.
- Clarifications appear as clickable options (multi-select where allowed).
- **Follow-ups:** below the latest answer, "Ask a follow-up about the latest answer" takes a short change ("only phase 3", "show it by start year instead"; one-click examples are offered) and sends it with that answer's `previous_run_id`. Each follow-up's answer is added below as its own section, labelled as a refinement of the previous answer or a new question, so the whole conversation stays on the page with every chart and citation. "Ask a new question" starts a new conversation with what is in the form; "New question", shown once an answer is on the page, clears the page, the question and the filters.
- Failures show their error code, with a Retry button when retrying may help; if the chart library cannot load, the chart's data is shown as a clickable table instead.
- A Light / Dark / Auto switch (Auto follows the system setting); charts redraw in the chosen theme.
- Buttons are disabled while a request runs, so a double-click never sends twice; each request also carries a fresh `Idempotency-Key`.
- Registry text is always inserted as text, never as markup.

## Hosted mode (README §1)

**Hosted version (AWS).** Live demo: https://cl-f44fdf3287b047ef971affaa8d767246.ecs.us-east-2.on.aws (open for the review: no API key needed). The same container runs on AWS in us-east-2, as decided in [`docs/hosted-deployment.md`](hosted-deployment.md). `DEPLOYMENT=hosted` switches on:
- **Shared state in Redis** (`REDIS_URL`; ElastiCache Serverless for Valkey): one ClinicalTrials.gov request budget for all instances (the registry's limit is per IP), the page cache, Idempotency-Keys and per-user counts. If Redis fails, each falls back to working per instance.
- **Run history in Postgres** (`DATABASE_URL`; RDS for PostgreSQL, not public): any instance can serve a Follow-up or a chart. Each row records the user, outcome, model calls and latency.
- **API keys** (`API_KEYS`, `name:key` pairs): `POST /v1/query` needs an `X-API-Key` header (the key alone or as `name:key`; case, quotes and stray spaces from a pasted key are ignored), and each user may ask 30 questions an hour (`USER_QUERIES_PER_HOUR`). Reading runs and charts stays open (run IDs are random). The web page asks for the key once. For the review, the live demo runs with `OPEN_ACCESS=true`: no key is needed and there is no hourly limit (a key that is sent still names its user).
- **Circuit breakers** for ClinicalTrials.gov and each model, and a **30 s run deadline** (`RUN_DEADLINE_SECONDS`). It covers loading a Follow-up's earlier run through verification. Saving the run comes after it, with its own 5 s limit; a save that runs out of time still returns the answer, with a warning that its image and follow-ups are unavailable.
- **Traces:** OpenTelemetry spans per stage (plus HTTP and model calls), exported over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

On AWS it runs on **ECS Express Mode** (App Runner closed to new customers in April 2026): Fargate tasks on ARM with 0.5 vCPU and 1 GB, 1–2 of them, behind an HTTPS load balancer and auto scaling that Express Mode manages; the image is in ECR. **Secrets Manager** holds the model keys, `API_KEYS` and the connection strings, and ECS reads them into environment variables when a task starts. The database and cache accept connections only from the service's security group.

```bash
API_KEYS=you:a-long-random-key docker compose up --build   # the hosted mode on this machine, with Redis and Postgres
deploy/aws/deploy.sh                                        # everything in one command: sets up what is missing, ships, smoke-tests
DRY_RUN=1 deploy/aws/deploy.sh                              # the same, read-only: lists what it would create or change
deploy/aws/teardown.sh                                      # delete the whole deployment (DRY_RUN=1 to preview; asks to confirm)
deploy/aws/create-service.sh                                # create the ECS Express Mode service (after the one-time setup)
TAG=v6 deploy/aws/update-service.sh                         # roll out new code: build, push, rolling update with rollback
TAG=v6 SET_ENV="OPEN_ACCESS=true" deploy/aws/update-service.sh   # the same, also changing settings
deploy/smoke-test.sh https://YOUR-SERVICE-URL               # health, a question, a follow-up and a chart
```

The hosted mode refuses to start without Redis, Postgres and API keys (API keys may be left out only with `OPEN_ACCESS=true`). `deploy/aws/deploy.sh` runs the whole deployment and is safe to run again: each step checks what exists and skips it, and secret values are never printed. The same one-time setup (registry, roles, database, cache, secrets) is also listed command by command in [`docs/hosted-deployment.md`](hosted-deployment.md#aws-deployment-2026-10-04), with costs (roughly $50–60 a month while running) and teardown. Every question costs 2–5 model calls (one split call, two if the primary model fails, then up to three planning calls); the per-user limit caps that (with open access, nothing does: tear the service down after the review).

## Idempotent retries and API keys (README §3)

**Idempotent retries.** Send an optional `Idempotency-Key` header (up to 255 characters) to make retries safe. Keys belong to the user who sent them: the same key from another API key is a new request.

| Case | Response |
|---|---|
| Same key, same request | The original response, unchanged: same `run_id`, no model call, no API requests, header `Idempotent-Replayed: true` |
| Same key, different request | **422** |
| Same key while the first request is still running | **409** (retry shortly) |
| Key older than 24 hours | Treated as new |

**API key (hosted).** When the service is configured with `API_KEYS` (and not `OPEN_ACCESS`), send `X-API-Key: <your key>`. A missing or wrong key gets **401** (`unauthorized`); more questions than the hourly limit get **429** (`rate_limited`) with a `Retry-After` header. Idempotent replays do not count against the limit.

Request equality is judged on the validated request, so whitespace and field order do not matter. A request that fails before producing a run (e.g. unknown `previous_run_id`) does not consume its key. Without the header, every POST is a new run. The JSON Schemas for request and response are served at `GET /v1/schema`.

## One real Datum of each chart type (README §4)

One real Datum of each type (`trial_ids` shortened). The numbers in brackets name the example in [`examples/`](../examples/); the single value and the table come from the service's tests:

```jsonc
// single_value
{"label": "Trials", "trial_count": 50, "trial_ids": ["NCT02122861", "…"]}
// bar_chart (02)
{"country": "United States", "trial_count": 248, "trial_ids": ["NCT00001823", "…"]}
// time_series (01); a crossed chart carries the series field, e.g. "phase", in place of estimated_count
{"start_year": "2008", "trial_count": 1, "estimated_count": 0, "trial_ids": ["NCT04898751"]}
// grouped_bar_chart (03)
{"phase": "Early Phase 1", "group": "semaglutide only", "trial_count": 5, "trial_ids": ["NCT05249881", "…"]}
// histogram (06)
{"enrollment_bin": "0", "enrollment_type": "Actual", "trial_count": 523, "trial_ids": ["NCT00003455", "…"]}
// table: columns nct_id, title, status, phase, start_date, lead_sponsor, enrollment
{"nct_id": "NCT05913388", "title": "GB1211 and Pembrolizumab …", "status": "ACTIVE_NOT_RECRUITING", "phase": "Phase 2",
 "start_date": "2024-02-29", "lead_sponsor": "Providence Health & Services", "enrollment": 12, "trial_ids": ["NCT05913388"]}
// timeline (07)
{"trial": "NCT05920356 · A Study Evaluating Sotorasib Platinum Doublet Comb", "nct_id": "NCT05920356", "start": "2023-11-16",
 "end": "2027-06-30", "dates": "Includes estimated dates", "status": "RECRUITING", "trial_ids": ["NCT05920356"]}
// scatter_plot (08)
{"nct_id": "NCT00696657", "title": "A Randomised Controlled Clinical Trial …", "duration_months": 8.1, "enrollment": 415,
 "values": "Actual values", "trial_ids": ["NCT00696657"]}
// network_graph (04): data is {"nodes": [...], "edges": [...]}
{"id": "sponsor:National Cancer Institute (NCI)", "label": "National Cancer Institute (NCI)", "kind": "sponsor",
 "trial_count": 127, "trial_ids": ["NCT00004028", "…"]}
{"source": "sponsor:National Cancer Institute (NCI)", "target": "drug:temozolomide", "kind": "sponsors_trials_of",
 "trial_count": 30, "trial_ids": ["NCT00039494", "…"]}
```

The encoding names these fields, e.g. a timeline's `{"x": {"field": "start"}, "x2": {"field": "end"}, "y": {"field": "trial"}, "color": {"field": "dates"}}`. A renderer reads `encoding` and `data` and needs nothing else.

The images are produced by translating this spec, and only this spec, into Vega-Lite with finished values. That doubles as a check that the spec is complete.

## Every failure point (README §7)

Every failure ends in one Outcome with a structured `error` (`code`, `message`, `retryable`), is logged with the run ID, and never shows provider response bodies. A Question that asks several things fails part by part: one part's error leaves the others answered.

| Failure point | What happens | Outcome · `error.code` |
|---|---|---|
| **Primary planning model fails** (5xx, 429, timeout, network, bad key) | The fallback model plans instead; the answer carries a warning naming the failure ("gpt-5.4-mini: HTTP 401") and `planner_model` names the model that answered | `success` + warning |
| **Every model fails**, retryably | Stopped before any trial is fetched (the list of country names, cached for the life of the process, is read first) | `upstream_error` · `planner_unavailable` (retryable) |
| **Every model rejects the request** (401/403/404: bad key or model name) | Stopped with a configuration hint | `upstream_error` · `planner_rejected` (not retryable) |
| **A model hangs** | Each attempt is limited to `PLANNER_TIMEOUT_SECONDS` (20 s, one quick retry, instead of the SDKs' 600 s with two retries); the whole step to `PLANNER_DEADLINE_SECONDS` (60 s) | `upstream_error` · `planner_timeout` (retryable) |
| **The model returns no usable plan** (text instead of a plan, after one retry) | Stopped; not sent to the fallback, which is for provider failures only | `internal_error` · `planner_invalid_output` (retryable) |
| **No model configured** | The service still starts; every question explains which key is missing | `internal_error` · `planner_not_configured` |
| **ClinicalTrials.gov** fails | Retries with backoff (honouring `Retry-After`), then stops with the reason | `upstream_error` · `source_unavailable` / `source_rate_limited` / `source_rejected` / `source_invalid_response` |
| **Over 20,000 matching trials** | Stopped after the first page, which reports the total; the message gives that count and asks to narrow the question. A plain total (no grouping, drug or sponsor) is answered with that registry count instead, citing the API request | `scope_required` · `scope_too_large`; or `success` |
| **A background run fails** (`POST /v1/runs`) | The status says `failed` with a structured error; a run that ends in any Outcome is `done` | `GET /v1/runs/{id}/status` |
| **No trial matches** | Says how the question was read; offers the corrections that find trials, each counted live with one filter removed. A drug name that no trial lists at all gets a spelling hint | `no_data` + `suggestions` |
| **Nothing to plot** (no trial reports a start date for a trend, the dates for a timeline, or enrollment for a scatter) | Stopped after full retrieval, naming the missing field | `no_data` |
| **The registry cannot answer the question** (efficacy, advice) | Says why, and offers up to three related questions it can answer | `unsupported_query` + `suggestions` |
| **The chart cannot be built** (Vega-Lite fails to compile) | Checked at query time by compiling the chart (milliseconds, no drawing): the answer, specification and citations stand; `chart_url` is withheld with a warning | `success` + warning |
| **Drawing the image fails or hangs** | Drawing runs off the event loop with a time limit; the image request fails in JSON, the run is unaffected | HTTP `500 render_failed` / `504 render_timeout` |
| **The interactive chart cannot load** (offline, CDN blocked) | The web page shows the same data as a table, still clickable for citations | — |
| **The answer fails verification** | Withheld, with the failed checks | `internal_error` · `verification_failed` |
| **A bug** | Isolated to its part; logged with a stack trace and the run ID, which the message quotes | `internal_error` · `internal` |
| **The run record cannot be saved** (full disk, Postgres down) | The answer is still returned; image links and follow-ups are withdrawn with a warning. Reading a run while Postgres is down answers 503 | `success` + warning; HTTP `503 store_unavailable` |
| **A dependency keeps failing** | After 5 outage-type failures in a row its circuit breaker opens for 30 s: ClinicalTrials.gov calls fail at once, and an open model is skipped so the fallback answers without waiting. A rejected key never opens it | `upstream_error` · `source_unavailable`; `success` + warning |
| **Redis is down** (hosted) | Each instance falls back to its own rate limit and user counts, the page cache misses, Idempotency-Keys are skipped; logged once | — |
| **A run takes too long** (hosted: 30 s) | Parts already answered stand; the part under way and later ones end with the stage they reached and a hint to narrow the question | `upstream_error` · `run_timeout` (retryable) |
| **No or wrong API key, or over the hourly limit** (hosted) | Refused before any model call | HTTP `401 unauthorized` / `429 rate_limited` + `Retry-After` |

HTTP-level errors (unknown run, idempotency conflicts, image failures, anything unexpected) share one JSON shape: `{"detail": {"code", "message", "retryable"}}`. Request validation errors keep FastAPI's standard 422 format.

## What the tests cover (README §9)

- **329 offline tests**, most of them through the HTTP API, with a scripted planner and ClinicalTrials.gov mocked by real records saved from the API. They cover:
  - every chart type, clarifications, follow-ups, repair
  - `scope_required`, `no_data`, upstream errors, rate-limit retries
  - the Filter module (29 tests): 17 filter combinations compiled to API parameters, 850 local checks on saved trials, and 10 merges of plan filters with structured fields, each pinned to behaviour recorded before the module existed
  - answering (14 tests) across the module's own interface, and the model-call ledger (5 tests)
  - every failure point in §7 (26 tests): fallback to the second model with a warning, every model failing or rejecting, a hanging model, a model returning text instead of a plan, ClinicalTrials.gov errors by status, a chart that cannot compile or draw, an unsaved run record, and a bug confined to one part of a multi-part Question
  - the hosted mode (36 tests, with an in-memory Redis and SQLite in place of Postgres): settings that refuse to start or leak secrets, run history shared through SQL, one request budget and page cache across two instances, Idempotency-Keys across instances, a Redis outage, API keys and hourly limits (per user, and Idempotency-Keys scoped per user), open access, circuit breakers opening and closing, the run deadline keeping finished parts and covering a slow run store
  - counting rules (multi-phase, distinct trials per country, no year gaps, top-N + Other with "Not reported" kept separate, missing values as their own state, the drug filter keeping only drug-type interventions), with property tests showing input order and duplicates do not change counts
  - the additions of 2026-10-27: the age group (the worked extension example), investigator and site networks with name cleaning, keyword excerpts re-derived by the verifier, a sponsor side of a comparison asking which organization (one side, or both at once), the sponsor-category repair, the registry total above the cap, background runs, the image cache, and run bundles replayed offline to the same answer
  - **tamper tests** proving the verifier rejects a changed count, a trial moved to the wrong bar or bin, a chart grouped by the wrong field, a count that leaves out part of the cohort, a multi-phase trial missing from one of its phase bars, a trial wrongly counted in "Other", an altered citation value or link, a trial cited for a network edge it lacks, an edge without a shared arm, and a trial outside the filters, an altered keyword excerpt
- **Live tests** against ClinicalTrials.gov (`pytest -m live`), and every answer type run end to end with the real models, with the images inspected (tables have none).
- **Planner eval** (`evals/`): 66 questions modelled on the assignment's appendix, 17 of them adversarial, plus a 12-question held-out set scored only at the end, per question family per model (results in `evals/results/`).
- **An external code review** by a second model, every finding checked against the code. Ten were real and are fixed, each with a regression test that failed first. Among them: the drug filter kept trials that gave the drug only as a device or tracer; fallback attempts escaped the 3-call limit; the verifier missed a wrong grouping and altered citations; "Not reported" was folded into "Other"; and picking your own filter's value in a conflict clarification asked again forever.
- **Iteration driven by real data.** Each of these was found by running the real service, then fixed and covered by a test:
  - procedures counted as drugs (MeSH terms span all interventions) → drug identity matched to drug-type interventions
  - HTTP 429 → `Retry-After` handling
  - unreadable network images → 60-link cap
  - alternatives in one arm ("cisplatin OR carboplatin") counted as combinations → detected from the arm description; checked on the real KEYNOTE-189 record and on real "either / investigator's choice" arm texts, including a false positive the tests caught (a dose unit "mg/m²" read as "or")
  - incomplete current-year counts → assumption
  - shallow citations → per-filter source values with a verifier check
  - multi-part questions: one unrelated question silently merged with another into a wrong single number, and second requests were dropped. Asking the model to produce a multi-part plan was unreliable (about 50% across repeated runs, and its repair turn confused it), so code split the message and the model planned each part as a normal question. A later probe of nine phrasings found the code splitter still missing some (". Also, …", "… plus …", "1) … 2) …", "show their phases"), three of them dropping a part silently. A dedicated split call, which only lists the questions and rewrites each to stand alone, handled all nine phrasings of the probe, and 35 of 35 repeated runs of the seven hardest (rewrites standing alone, comparisons kept whole). The eval gained four multi-part cases
  - a refinement in the web page silently dropped the user's Clarification answer (the exact Merck companies) → refining Follow-ups now inherit the earlier structured fields
  - the first AWS deployment ([details](hosted-deployment.md#first-deployment-what-went-wrong-and-the-fixes)):
    - it was rolled back because new service roles were used in the same second they were created; the fix was to create them in setup
    - every task waited a minute on the database's closed firewall and was replaced → the table is now created in the background, with second-scale connection timeouts (regression tests added)
    - keys pasted through a notes app were refused → case, quotes and invisible characters are ignored
  - network charts in dark mode had black labels and a merged, oversized legend → theme-aware labels and separate legends, checked for every chart type in both themes
