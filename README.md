# Clinical Trials Question-to-Visualization

A backend service that answers natural-language questions about clinical trials with a **frontend-renderable visualization specification**. Every number in it is computed from the [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api), and every bar, point, node and edge is **cited** down to the trials and source fields behind it.

```
POST /v1/query  {"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab"}
→ time_series of trials per start year · 2,629 cited trials · verified · chart at /v1/runs/{id}/chart.png
```

Supported answers:
- trends: `time_series`
- distributions and geography: `bar_chart`
- comparisons: `grouped_bar_chart`, with an overlap group
- single counts: `single_value`
- trial lists: `table`
- trial timelines: `timeline`
- enrollment vs duration: `scatter_plot`
- enrollment distributions: `histogram`
- sponsor ↔ drug networks and same-arm drug ↔ drug combination networks: `network_graph`

The service asks a multiple-choice clarification when a question is genuinely ambiguous, and refuses questions the registry cannot answer.

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
- **Follow-ups:** under each answer, "Ask a follow-up about this answer" takes a short change ("only phase 3", "show it by start year instead"; one-click examples are offered) and sends it with the answer's `previous_run_id`. A trail above the answer lists the conversation, says whether each follow-up refined the previous answer or was treated as a new question, and reopens any earlier answer. "Ask a new question" starts over.
- Failures show their error code, with a Retry button when retrying may help; if the chart library cannot load, the chart's data is shown as a clickable table instead.
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
uv run python -m evals.run       # score the configured planner on 42 questions (needs an API key)
uv run ruff check src tests && uv run pyright
```

**Hosted version.** The same service in a container on Google Cloud Run, with Upstash Redis and Neon Postgres, as decided in [`docs/hosted-deployment.md`](docs/hosted-deployment.md). `DEPLOYMENT=hosted` switches on:
- **Shared state in Redis** (`REDIS_URL`): one ClinicalTrials.gov request budget for all instances (the registry's limit is per IP), the page cache, Idempotency-Keys and per-user counts. If Redis fails, each falls back to working per instance.
- **Run history in Postgres** (`DATABASE_URL`): any instance can serve a Follow-up or a chart. Each row records the user, outcome, model calls and latency.
- **API keys** (`API_KEYS`, `name:key` pairs): `POST /v1/query` needs an `X-API-Key` header, and each user may ask 30 questions an hour (`USER_QUERIES_PER_HOUR`). Reading runs and charts stays open (run IDs are random). The web page asks for the key once.
- **Circuit breakers** for ClinicalTrials.gov and each model, and a **30 s run deadline** (`RUN_DEADLINE_SECONDS`).
- **Traces** to Langfuse over OpenTelemetry (optional).

```bash
API_KEYS=you:a-long-random-key docker compose up --build   # the hosted mode on this machine, with Redis and Postgres
PROJECT=my-gcp-project deploy/cloud-run.sh                  # deploy to Cloud Run; secrets go to Secret Manager
deploy/smoke-test.sh https://YOUR-SERVICE-URL               # health, a question, a follow-up and a chart
```

The hosted mode refuses to start without Redis, Postgres and API keys. Deploying needs your own accounts: Google Cloud with billing and the `gcloud` CLI, Upstash, Neon, and optionally Langfuse (all have free tiers). Every question costs 1–3 model calls; the per-user limit caps that.

---

## 2. How it works

```
question ─► PLAN (the only model step) ─► GATE ─► RETRIEVE ─► COUNT ─► BUILD SPEC ─► VERIFY ─► response
              typed Query Plan        validate,   all pages,   code   chart chosen   gate on      + run record
              (or clarify/refuse)     repair once  match check  only   by code        every answer
