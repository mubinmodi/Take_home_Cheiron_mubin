# Clinical Trials Question-to-Visualization

A backend service that answers natural-language questions about clinical trials with a **frontend-renderable visualization specification**. Every number in it is computed from the [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api), and every bar, point, node and edge is **cited** down to the trials and source fields behind it.

```
POST /v1/query  {"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab"}
→ time_series of trials per start year · 2,620 cited trials · verified · chart at /v1/runs/{id}/chart.png
```

## The task, as I understood it

A user asks a question about clinical trials in plain English ("How has the number of Keytruda trials changed since 2015?"), optionally with structured fields such as `drug_name` or `trial_phase`. The service must answer with a **visualization specification** that a frontend can draw without further work: the chart type, title, encoding, data and metadata. Every number must come from the live ClinicalTrials.gov API, and the bonus asks that each data point cite the trials behind it.

What makes it hard:
- **Questions are ambiguous.** "Merck" is two companies, "PD-1 inhibitors" is a class rather than a drug, and a structured field can contradict the question.
- **Counting needs definitions.** A trial with two phases, a trial with sites in ten countries, a trial that only *mentions* a drug: the answer depends on rules someone has to choose and state.
- **The registry is messy.** Start dates are often estimated, one drug appears under hundreds of spellings, and an arm can list alternatives ("cisplatin or carboplatin") that look like combinations.
- **A language model must not invent numbers.** Any count, trial ID or citation it produced would be unverifiable.

## My approach

**The model reads the question; code does everything else.** A first model call lists the separate questions a message asks, each rewritten to stand alone. A second turns each question into a typed **Query Plan**: the filters, an operation (`aggregate`, `compare`, `per_trial`, `bin`, `relate`) and what to group by. From there plain, tested code takes over:
- it fetches **every** matching trial from the live API, and never samples;
- it counts distinct trials under documented rules;
- it chooses the chart type from the plan;
- it builds the specification with citations.

A **verifier** then re-derives every count and cited value from the trial records before any answer is returned. When there is no sensible default the service asks a multiple-choice question built from the data; otherwise it states its default in the answer's assumptions. Every run ends in one explicit outcome, such as `success`, `clarification_required` or `no_data`, and the model never sees trial records.

**What I built:**
- nine answer types:
  - trends: `time_series`
  - distributions and geography: `bar_chart`
  - comparisons with an overlap group: `grouped_bar_chart`
  - single counts: `single_value`
  - trial lists: `table`
  - trial timelines: `timeline`
  - enrollment against duration: `scatter_plot`
  - enrollment distributions: `histogram`
  - sponsor ↔ drug networks and same-arm drug ↔ drug combination networks: `network_graph`
