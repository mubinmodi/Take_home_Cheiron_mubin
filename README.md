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
uv run clinical-trials-viz serve          # API on http://127.0.0.1:8000, interactive docs at /docs
```

Ask from the command line (in-process; prints a summary and saves the chart under `data/charts/`):

```bash
uv run clinical-trials-viz ask "Which countries have the most recruiting trials for melanoma?"
uv run clinical-trials-viz ask "How has the number of trials for this drug changed over time?" --fields '{"drug_name": "Pembrolizumab"}'
uv run clinical-trials-viz ask "What phases are Merck's trials in?"          # → clarification with options
uv run clinical-trials-viz ask "What phases are Merck's trials in?" --previous RUN_ID --fields '{"sponsor": ["Merck Sharp & Dohme LLC"]}'
uv run clinical-trials-viz ask "..." --json                                   # full JSON response
```

**Configuration.** All settings are environment variables, documented in [`.env.example`](.env.example):
- planner models: primary and fallback, as `provider:model` strings
- ClinicalTrials.gov base URL, rate limit, timeout and page cap
- run-record directory
- public base URL used in `chart_url`
- OpenTelemetry exporter (`none`, `console` or `otlp`)

Models change by configuration only. Default: `openai:gpt-5.4-mini`, with `anthropic:claude-haiku-4-5` as fallback.

**Checks.**

```bash
uv run pytest                    # 95 offline tests (ClinicalTrials.gov mocked with saved real records)
uv run pytest -m live            # tests against the live ClinicalTrials.gov API
uv run python -m evals.run       # score the configured planner on 36 questions (needs an API key)
uv run ruff check src tests && uv run pyright
```

---

## 2. How it works

```
question ─► PLAN (the only model step) ─► GATE ─► RETRIEVE ─► COUNT ─► BUILD SPEC ─► VERIFY ─► response
              typed Query Plan        validate,   all pages,   code   chart chosen   gate on      + run record
              (or clarify/refuse)     repair once  match check  only   by code        every answer
```

- **One model step.** A pydantic-ai agent turns the question (plus any structured fields, plus the previous plan for follow-ups) into a typed **Query Plan**: filters, operation (`aggregate`, `compare`, `per_trial`, `bin`, `relate`), what to group by, and a view or network kind. It may instead return a clarification request or a refusal. The model **never sees trial records** and **never outputs numbers, trial IDs or citations**.
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

If a structured field and the question name different values for the same filter, the service asks rather than picking one. The JSON Schemas for request and response are served at `GET /v1/schema`.

Other endpoints:
- `GET /v1/runs/{run_id}`: the saved run record (request, plan, response)
- `GET /v1/runs/{run_id}/chart.png` and `.svg`: the rendered chart
- `GET /health`

---

## 4. Response schema

All outcomes return HTTP 200 with the outcome in the body.

| Field | Meaning |
|---|---|
| `run_id`, `outcome`, `message` | Identity and result; `message` explains non-success outcomes |
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
| `time_series` | one row per start year, no gaps, plus `estimated_count` | `x` year (`time_granularity: "year"`), `y` count. A `Not reported` row holds undated trials and is not plotted on the axis. |
| `grouped_bar_chart` | one row per category × comparison group (`"A only"`, `"B only"`, `"Both"`/`"More than one"`) | `x`, `y`, `color` = group |
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
| **Follow-ups via `previous_run_id`** | No conversation memory: the earlier plan is loaded from the run record, and the response says whether it was refined or replaced. | One step back only; the client holds the conversation. |
| **pydantic-ai `FallbackModel`, tool-based output** | Swap OpenAI, Anthropic or Gemini by configuration. Tool output avoids a known issue with native structured output inside fallbacks. | Fallback only on provider errors, so a weak plan from the primary is repaired, not re-asked elsewhere. |

---

## 7. Limitations and what I would improve with more time

- **Scope cap:** questions matching more than 20,000 trials return `scope_required`. A background job (`POST /runs` → `GET /runs/{id}`) or the API's own count endpoint for simple totals would lift it.
- **Rate limit:** ClinicalTrials.gov returned HTTP 429 during development. The client honours `Retry-After` and backs off, and the limiter allows 40 requests per minute per process. Several processes, or a hosted service, would need a shared limiter (e.g. Redis).
- **Drug classes** ("PD-1 inhibitors") are handled by a clarification listing the drugs most often found in matching trials. There is no verified class membership; the registry has none.
- **Data quality is passed through, not corrected.** Enrollment outliers (one melanoma record lists 2,953,748 participants, another 999,999) are shown as recorded. Alternatives listed in an arm are detected from its description; when the description does not name both drugs, they still count as given together.
- **Run records** keep the plan and response only. Full run bundles with the raw API pages, for exact offline replay, are designed but not built.
- **The planner eval** has 36 questions: `gpt-5.4-mini` scores 97%, `claude-haiku-4-5` 92% (results in [`evals/results/`](evals/results/)). A larger held-out set and adversarial phrasings would make it stronger. The one shared miss ("industry vs academic … Parkinson's and ALS") shows that questions naming two comparison axes need a clearer rule.
- **Not built:**
  - an interactive frontend (click a bar or edge to see its trials; the spec and evidence already support it)
  - hosting
  - investigator and site networks
  - a hard per-run time cap

---

## 8. How correctness was validated

- **API spike before design.** Every filter was checked against the live API, and local counts reproduce the API's own totals exactly: start year 2020 = 263, Phase 3 = 367, Germany = 326, recruiting = 712. Findings and data-quality measurements are in [`docs/research/api-data-guide.md`](docs/research/api-data-guide.md).
- **95 offline tests** run through the HTTP API, with a scripted planner and ClinicalTrials.gov mocked by real records saved from the API. They cover:
  - every chart type, clarifications, follow-ups, repair
  - `scope_required`, `no_data`, upstream errors, rate-limit retries
  - counting rules (multi-phase, distinct trials per country, no year gaps, top-N + Other, missing values as their own state), with property tests showing input order and duplicates do not change counts
  - **tamper tests** proving the verifier rejects a changed count, a trial moved to the wrong bar or bin, a trial cited for a network edge it lacks, an edge without a shared arm, and a trial outside the filters
- **Live tests** against ClinicalTrials.gov (`pytest -m live`), and every answer type run end to end with the real models, with the images inspected (tables have none).
- **Planner eval** (`evals/`): 36 questions modelled on the assignment's appendix, scored per question family per model.
- **Iteration driven by real data.** Each of these was found by running the real service, then fixed and covered by a test:
  - procedures counted as drugs (MeSH terms span all interventions) → drug identity matched to drug-type interventions
  - HTTP 429 → `Retry-After` handling
  - unreadable network images → 60-link cap
  - alternatives in one arm ("cisplatin OR carboplatin") counted as combinations → detected from the arm description; checked on the real KEYNOTE-189 record and on real "either / investigator's choice" arm texts, including a false positive the tests caught (a dose unit "mg/m²" read as "or")
  - incomplete current-year counts → assumption
  - shallow citations → per-filter source values with a verifier check

---

## 9. Tools used, and what was designed vs generated

- **Tools:**
  - [Claude Code](https://claude.com/claude-code) (Anthropic) as the coding assistant: research, API exploration, implementation, tests and documentation
  - pydantic-ai with OpenAI and Anthropic models as the service's planner
  - Vega-Lite via `vl-convert` for rendering
  - uv, ruff, pyright, pytest, respx, hypothesis
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
  - outputs and charts were inspected after each feature, and several defects found that way were fixed (section 8)
  - the eval set's expected plans were drafted by the assistant for the author's review