```

- **One model step.** A pydantic-ai agent turns the question (plus any structured fields, plus the previous plan for follow-ups) into a typed **Query Plan**: filters, operation (`aggregate`, `compare`, `per_trial`, `bin`, `relate`), what to group by (optionally with a second **series** dimension, "phases per year"), and a view or network kind. It may instead return a clarification request or a refusal. The model **never sees trial records** and **never outputs numbers, trial IDs or citations**.
- **Questions that ask several things are split by code, not by the model.** A plain-code detector splits the message where a new request starts ("…, and what…", "…; show…", "… and also by …"). Each **part** is planned on its own as an ordinary single question, with the full message as context only for references. The model never sees a multi-part shape, so its job doesn't change. Each part gets its own filters, chart, citations and verification, and fails or asks independently. Parts are planned one after another and share the 3-call budget (at most 3 parts).
- **Code does everything else:**
  - compiles filters into API requests
  - retrieves **every** page, or refuses with `scope_required` and never samples
  - applies match checks
  - counts distinct trials
  - chooses the chart type deterministically from the plan
  - builds the spec and evidence
  - verifies
- **Bounded:** at most 3 model calls per run (plan, one repair with the validator's errors, one provider fallback on transport errors only). Every run ends in exactly one **outcome**: `success`, `no_data`, `clarification_required`, `unsupported_query`, `scope_required`, `upstream_error`, `internal_error`.
- **Verifier**, a gate before every successful response. It checks that:
  - the chart answers the plan
  - every encoded field exists
  - each count equals its distinct cited trials
  - every citation resolves
  - each cited trial really has its bucket's value, re-derived from its own record
  - each cited trial meets every filter
  - network edges join existing nodes, and every cited trial has both ends (or a shared arm, for combinations)
  - time series have no gaps

  A failed check withholds the answer.

Code map (`src/clinical_trials_viz/`):

| Module | Role |
|---|---|
| `pipeline.py` | The fixed workflow and outcomes |
| `planner.py` | The model step |
| `validate.py` | Merge structured fields; semantic gate |
| `cohort.py` | Compile filters, retrieve, drug match check |
| `analyze.py`, `network.py` | All counting |
| `spec_builder.py` | Specification and evidence |
| `verify.py` | The gate |
| `render.py` | Spec → Vega-Lite → PNG/SVG |
| `clarify.py` | Clarification options built from data |
| `catalog.py` | The versioned capability catalog: dimensions, enums, counting constants |
| `ctgov/` | API client and the typed trial record |
| `api.py`, `cli.py` | HTTP API and command line |

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

If a structured field and the question name different values for the same filter, the service asks rather than picking one.

**Idempotent retries.** Send an optional `Idempotency-Key` header (up to 255 characters) to make retries safe:

| Case | Response |
|---|---|
| Same key, same request | The original response, unchanged: same `run_id`, no model call, no API requests, header `Idempotent-Replayed: true` |
| Same key, different request | **422** |
| Same key while the first request is still running | **409** (retry shortly) |
| Key older than 24 hours | Treated as new |

**API key (hosted).** When the service is configured with `API_KEYS`, send `X-API-Key: <your key>`. A missing or wrong key gets **401** (`unauthorized`); more questions than the hourly limit get **429** (`rate_limited`) with a `Retry-After` header. Idempotent replays do not count against the limit.

Request equality is judged on the validated request, so whitespace and field order do not matter. A request that fails before producing a run (e.g. unknown `previous_run_id`) does not consume its key. Without the header, every POST is a new run. The JSON Schemas for request and response are served at `GET /v1/schema`.

Other endpoints:
- `GET /v1/runs/{run_id}`: the saved run record (request, plan, response)
- `GET /v1/runs/{run_id}/chart.png` and `.svg` (`?part=N` for part N+1 of a multi-part question): the rendered chart
- `GET /v1/runs/{run_id}/vega-lite.json` (`?part=N`): the chart as Vega-Lite with finished values; each mark carries `_datum`, its index in the spec's Datums (rows, or nodes then edges), so a client can show the Citation for whatever is clicked
- `GET /`: the web page
- `GET /health`

---

## 4. Response schema

All outcomes return HTTP 200 with the outcome in the body.

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
| `assumptions` | Defaults applied and data caveats (e.g. "9 of 126 search matches were excluded because 'Keytruda' is not one of their interventions") |
| `applied_filters` | The filters actually applied after merging structured fields; `from_request` names those pinned by the caller |
| `clarification` | On `clarification_required`: `{field, question, options:[{label, value, trial_count}], multi_select, allow_free_text}`. Send the chosen `value` back in `field` with `previous_run_id`. |
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

The images are produced by translating this spec, and only this spec, into Vega-Lite with finished values. That doubles as a check that the spec is complete.

---

## 5. Example runs

[`examples/`](examples/) holds five real runs from the live service, unedited: request, full JSON response and chart.

| Example | Outcome |
|---|---|
| The assignment's own request ("this drug" + `drug_name: Pembrolizumab`) | `time_series`, 2,629 cited trials |
| "Which countries have the most recruiting trials for melanoma?" | `bar_chart`, 480 |
| "Compare phases for trials involving semaglutide vs tirzepatide" | `grouped_bar_chart`, 832 |
| "Show a network of sponsors and drugs for glioblastoma trials" | `network_graph`, 975 |
| "What phases are Merck's trials in?" | `clarification_required`: Merck Sharp & Dohme (2,151) / Merck KGaA (275) / All of these (2,426) |

Regenerate with `uv run python -m examples.generate`.

---

## 6. Key design decisions and tradeoffs

| Decision | Why | Tradeoff |
|---|---|---|
| **Fixed workflow with one model step**, not an agent loop | Planning is the only judgement call; everything else is deterministic. Bounded cost (≤ 3 calls), testable stages, no hallucinated numbers. | Less open-ended than a tool-using agent; new question types need a new operation in the catalog. |
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
| **pydantic-ai `FallbackModel`, tool-based output** | Swap OpenAI, Anthropic or Gemini by configuration. Tool output avoids a known issue with native structured output inside fallbacks. | Fallback only on provider errors, so a weak plan from the primary is repaired, not re-asked elsewhere. |

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
- **The planner eval** has 42 questions, including multi-part and crossed ones: `gpt-5.4-mini` scores 100% (42/42), `claude-haiku-4-5` 95% (40/42) (results in [`evals/results/`](evals/results/)). A larger held-out set and adversarial phrasings would make it stronger. Repeated runs show residual variance: the "industry vs academic … Parkinson's and ALS" comparison sometimes omits the sponsor-category breakdown (4 of 5 runs correct).
- **Hosting:** the hosted version is built and was tested on this machine with Docker (compose: service, Redis, Postgres; health, a question, a follow-up and a chart, 401 without a key), but it has not been deployed to Cloud Run from this repository: that needs your cloud accounts. Circuit breakers are per instance; the table is created on startup (a migration tool such as Alembic would come with the first schema change).
- **Time cap:** the 30 s run deadline applies when hosted; locally there is none. Questions near the page cap can exceed 30 s; a background job (`POST /runs`) would be the next step if traces show deadline hits.
- **Not built:** investigator and site networks.

---

## 9. How correctness was validated

- **API spike before design.** Every filter was checked against the live API, and local counts reproduce the API's own totals exactly: start year 2020 = 263, Phase 3 = 367, Germany = 326, recruiting = 712. Findings and data-quality measurements are in [`docs/research/api-data-guide.md`](docs/research/api-data-guide.md).
- **161 offline tests** run through the HTTP API, with a scripted planner and ClinicalTrials.gov mocked by real records saved from the API. They cover:
  - every chart type, clarifications, follow-ups, repair
  - `scope_required`, `no_data`, upstream errors, rate-limit retries
  - every failure point in §7 (21 tests): fallback to the second model with a warning, every model failing or rejecting, a hanging model, a model returning text instead of a plan, ClinicalTrials.gov errors by status, a chart that cannot compile or draw, an unsaved run record, and a bug confined to one part of a multi-part Question
  - the hosted mode (26 tests, with an in-memory Redis and SQLite in place of Postgres): settings that refuse to start or leak secrets, run history shared through SQL, one request budget and page cache across two instances, Idempotency-Keys across instances, a Redis outage, API keys and hourly limits, circuit breakers opening and closing, and the run deadline keeping finished parts
  - counting rules (multi-phase, distinct trials per country, no year gaps, top-N + Other, missing values as their own state), with property tests showing input order and duplicates do not change counts
  - **tamper tests** proving the verifier rejects a changed count, a trial moved to the wrong bar or bin, a trial cited for a network edge it lacks, an edge without a shared arm, and a trial outside the filters
- **Live tests** against ClinicalTrials.gov (`pytest -m live`), and every answer type run end to end with the real models, with the images inspected (tables have none).
- **Planner eval** (`evals/`): 42 questions modelled on the assignment's appendix, scored per question family per model.
- **Iteration driven by real data.** Each of these was found by running the real service, then fixed and covered by a test:
  - procedures counted as drugs (MeSH terms span all interventions) → drug identity matched to drug-type interventions
  - HTTP 429 → `Retry-After` handling
  - unreadable network images → 60-link cap
  - alternatives in one arm ("cisplatin OR carboplatin") counted as combinations → detected from the arm description; checked on the real KEYNOTE-189 record and on real "either / investigator's choice" arm texts, including a false positive the tests caught (a dose unit "mg/m²" read as "or")
  - incomplete current-year counts → assumption
  - shallow citations → per-filter source values with a verifier check
  - multi-part questions: one unrelated question silently merged with another into a wrong single number, and second requests were dropped. Asking the model to produce a multi-part plan was unreliable (about 50% across repeated runs, and its repair turn confused it), so code now splits the message and the model plans each part as a normal question: 20/20 across repeated runs, with the eval otherwise unchanged
  - a refinement in the web page silently dropped the user's Clarification answer (the exact Merck companies) → refining Follow-ups now inherit the earlier structured fields

---

## 10. Tools used, and what was designed vs generated

- **Tools:**
  - [Claude Code](https://claude.com/claude-code) (Anthropic) as the coding assistant: research, API exploration, implementation, tests and documentation
  - pydantic-ai with OpenAI and Anthropic models as the service's planner
  - Vega-Lite via `vl-convert` for rendering
  - uv, ruff, pyright, pytest, respx, hypothesis, fakeredis; Docker for the container
- **Designed deliberately by the author**, decided in design reviews before and during implementation (recorded in [`docs/harness-design.md`](docs/harness-design.md)):
  - the one-model-step workflow and its limits
  - the live-API decision
  - counting rules: all study types count as trials, conditions trust the API search, comparison overlap groups, network size limits
  - clarification as data-backed multiple choice, and follow-ups via `previous_run_id`
  - our own spec as the contract with Vega-Lite only as renderer
  - OpenAI primary with Anthropic fallback and Gemini as an option
  - the request fields
  - the order of work
- **Generated with the assistant, then reviewed and adapted:**
  - most of the code, tests and documentation, written by Claude Code against those decisions
  - outputs and charts were inspected after each feature, and several defects found that way were fixed (section 9)
  - the eval set's expected plans were drafted by the assistant for the author's review