- deep citations: every data point lists its trials, and each trial carries the source field values that placed it there;
- clarifications for ambiguous sponsors, drug classes, missing references and fields that contradict the question;
- when there is no answer, a plain reason and a way forward: how the question was read, corrections counted live ("without the country filter: about 126 trials", one click to apply), a hint for a drug name nobody lists, or questions the registry can answer;
- follow-ups and corrections ("only phase 3") through `previous_run_id`, and questions that ask several things at once;
- PNG/SVG images, and a web page where clicking any bar, point, node or link shows its trials;
- a hosted version on AWS: a [live demo](https://cl-f44fdf3287b047ef971affaa8d767246.ecs.us-east-2.on.aws), no key needed.

**What I chose not to build:**
- **No agent loop:** a fixed workflow keeps cost bounded (usually 2 model calls, at most 5) and every step testable.
- **No RAG or vector database:** the data is structured and queried live.
- **No local copy of the registry:** answers reflect today's data.
- **Not needed for this task:** conversation memory beyond the previous answer, an MCP server, a graph database.
- **Not in the data, or left for later (§8):** investigator and site networks, maps, and efficacy questions, which the registry cannot answer.

**Where to read more:**
- §1: running it
- §2: the system design
- §3–§4: the request and response schemas
- §5: example runs
- §6: key decisions and their tradeoffs
- §7: failure handling
- §8: what I would improve with more time
- §9: how correctness was checked

---

## 1. Run it

Requirements: Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # install
cp .env.example .env         # then set OPENAI_API_KEY and/or ANTHROPIC_API_KEY (GOOGLE_API_KEY for Gemini)
uv run clinical-trials-viz serve          # web page at http://127.0.0.1:8000, API docs at /docs
```

**Web page.** Open http://127.0.0.1:8000 after `serve`. Served by the API itself; nothing extra to install.
- Ask a question, optionally pinning filters (phase and status lists come from `/v1/schema`).
- See the chart interactively (Vega-Lite in the browser, from the same translation as the PNG).
- **Click any bar, point, node, link or table row to see its cited trials**, with links and the source values that placed each one there.
- Clarifications appear as clickable options (multi-select where allowed).
- **Follow-ups:** below the latest answer, "Ask a follow-up about the latest answer" takes a short change ("only phase 3", "show it by start year instead"; one-click examples are offered) and sends it with that answer's `previous_run_id`. Each follow-up's answer is added below as its own section, labelled as a refinement of the previous answer or a new question, so the whole conversation stays on the page with every chart and citation. "Ask a new question" starts over.
- Failures show their error code, with a Retry button when retrying may help; if the chart library cannot load, the chart's data is shown as a clickable table instead.
- A Light / Dark / Auto switch (Auto follows the system setting); charts redraw in the chosen theme.
- Every submit carries an `Idempotency-Key`, so a double-click never runs twice.
- Registry text is always inserted as text, never as markup.

Ask from the command line (in-process; prints a summary and saves the chart under `data/charts/`):

```bash
uv run clinical-trials-viz ask "Which countries have the most recruiting trials for melanoma?"
uv run clinical-trials-viz ask "How has the number of trials for this drug changed over time?" --fields '{"drug_name": "Pembrolizumab"}'
uv run clinical-trials-viz ask "What phases are Merck's trials in?"          # → clarification with options
uv run clinical-trials-viz ask "What phases are Merck's trials in?" --previous RUN_ID --fields '{"sponsor": ["Merck Sharp & Dohme LLC"]}'
uv run clinical-trials-viz ask "..." --json                                   # full JSON response
```

**Configuration.** All settings are environment variables, documented in [`.env.example`](.env.example):
- planner models: primary and fallback, as `provider:model` strings, with a per-attempt timeout and a deadline for planning
- ClinicalTrials.gov base URL, rate limit, timeout and page cap
- run-record directory
- public base URL used in `chart_url` (optional: by default links use the address each request arrived on)
- chart image time limit, log level, and OpenTelemetry exporter (`none`, `console` or `otlp`)
- hosted mode: Redis, Postgres, API keys, per-user limit, run deadline and circuit breakers (all unset locally)

Models change by configuration only. Default: `openai:gpt-5.4-mini`, with `anthropic:claude-haiku-4-5` as fallback.

**Checks.**

```bash
uv run pytest                    # offline tests (ClinicalTrials.gov mocked with saved real records)
uv run pytest -m live            # tests against the live ClinicalTrials.gov API
uv run python -m evals.run       # score the configured planner on 46 questions (needs an API key)
uv run ruff check src tests && uv run pyright
```

**Hosted version (AWS).** Live demo: https://cl-f44fdf3287b047ef971affaa8d767246.ecs.us-east-2.on.aws (open for the review: no API key needed). The same container runs on AWS in us-east-2, as decided in [`docs/hosted-deployment.md`](docs/hosted-deployment.md). `DEPLOYMENT=hosted` switches on:
- **Shared state in Redis** (`REDIS_URL`; ElastiCache Serverless for Valkey): one ClinicalTrials.gov request budget for all instances (the registry's limit is per IP), the page cache, Idempotency-Keys and per-user counts. If Redis fails, each falls back to working per instance.
- **Run history in Postgres** (`DATABASE_URL`; RDS for PostgreSQL, not public): any instance can serve a Follow-up or a chart. Each row records the user, outcome, model calls and latency.
- **API keys** (`API_KEYS`, `name:key` pairs): `POST /v1/query` needs an `X-API-Key` header (the key alone or as `name:key`; case, quotes and stray spaces from a pasted key are ignored), and each user may ask 30 questions an hour (`USER_QUERIES_PER_HOUR`). Reading runs and charts stays open (run IDs are random). The web page asks for the key once. For the review, the live demo runs with `OPEN_ACCESS=true`: no key is needed and there is no hourly limit (a key that is sent still names its user).
- **Circuit breakers** for ClinicalTrials.gov and each model, and a **30 s run deadline** (`RUN_DEADLINE_SECONDS`). It covers loading a Follow-up's earlier run through verification. Saving the run comes after it, with its own 5 s limit; a save that runs out of time still returns the answer, with a warning that its image and follow-ups are unavailable.
- **Traces:** OpenTelemetry spans per stage (plus HTTP and model calls), exported over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

On AWS it runs on **ECS Express Mode** (App Runner closed to new customers in April 2026): Fargate tasks on ARM with 0.5 vCPU and 1 GB, 1–2 of them, behind an HTTPS load balancer and auto scaling that Express Mode manages; the image is in ECR. **Secrets Manager** holds the model keys, `API_KEYS` and the connection strings, and ECS reads them into environment variables when a task starts. The database and cache accept connections only from the service's security group.

```bash
API_KEYS=you:a-long-random-key docker compose up --build   # the hosted mode on this machine, with Redis and Postgres
deploy/aws/create-service.sh                                # create the ECS Express Mode service (after the one-time setup)
TAG=v6 deploy/aws/update-service.sh                         # roll out new code: build, push, rolling update with rollback
TAG=v6 SET_ENV="OPEN_ACCESS=true" deploy/aws/update-service.sh   # the same, also changing settings
deploy/smoke-test.sh https://YOUR-SERVICE-URL               # health, a question, a follow-up and a chart
```

The hosted mode refuses to start without Redis, Postgres and API keys (API keys may be left out only with `OPEN_ACCESS=true`). The one-time AWS setup (registry, roles, database, cache, secrets) is listed command by command in [`docs/hosted-deployment.md`](docs/hosted-deployment.md#aws-deployment-2026-10-04), with costs (roughly $50–60 a month while running) and teardown. Every question costs 1–3 model calls; the per-user limit caps that (with open access, nothing does: tear the service down after the review).

---

## 2. How it works

```
request ─► LOAD ─► SPLIT ─► PLAN ─► GATE ─► RETRIEVE ─► COUNT + BUILD SPEC ─► VERIFY ─► response + run record
                            └ each part ──┘ └──────── then each part in turn ─────────┘
                            (at most 3 parts: all are planned and gated first, then answered one by one)

LOAD      follow-ups only: the earlier run's plan and request, by previous_run_id          code
SPLIT     list the separate questions, each rewritten to stand alone                        the model
PLAN      the question → a typed Query Plan, or a clarification request, or a refusal       the model
GATE      merge structured fields, check the plan against the catalog, repair once          code (+1 model call)
RETRIEVE  every page from ClinicalTrials.gov; drug match check                             code
COUNT +   count distinct trials; choose the chart type from the plan; build the spec        code
BUILD SPEC  and the evidence
VERIFY    re-derive every count and cited value from the trial records                     code

Exits (each part ends in exactly one outcome):
  PLAN      unsupported_query · clarification_required (options built by code from data)
  GATE      unsupported_query · clarification_required when a field contradicts the question
  RETRIEVE  scope_required (over 20,000 trials) · no_data · clarification_required ("which Merck?")
  COUNT     no_data (nothing to plot, e.g. no trial reports a start date)
  VERIFY    internal_error (verification_failed): the answer is withheld
  any step  upstream_error / internal_error with a structured error; hosted: run_timeout after 30 s
```

- **Two model steps, both reading only the question.** The **split step** lists the separate questions a message asks (below). The **planning step**, a pydantic-ai agent, turns each question (plus any structured fields, plus the previous plan for follow-ups) into a typed **Query Plan**: filters, operation (`aggregate`, `compare`, `per_trial`, `bin`, `relate`), what to group by (optionally with a second **series** dimension, "phases per year"), and a view or network kind. It may instead return a clarification request or a refusal. The model **never sees trial records** and **never outputs numbers, trial IDs or citations**.
- **Questions that ask several things are split before planning.** One model call lists the separate questions in the message and rewrites each to stand alone: "List recruiting Keytruda trials in Germany and show their phases" becomes "List recruiting Keytruda trials in Germany" and "Show the phases of recruiting Keytruda trials in Germany". A comparison ("A vs B") stays one question. Each **part** is then planned on its own as an ordinary single question, with the full message as context only, so the planning step never sees a multi-part shape. Each part gets its own filters, chart, citations and verification, and fails or asks independently. At most 3 parts are answered; a note names any that are not. If the split reply is unusable, simple code rules split the message instead, with a warning. Follow-ups are never split.
- **Code does everything else:**
  - compiles filters into API requests
  - retrieves **every** page, or refuses with `scope_required` and never samples
  - applies match checks
  - counts distinct trials
  - chooses the chart type deterministically from the plan
  - builds the spec and evidence
  - verifies
- **When the answer is a chart** (the assignment asks both to judge whether a visualization fits and for a visualization as the answer). Every successful analytical answer is a visualization specification; a single number is a `single_value` and a list of trials a `table`, so a renderer handles every answer the same way. Code picks the type from the plan: counts over start years → `time_series`; a breakdown → `bar_chart`; a comparison or a crossed breakdown → `grouped_bar_chart`; enrollment distribution → `histogram`; trial by trial → `table`, `timeline` or `scatter_plot`; relationships → `network_graph`. Outcomes that are not answers (clarification, unsupported, no data, scope required, errors) carry no visualization; they say why and what to do next. A `no_data` answer states how the question was read and, in `suggestions`, offers the corrections that find trials, each counted live with that one filter removed. An `unsupported_query` answer offers up to three related questions the registry can answer.
- **Bounded:** one split call (two if the primary model fails), then at most 3 planning calls per run, shared by the parts (plan, one repair with the validator's errors, one provider fallback on transport errors only). A single question usually takes 2 calls. Every attempt counts, a failed one included, and a model that failed is not tried again in the same run. The SDK's own single retry of a request is not counted. Every run ends in exactly one **outcome**: `success`, `no_data`, `clarification_required`, `unsupported_query`, `scope_required`, `upstream_error`, `internal_error`.
- **Verifier**, a gate before every successful response. It checks that:
  - the chart answers the plan: its type, grouped by the plan's dimensions
  - every encoded field exists
  - each count equals its distinct cited trials, and a chart of the whole cohort (single value, bar, time series, grouped bar, histogram) counts every trial in it
  - every citation resolves, and every value it quotes is re-derived from the trial record
  - each cited trial really has its bucket's value, re-derived from its own record, and every trial with that value is in the bucket (a multi-phase trial under each of its phases)
  - each cited trial meets every filter
  - network edges join existing nodes, and every cited trial has both ends (or a shared arm, for combinations)
  - time series have no gaps

  A failed check withholds the answer.

Code map (`src/clinical_trials_viz/`):

| Module | Role |
|---|---|
| `pipeline.py` | The fixed workflow and outcomes |
| `planning.py` | Split a multi-part question; plan each part; gate and one repair |
| `planner.py` | The model step |
| `validate.py` | Merge structured fields; semantic gate |
| `cohort.py` | Compile filters, retrieve, drug match check |
| `analyze.py`, `network.py` | All counting |
| `spec_builder.py` | Specification and evidence |
| `verify.py` | The gate |
| `render.py` | Spec → Vega-Lite → PNG/SVG |
| `clarify.py` | Clarification options built from data |
| `suggest.py` | Corrections for `no_data`, counted live |
| `catalog.py` | The versioned capability catalog: dimensions, enums, counting constants |
| `ctgov/` | API client and the typed trial record |
| `failures.py` | Any exception → one outcome and a structured error |
| `runs.py`, `idempotency.py` | Run records (files, or Postgres when hosted) and Idempotency-Keys |
| `shared_state.py`, `access.py`, `breaker.py` | Hosted mode: Redis-backed shared state, API keys and limits, circuit breakers |
| `config.py`, `telemetry.py` | Settings; logging and OpenTelemetry |
| `api.py`, `cli.py`, `web/index.html` | HTTP API, command line, web page |

**Extending.** Adding a dimension (say, primary purpose) touches three places: an entry in `catalog.py` (label, the description the planner prompt shows, the source field to cite, and whether it is multi-valued, ordered or long-tailed); one case in `ctgov/trial.py` that reads the value and its citation from a trial record (plus the API field in `TRIAL_FIELDS`); and a fixture test with an eval question. Counting (`analyze.py`), chart choice and spec building (`spec_builder.py`), the verifier and the plan gate all work from the catalog, so they need no change. A new chart type touches `spec_builder.py` (its data shape), `render.py` (its Vega-Lite) and `verify.py` (its checks).

Design notes: [`docs/harness-design.md`](docs/harness-design.md). API findings: [`docs/research/api-data-guide.md`](docs/research/api-data-guide.md). Glossary: [`CONTEXT.md`](CONTEXT.md).

---

## 3. Request schema — `POST /v1/query`

Only `query` is required. Every other field pins a filter, so the model does not have to extract it. Fields marked "one or list" accept a single value or a list. Unknown fields are rejected (HTTP 422).

| Field | Type | Validation | Meaning |
|---|---|---|---|
| `query` | string | required; 1–1,000 chars after trimming | The question |
| `drug_name` | string or list | ≤ 5 items, each 1–200 chars | Drug(s); brand or code names work (Keytruda, MK-3475). Resolves "this drug". |
| `condition` | string or list | ≤ 5 items | Condition(s) / disease(s) |
| `trial_phase` | enum or list | `EARLY_PHASE1`, `PHASE1`–`PHASE4`, `NA` (any case) | Phase filter |
| `sponsor` | string or list | ≤ 10 items | Lead sponsor name(s), matched exactly; a list means any of them (used to answer sponsor clarifications) |
| `country` | string or list | ≤ 10 items | Countries with a current site; common aliases (USA, UK) accepted |
| `status` | enum or list | ClinicalTrials.gov overall status, e.g. `RECRUITING` | Status filter |
| `start_year`, `end_year` | integer | 1990 to current year + 5; `start_year` ≤ `end_year` | Trial start-date range |
| `nct_id` | string or list | `NCT` + 8 digits; ≤ 20 | Specific trial(s) |
| `previous_run_id` | string | must exist (404 otherwise) | Follow-up, correction, or clarification answer |

If a structured field and the question name different values for the same filter, the service asks which one you mean and never picks one. The planner only reads the question; code compares it with the fields and builds the clarification: `field` is the request field (e.g. `trial_phase`), `reason` is `conflict`, and the two options are valid values for that field, e.g. "Phase 3 (your question)" → `["PHASE3"]` and "Phase 2 (your filters)" → `["PHASE2"]`. This covers every field above, including `sponsor` and the years. Send the chosen value with `previous_run_id`; that answer is final, and it is not asked again. A sponsor term that matches the chosen names ("Merck" and "Merck Sharp & Dohme LLC") is not a conflict, and choosing the question's sponsor term searches it as a lead sponsor name, so "which Merck?" can still be asked. The fields and the rule live in the capability catalog (`catalog.PINNED_FIELDS`). A brand name and its generic name ("Keytruda" and "pembrolizumab") are not a conflict: code checks that both resolve to the same drug in the registry and says so in the assumptions.

**Idempotent retries.** Send an optional `Idempotency-Key` header (up to 255 characters) to make retries safe. Keys belong to the user who sent them: the same key from another API key is a new request.

| Case | Response |
|---|---|
| Same key, same request | The original response, unchanged: same `run_id`, no model call, no API requests, header `Idempotent-Replayed: true` |
| Same key, different request | **422** |
| Same key while the first request is still running | **409** (retry shortly) |
| Key older than 24 hours | Treated as new |

**API key (hosted).** When the service is configured with `API_KEYS` (and not `OPEN_ACCESS`), send `X-API-Key: <your key>`. A missing or wrong key gets **401** (`unauthorized`); more questions than the hourly limit get **429** (`rate_limited`) with a `Retry-After` header. Idempotent replays do not count against the limit.

Request equality is judged on the validated request, so whitespace and field order do not matter. A request that fails before producing a run (e.g. unknown `previous_run_id`) does not consume its key. Without the header, every POST is a new run. The JSON Schemas for request and response are served at `GET /v1/schema`.

Other endpoints:
- `GET /v1/runs/{run_id}`: the saved run record (request, plan, response)
- `GET /v1/runs/{run_id}/chart.png` and `.svg` (`?part=N` for part N+1 of a multi-part question): the rendered chart
- `GET /v1/runs/{run_id}/vega-lite.json` (`?part=N`): the chart as Vega-Lite with finished values; each mark carries `_datum`, its index in the spec's Datums (rows, or nodes then edges), so a client can show the Citation for whatever is clicked
- `GET /v1/schema`: the JSON Schemas of the request and the response
- `GET /`: the web page
- `GET /health`

---

## 4. Response schema

Every Outcome returns HTTP 200 with the outcome in the body; HTTP error codes are only for requests that cannot run (listed in §3 and §7).

| Field | Meaning |
|---|---|
| `run_id`, `outcome`, `message` | Identity and result; `message` explains non-success outcomes |
| `error` | On a failure: `{code, message, retryable}` (see §7); `retryable` says whether sending the same request later may work |
| `warnings` | Problems that did not change the answer, e.g. the primary model failed and the fallback answered, or the chart image could not be prepared |
| `planner_model` | The model that produced the plan (the fallback, if the primary failed) |
| `additional_answers` | When the Question asks several things: the answers to parts 2 and 3, each with the same fields as the top level (outcome, plan, filters, visualization, `chart_url` with `?part=N`, evidence, assumptions, clarification, verification). The top level is part 1; `plan.kind == "multi"` lists every part's request in `plan.requests`. |
| `visualization` | The **Visualization Specification** (below); present on success |
| `chart_url` | Link to the rendered image (absent for tables) |
| `evidence` | `{nct_id: {nct_id, title, url, fields}}`: each cited trial once, with the **source field values** (API field path → value) that placed it in the data and satisfied each filter |
| `assumptions` | Defaults applied and data caveats (e.g. "9 of 126 search matches were excluded because 'Keytruda' is not one of their drug interventions") |
| `applied_filters` | The filters actually applied after merging structured fields; `from_request` names those pinned by the caller |
| `suggestions` | When there is no answer: `[{label, follow_up, query, trial_count}]`. A correction (`no_data`) has `follow_up`, text to send with `previous_run_id`, and `trial_count`, counted live; a question the registry can answer (`unsupported_query`) has `query`, to send as a new question |
| `clarification` | On `clarification_required`: `{field, question, reason, options:[{label, value, trial_count}], multi_select, allow_free_text}`. Send the chosen `value` back in `field` with `previous_run_id`. `reason` is `conflict` when a structured field contradicts the question. |
| `plan`, `relation` | The model's Query Plan, and whether a follow-up refined (`refine`) or replaced (`new`) the previous one |
| `source` | API version, data timestamp, retrieval time, search matches, cohort size, API requests |
| `verification` | `{passed, checks:[{name, passed, detail}]}` |
| `model_calls`, `timings_ms` | Cost and per-stage timing |

### Visualization Specification

```json
{
  "type": "bar_chart",
  "title": "Trials by country: melanoma; recruiting",
  "subtitle": "480 trials",
  "encoding": {
    "x": {"field": "country", "type": "nominal", "title": "Country"},
    "y": {"field": "trial_count", "type": "quantitative", "title": "Trials"},
    "tooltip": [...]
  },
  "data": [{"country": "United States", "trial_count": 246, "trial_ids": ["NCT…", "…"]}, "…"],
  "metadata": {"units": "trials", "sort": "data_order", "category_order": ["United States", "…", "Other"],
               "top_n": 10, "other_bucket": true, "multi_valued": true, "cohort_size": 480}
}
```

Rules every renderer can rely on:
- `data` is already in display order (`metadata.sort = "data_order"`; `category_order` and `series_order` repeat it).
- Every **Datum** carries `trial_ids`, and when it shows a count, `trial_count == len(trial_ids)`.
- No aggregation is left to the renderer.

Each type, its data and its channels:

| `type` | `data` | Channels |
|---|---|---|
| `single_value` | `[{label, trial_count, trial_ids}]` | `value` |
| `bar_chart` | one row per category | `x` category, `y` count; `metadata.top_n`, `other_bucket`, `multi_valued` (a trial can fall in several categories) |
| `time_series` | one row per start year, no gaps, plus `estimated_count`; for a crossed chart one row per year × series value | `x` year (`time_granularity: "year"`), `y` count, optional `color` = series (one line each). A `Not reported` row holds undated trials and is not plotted on the axis. |
| `grouped_bar_chart` | one row per category × comparison group (`"A only"`, `"B only"`, `"Both"`/`"More than one"`), or × series value for a crossed chart | `x`, `y`, `color` = group or series (`metadata.series_order`) |
| `histogram` | one row per enrollment bin × enrollment type (Actual / Estimated / Type not reported) | `x` bin (ordinal), `y` count, `color` type; `metadata.bins = [{label, min, max}]` |
| `table` | one row per trial (≤ 100; `metadata.total_rows` = all) | `columns` |
| `timeline` | one row per trial: `start`, `end` (ISO dates), `dates` (actual vs estimated) | `x` start, `x2` end, `y` trial, `color` |
| `scatter_plot` | one point per trial: `duration_months`, `enrollment`, `values` | `x`, `y` (`metadata.y_scale: "symlog"`), `color` |
| `network_graph` | `{"nodes": [{id, label, kind, trial_count, trial_ids}], "edges": [{source, target, kind, trial_count, trial_ids}]}` | `label`, `size`, `color` (node kind), `source`, `target`, `weight`; `metadata.node_kinds`, `bipartite`, `min_edge_trials` |

One real Datum of each type (`trial_ids` shortened). The numbers in brackets name the example in [`examples/`](examples/); the single value and the table come from the service's tests:

```jsonc
// single_value
{"label": "Trials", "trial_count": 50, "trial_ids": ["NCT02122861", "…"]}
// bar_chart (02)
{"country": "United States", "trial_count": 246, "trial_ids": ["NCT00001823", "…"]}
// time_series (01); a crossed chart adds the series field, e.g. "phase"
{"start_year": "2008", "trial_count": 1, "estimated_count": 0, "trial_ids": ["NCT04898751"]}
// grouped_bar_chart (03)
{"phase": "Early Phase 1", "group": "semaglutide only", "trial_count": 5, "trial_ids": ["NCT05249881", "…"]}
// histogram (06)
{"enrollment_bin": "0", "enrollment_type": "Actual", "trial_count": 522, "trial_ids": ["NCT00003455", "…"]}
// table: columns nct_id, title, status, phase, start_date, lead_sponsor, enrollment
{"nct_id": "NCT05913388", "title": "GB1211 and Pembrolizumab …", "status": "ACTIVE_NOT_RECRUITING", "phase": "Phase 2",
 "start_date": "2024-02-29", "lead_sponsor": "Providence Health & Services", "enrollment": 12, "trial_ids": ["NCT05913388"]}
// timeline (07)
{"trial": "NCT05899608 · Clinical Study of Ivonescimab …", "nct_id": "NCT05899608", "start": "2023-10-26",
 "end": "2028-12-31", "dates": "Includes estimated dates", "status": "RECRUITING", "trial_ids": ["NCT05899608"]}
// scatter_plot (08)
{"nct_id": "NCT00696657", "title": "A Randomised Controlled Clinical Trial …", "duration_months": 8.1, "enrollment": 415,
 "values": "Actual values", "trial_ids": ["NCT00696657"]}
// network_graph (04): data is {"nodes": [...], "edges": [...]}
{"id": "sponsor:National Cancer Institute (NCI)", "label": "National Cancer Institute (NCI)", "kind": "sponsor",
 "trial_count": 128, "trial_ids": ["NCT00004028", "…"]}
{"source": "sponsor:National Cancer Institute (NCI)", "target": "drug:temozolomide", "kind": "sponsors_trials_of",
 "trial_count": 31, "trial_ids": ["NCT00039494", "…"]}
```

The encoding names these fields, e.g. a timeline's `{"x": {"field": "start"}, "x2": {"field": "end"}, "y": {"field": "trial"}, "color": {"field": "dates"}}`. A renderer reads `encoding` and `data` and needs nothing else.

The images are produced by translating this spec, and only this spec, into Vega-Lite with finished values. That doubles as a check that the spec is complete.

### Citations

The assignment asks for references of the form `{nct_id, excerpt}` (or a field/value). Ours is the field/value form, split in two so each trial is described once however many Datums cite it:
- each Datum lists the trials behind it in `trial_ids`;
- `evidence[nct_id]` holds the trial's title, its ClinicalTrials.gov link, and `fields`: each API field path with the value it had in the record.

For the "China" bar of example 02, `evidence["NCT03340506"]` is:

```json
{"nct_id": "NCT03340506", "title": "Dabrafenib and/or Trametinib Rollover Study",
 "url": "https://clinicaltrials.gov/study/NCT03340506",
 "fields": {
   "protocolSection.identificationModule.nctId": "NCT03340506",
   "protocolSection.statusModule.overallStatus": "RECRUITING",
   "protocolSection.contactsLocationsModule.locations.country": ["United States", "Argentina", "…", "China", "…"],
   "derivedSection.conditionBrowseModule.meshes.term": ["Melanoma", "…"]}}
```

`fields` holds the values that placed the trial in the answer: the field it is grouped by (countries: it is counted under each of them) and the field behind each filter (status, condition). The verifier re-derives every one of these values from the trial record before answering.

---

## 5. Example runs

[`examples/`](examples/) holds the five submitted examples (01–05; the assignment asks for 3–5) and four more outputs (06–09), one for each remaining chart type. All are real runs of the service against the live ClinicalTrials.gov API, unedited: request, full JSON response and chart.

| Example | Outcome |
|---|---|
| The assignment's own request ("this drug" + `drug_name: Pembrolizumab`) | `time_series`, 2,620 cited trials |
| "Which countries have the most recruiting trials for melanoma?" | `bar_chart`, 480 |
| "Compare phases for trials involving semaglutide vs tirzepatide" | `grouped_bar_chart`, 824 |
| "Show a network of sponsors and drugs for glioblastoma trials" | `network_graph`: 975 trials cited by the 52 links shown (of 2,269 matching trials, 1,736 with a drug and a lead sponsor) |
| "What phases are Merck's trials in?" | `clarification_required`: Merck Sharp & Dohme (2,151) / Merck KGaA (275) / All of these (2,426) |
| "What is the enrollment distribution of breast cancer trials?" | `histogram`, 16,873 |
| "Show a timeline of recruiting phase 3 Keytruda trials in Germany" | `timeline`, the 50 most recently started of 55 |
| "Plot enrollment against duration for completed semaglutide trials" | `scatter_plot`, 309 |
| "Which drugs are most often combined with pembrolizumab?" | `network_graph` (drug ↔ drug, same arm), 1,776 |

Regenerate with `uv run python -m examples.generate`.

### The assignment's nine example questions

All nine are supported. The eval (`evals/questions.json`) checks the planner on each family with different drugs, conditions and phrasings, none of them copied into the prompt.

| # | Example | Answer | What defines it | Eval cases |
|---|---|---|---|---|
| Q-01 | Trials for a drug per year since 2015 | `time_series` | trial start year (estimated start dates counted in each row's `estimated_count`); years without trials shown as zero; the current year noted as incomplete in the assumptions; the drug must be an intervention, not just mentioned | trend-01, trend-02, trend-05 |
| Q-02 | Trials started each year for a condition | `time_series` | start date; the API's condition search is trusted (it includes basket trials) | trend-03, trend-04 |
| Q-03 | A condition's trials across phases | `bar_chart` | a multi-phase trial counts under each of its phases (disclosed); "Not applicable" and missing phases keep their own bars | dist-01, dist-02 |
| Q-04 | Most common intervention types | `bar_chart` | distinct trials per type: a trial with two drugs counts once for "Drug" | dist-03, dist-04 |
| Q-05 | Phases for drug A vs drug B | `grouped_bar_chart` | groups "A only", "B only" and "Both", so no trial is counted twice | cmp-01, cmp-02; example 03 |
| Q-06 | Sponsor categories across two conditions | `grouped_bar_chart` | the lead sponsor's class (industry, NIH, other…); conditions as the sides, with an overlap group | cmp-03, cmp-04 |
| Q-07 | Countries with the most recruiting trials for a condition | `bar_chart` | "recruiting" is the trial's overall status; a trial counts once per country with a current site; top 10 + Other | geo-01, geo-02, series-02; example 02 |
| Q-08 | Sponsor ↔ drug network for a condition | `network_graph` (two columns) | lead sponsor; drug identity from MeSH terms of drug interventions; an edge = trials sharing both, at least 2; top 15 sponsors and 25 drugs | net-01, net-03; example 04 |
| Q-09 | Drugs that co-occur in combination studies | `network_graph` (circle) | a combination means the same arm, not just the same trial; alternatives within an arm ("A or B") are excluded using the arm text | net-02 |

Also supported: the enrollment `histogram` (actual vs estimated), enrollment vs duration `scatter_plot`, trial `timeline` and `table`, single counts, crossed charts ("phases per year"), questions that ask several things, follow-ups and corrections. Not supported, by design or by the data: investigator and site networks, geographic maps, efficacy or results questions (answered as `unsupported_query`), and cohorts over 20,000 trials (`scope_required`).

### Walkthrough: from a source record to a cited bar

Example 02, "Which countries have the most recruiting trials for melanoma?":

1. **Plan.** The model returns a typed plan: `aggregate` by `country`, filters `conditions: ["melanoma"]` and `statuses: ["RECRUITING"]`. It produces no numbers and no trial IDs.
2. **Retrieve.** Code compiles the filters into one ClinicalTrials.gov request, `GET /api/v2/studies?query.cond=(melanoma)&filter.overallStatus=RECRUITING&fields=…&pageSize=1000&countTotal=true`. It reports 480 matches and fetches all of them, so the cohort is complete; 480 trials remain after the match checks.
3. **Count.** Each trial is placed under every country where it has a current site, once per country. China gets 59 distinct trials.
4. **Datum.** `{"country": "China", "trial_count": 59, "trial_ids": ["NCT03340506", …]}`. `trial_count` always equals the number of `trial_ids`.
5. **Citation.** `evidence["NCT03340506"]` ("Dabrafenib and/or Trametinib Rollover Study") holds the source values behind it: `overallStatus = "RECRUITING"` (the status filter), `locations.country` including `"China"` (its bar), and condition MeSH terms including `"Melanoma"` (the condition search). In the web page, clicking the China bar lists all 59 trials with these values.
6. **Verify.** Before answering, the verifier recounts the bar from its citations, re-derives "China" and every quoted citation value from each cited trial's own record, and checks every cited trial meets both filters. All nine checks passed (`verification` in the response).

**A limitation, made visible:** example 05, "What phases are Merck's trials in?". Two different lead sponsors match "Merck". Rather than pick one, the service asks, offering options built from the data: Merck Sharp & Dohme (2,151 trials), Merck KGaA (275) or both.

---

## 6. Key design decisions and tradeoffs

| Decision | Why | Tradeoff |
|---|---|---|
| **Fixed workflow with two small model steps**, not an agent loop | Reading the question (splitting it, then planning each part) is the only judgement; everything else is deterministic. Bounded cost (usually 2 calls, at most 5), testable stages, no hallucinated numbers. | Less open-ended than a tool-using agent; new question types need a new operation in the catalog. |
| **The model never sees trial data** | Numbers, IDs and citations cannot be invented, and trial text cannot inject instructions. | The model cannot list real names in clarifications, so code builds the options from data. |
| **Live API with a page cache**; no local database copy | Answers reflect the registry's current data; the cache key includes the data timestamp. | A broad question costs many requests (~1.1 s per 1,000 trials); above 20 pages the service returns `scope_required`. |
| **Own visualization spec, Vega-Lite only as renderer** | A documented, renderer-independent contract; Vega-Lite never aggregates, so every displayed number stays tied to its citations. | We maintain the spec and its renderer, including network layout. |
| **Drug match check, condition search trusted** | "Trials of drug X" should give drug X: 11% of drug search matches only *mention* the drug. Condition search is intentionally broad (basket trials count). | Asymmetric rules, disclosed as assumptions with the excluded count. |
| **Drug identity from MeSH, matched to drug-type interventions** | Pembrolizumab appears under 503 spellings. Trial-level MeSH terms also cover procedures (radiotherapy, biopsy), found on live data. | Unmatched brand names fall back to cleaned raw names. |
| **Combination = same arm, not same trial; alternatives read from arm text** | 34% of same-trial drug pairs sit in different arms (drug vs comparator). Within an arm, the description separates options ("cisplatin … OR carboplatin", "EITHER … OR …", "investigator's choice of …") from combinations ("… PLUS …"). | Text rules, not understanding: arms whose description never names both drugs are taken as given together. On "drugs combined with pembrolizumab", carboplatin + cisplatin fell from 113 to 49 trials while real combinations stayed. Fetching arm descriptions makes these questions slower (~8 s vs ~4 s). |
| **Multi-phase trials count under each phase**; countries count trials, not sites | Matches the API's own phase filter, so counts reconcile (verified: local counts equal API totals). | Categories can sum to more than the total; flagged in metadata. |
| **Clarify only without a sensible default** | "Year" = start year and "sponsor" = lead sponsor are reported as assumptions instead of asked. | Users must read assumptions to see defaults. |
| **Follow-ups via `previous_run_id`** | No conversation memory: the earlier plan is loaded from the run record, and the response says whether it was refined or replaced. A refinement also keeps the earlier request's structured fields (such as a Clarification answer), reported as an assumption; a new topic starts clean. | One step back only; the client holds the conversation. |
| **Hosted mode by configuration** | One codebase: local files and in-process state by default; Redis, Postgres and API keys are switched on by settings, and `DEPLOYMENT=hosted` refuses to start without them. | Two implementations of each store to keep in step (both tested). Breakers and caches of process-local state stay per instance. |
| **pydantic-ai `FallbackModel`, tool-based output** | Swap models by configuration: OpenAI and Anthropic were run; Gemini is wired in but untested. Tool output avoids a known issue with native structured output inside fallbacks. | Fallback only on provider errors, so a weak plan from the primary is repaired, not re-asked elsewhere. |

---

## 7. Failure handling

Every failure ends in one Outcome with a structured `error` (`code`, `message`, `retryable`), is logged with the run ID, and never shows provider response bodies. A Question that asks several things fails part by part: one part's error leaves the others answered.

| Failure point | What happens | Outcome · `error.code` |
|---|---|---|
| **Primary planning model fails** (5xx, 429, timeout, network, bad key) | The fallback model plans instead; the answer carries a warning naming the failure ("gpt-5.4-mini: HTTP 401") and `planner_model` names the model that answered | `success` + warning |
| **Every model fails**, retryably | Stopped; nothing is fetched from ClinicalTrials.gov | `upstream_error` · `planner_unavailable` (retryable) |
| **Every model rejects the request** (401/403/404: bad key or model name) | Stopped with a configuration hint | `upstream_error` · `planner_rejected` (not retryable) |
| **A model hangs** | Each attempt is limited to `PLANNER_TIMEOUT_SECONDS` (20 s, one quick retry, instead of the SDKs' 600 s with two retries); the whole step to `PLANNER_DEADLINE_SECONDS` (60 s) | `upstream_error` · `planner_timeout` (retryable) |
| **The model returns no usable plan** (text instead of a plan, after one retry) | Stopped; not sent to the fallback, which is for provider failures only | `internal_error` · `planner_invalid_output` (retryable) |
| **No model configured** | The service still starts; every question explains which key is missing | `internal_error` · `planner_not_configured` |
| **ClinicalTrials.gov** fails | Retries with backoff (honouring `Retry-After`), then stops with the reason | `upstream_error` · `source_unavailable` / `source_rate_limited` / `source_rejected` / `source_invalid_response` |
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

## 8. Limitations and what I would improve with more time

- **Scope cap:** questions matching more than 20,000 trials return `scope_required`. A background job (`POST /runs` → `GET /runs/{id}`) or the API's own count endpoint for simple totals would lift it.
- **Rate limit:** ClinicalTrials.gov returned HTTP 429 during development. The client honours `Retry-After` and backs off, and the limiter allows 40 requests per minute: per process locally, shared by all instances through Redis when hosted.
- **Drug classes** ("PD-1 inhibitors") are handled by a clarification listing the drugs most often found in matching trials. There is no verified class membership; the registry has none.
- **Data quality is passed through, not corrected.** Enrollment outliers (one melanoma record lists 2,953,748 participants, another 999,999) are shown as recorded. Alternatives listed in an arm are detected from its description; when the description does not name both drugs, they still count as given together.
- **Run records** keep the plan and response only. Full run bundles with the raw API pages, for exact offline replay, are designed but not built.
- **The planner eval** has 46 questions, including multi-part and crossed ones: `gpt-5.4-mini` scored 46/46 and 45/46 in two runs with the split step (the miss wrote "UK" for the country; the gate maps it to United Kingdom, so the answer is unaffected; the second run is in `evals/results/`); `claude-haiku-4-5` scored 95% (40/42) on the earlier 42-question set, before the split step (results in [`evals/results/`](evals/results/)). A larger held-out set and adversarial phrasings would make it stronger. Repeated runs show residual variance: the "industry vs academic … Parkinson's and ALS" comparison sometimes omits the sponsor-category breakdown (4 of 5 runs correct).
- **Hosting:** deployed on AWS (us-east-2) and checked live with the smoke test: health, a question, a follow-up from the Postgres run history, and a chart. The first deployment exposed a startup that waited a minute on an unreachable database; it now connects in the background. Circuit breakers are per instance. The table is created on startup; a migration tool such as Alembic would come with the first schema change. Traces are not yet sent anywhere on AWS: X-Ray needs an OpenTelemetry collector next to the app.
- **Time cap:** the 30 s run deadline applies when hosted; locally there is none. Questions near the page cap can exceed 30 s; a background job (`POST /runs`) would be the next step if traces show deadline hits.
- **Not built:** investigator and site networks.

---

## 9. How correctness was validated

- **API spike before design.** Every filter was checked against the live API, and local counts reproduce the API's own totals exactly: start year 2020 = 263, Phase 3 = 367, Germany = 326, recruiting = 712. Findings and data-quality measurements are in [`docs/research/api-data-guide.md`](docs/research/api-data-guide.md).
- **197 offline tests** run through the HTTP API, with a scripted planner and ClinicalTrials.gov mocked by real records saved from the API. They cover:
  - every chart type, clarifications, follow-ups, repair
  - `scope_required`, `no_data`, upstream errors, rate-limit retries
  - every failure point in §7 (23 tests): fallback to the second model with a warning, every model failing or rejecting, a hanging model, a model returning text instead of a plan, ClinicalTrials.gov errors by status, a chart that cannot compile or draw, an unsaved run record, and a bug confined to one part of a multi-part Question
  - the hosted mode (36 tests, with an in-memory Redis and SQLite in place of Postgres): settings that refuse to start or leak secrets, run history shared through SQL, one request budget and page cache across two instances, Idempotency-Keys across instances, a Redis outage, API keys and hourly limits (per user, and Idempotency-Keys scoped per user), open access, circuit breakers opening and closing, the run deadline keeping finished parts and covering a slow run store
  - counting rules (multi-phase, distinct trials per country, no year gaps, top-N + Other with "Not reported" kept separate, missing values as their own state, the drug filter keeping only drug-type interventions), with property tests showing input order and duplicates do not change counts
  - **tamper tests** proving the verifier rejects a changed count, a trial moved to the wrong bar or bin, a chart grouped by the wrong field, a count that leaves out part of the cohort, a multi-phase trial missing from one of its phase bars, a trial wrongly counted in "Other", an altered citation value or link, a trial cited for a network edge it lacks, an edge without a shared arm, and a trial outside the filters
- **Live tests** against ClinicalTrials.gov (`pytest -m live`), and every answer type run end to end with the real models, with the images inspected (tables have none).
- **Planner eval** (`evals/`): 46 questions modelled on the assignment's appendix, scored per question family per model.
- **An external code review** by a second model, every finding checked against the code. Ten were real and are fixed, each with a regression test that failed first. Among them: the drug filter kept trials that gave the drug only as a device or tracer; fallback attempts escaped the 3-call limit; the verifier missed a wrong grouping and altered citations; "Not reported" was folded into "Other"; and picking your own filter's value in a conflict clarification asked again forever.
- **Iteration driven by real data.** Each of these was found by running the real service, then fixed and covered by a test:
  - procedures counted as drugs (MeSH terms span all interventions) → drug identity matched to drug-type interventions
  - HTTP 429 → `Retry-After` handling
  - unreadable network images → 60-link cap
  - alternatives in one arm ("cisplatin OR carboplatin") counted as combinations → detected from the arm description; checked on the real KEYNOTE-189 record and on real "either / investigator's choice" arm texts, including a false positive the tests caught (a dose unit "mg/m²" read as "or")
  - incomplete current-year counts → assumption
  - shallow citations → per-filter source values with a verifier check
  - multi-part questions: one unrelated question silently merged with another into a wrong single number, and second requests were dropped. Asking the model to produce a multi-part plan was unreliable (about 50% across repeated runs, and its repair turn confused it), so code split the message and the model planned each part as a normal question. A later probe of nine phrasings found the code splitter still missing some (". Also, …", "… plus …", "1) … 2) …", "show their phases"), three of them dropping a part silently. A dedicated split call, which only lists the questions and rewrites each to stand alone, now handles all nine, and 35 of 35 repeated runs, with comparisons kept whole. The eval gained four multi-part cases
  - a refinement in the web page silently dropped the user's Clarification answer (the exact Merck companies) → refining Follow-ups now inherit the earlier structured fields
  - the first AWS deployment ([details](docs/hosted-deployment.md#first-deployment-what-went-wrong-and-the-fixes)):
    - it was rolled back because new service roles were used in the same second they were created; the fix was to create them in setup
    - every task waited a minute on the database's closed firewall and was replaced → the table is now created in the background, with second-scale connection timeouts (regression tests added)
    - keys pasted through a notes app were refused → case, quotes and invisible characters are ignored
  - network charts in dark mode had black labels and a merged, oversized legend → theme-aware labels and separate legends, checked for every chart type in both themes

---

## 10. Tools used, and what was designed vs generated

**Tools:** Claude Code as the coding assistant; pydantic-ai with an OpenAI model (primary) and an Anthropic model (fallback) for the planner (Gemini can be configured but was never run); Vega-Lite (`vl-convert`) for charts; uv, ruff, pyright, pytest; Docker and the AWS CLI for the hosted version.

**How we worked:** I directed the work. I chose what to research and set the architecture; the assistant researched what I asked and laid out the trade-offs for each open question. I made the decisions, and they are recorded in [`docs/harness-design.md`](docs/harness-design.md) and [`docs/hosted-deployment.md`](docs/hosted-deployment.md). The assistant then wrote the code, tests and docs to that architecture.

**My decisions:**
- **Scope:** the sample questions are examples; the service must also handle follow-ups, corrections and several questions in one request.
- **Filters and clarifications:** filters come from the question; structured fields are optional overrides; ask a multiple-choice question only when the request is unclear.
- **Data:** the live API only, with no local copy, and citations as trial IDs on every data point.
- **Charts:** our own spec is the contract. Counting stays in our code and Vega-Lite only draws, so no library does the analysis. Images by default, plus an interactive page.
- **Networks:** modelled on a gene-network viewer I used as a reference; drugs, sponsors and conditions, because the source has no gene data.
- **Models:** OpenAI first, Anthropic as fallback, Gemini as an untested option, all changed by configuration; the allowed OpenAI model list.
- **Left out:** MCP, Neo4j, CI and full run bundles.
- **Hosting:** OpenTelemetry without a vendor; a working local version before any hosting; AWS for the hosted version.
- **Web page and failures:** follow-ups added below as a thread; light and dark themes; explicit handling of model and chart failures.

**Generated by the assistant, then checked:**
- Most of the code, tests and documentation, plus the eval questions' expected plans, which I reviewed.
- I ran the outputs and charts; problems I found (unreadable charts, follow-ups replacing answers, pasted keys rejected) went back as fixes.
- Data problems found on live data (procedures counted as drugs, rate limits) are handled in code, at my request.
- I ran the AWS deployment myself, step by step. The assistant wrote the scripts and diagnosed the first deployment's failures.
- A second model reviewed the finished code. Each finding was checked against the code and logged; the confirmed bugs were fixed, each with a regression test that failed first.
