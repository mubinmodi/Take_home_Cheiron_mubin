# A guided tour of the project

This guide explains the whole project to someone seeing it for the first time: what it does, how one question travels through the code, what each module is for, and where to make common changes. The [README](../README.md) is the reference (schemas, decisions, limitations); this is the walk-through. Domain words in **bold** are defined in the glossary, [`CONTEXT.md`](../CONTEXT.md).

## 1. What it does

You ask a question about clinical trials in plain English:

> "Which countries have the most recruiting trials for melanoma?"

The service answers with a **Visualization Specification**: a chart described as data (its type, title, encoding and rows), ready for any frontend to draw. Every number in it is counted from the live [ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/api), and every bar, point, node and link lists the trials behind it, so each number can be checked against the source records (a **Citation**).

It also draws the chart as a PNG or SVG, and serves a small web page where you can ask questions and click any bar to see its trials.

## 2. The one idea that shapes everything

**A language model reads the question; plain code does everything else.**

- The model has two small jobs:
  1. split a message into the separate questions it asks;
  2. turn each question into a typed **Query Plan**: which trials, counted how, grouped by what.
- Code does the rest: it fetches every matching trial, counts them, picks the chart, builds the specification with its citations, and checks the result.
- The model never sees a trial record, and never writes a number, a trial ID or a citation. So it cannot invent one.
- A **verifier** re-derives every count and every cited value from the trial records before any answer is returned. If a check fails, the answer is withheld.

Because the steps are fixed (not an agent choosing its own tools), each one is an ordinary function that can be tested on its own, and the cost is bounded: usually 2 model calls per question, never more than 5.

## 3. Follow one question through the code

A request arrives at `POST /v1/query` in `api.py`, which hands it to `Pipeline.run` in `pipeline.py`. From there:

```
1. LOAD      pipeline._load_previous     a Follow-up's earlier Run, if any (its plan and request)
2. SPLIT     planning._split             the model lists the separate questions (planner.split)
3. PLAN      planning._plan_single       the model turns each question into a Query Plan (planner.plan)
             or planning._plan_parts     ... one part at a time for a multi-part message
4. GATE      validate.check_plan         merge the structured fields, check the plan; repair once if needed
5. CONFLICTS conflicts.resolve           a structured field that contradicts the question is asked about
6. RETRIEVE  cohort.fetch_cohort         every matching trial from the API, plus the drug match check
7. ANSWER    answer.answer               count, build the chart and its Evidence, verify
8. RESPOND   pipeline._finish_answer     the Outcome, the chart link; runs.save keeps the Run Record
```

What each step does, with the melanoma question as the example:

1. **Load.** A new question has no earlier Run, so nothing happens. For a Follow-up ("only phase 3") the earlier Query Plan is loaded from its **Run Record**.
2. **Split.** One model call confirms this is a single question. A message like "Show the phases of melanoma trials. Also, who are the top sponsors?" would come back as two standalone questions, each planned and answered on its own (each a **Part**).
3. **Plan.** The model returns a Query Plan: operation `aggregate`, group by `country`, Filters `conditions: ["melanoma"]` and `statuses: ["RECRUITING"]`.
4. **Gate.** `validate.check_plan` merges any structured fields the caller sent (`AppliedFilters.merge` in `filters.py`) and checks the plan against the **Capability Catalog** in `catalog.py` (known operations, Dimensions, countries). A plan with fixable errors goes back to the model once, with the errors.
5. **Conflicts.** Had the caller also sent `country: "France"`, the question and the field would disagree. `conflicts.resolve` would then return a **Clarification** offering both values, rather than picking one. Here there is nothing to resolve.
6. **Retrieve.** `cohort.fetch_cohort` turns the Filters into API parameters (`AppliedFilters.api_params`) and fetches **every** page: 485 trials. Above 20,000 trials it stops with `scope_required` rather than counting a sample.
7. **Answer.** `answer.answer` does the analysis in one place:
   - `analyze.breakdown` counts the distinct trials in each country;
   - `spec_builder.breakdown_spec` builds the bar chart (the top 10 countries, then "Other");
   - `spec_builder.build_evidence` records, for each cited trial, the source values that placed it there;
   - `verify.verify` re-derives all of it.

   China gets 59 trials, and each of them really lists a site in China.
8. **Respond.** The pipeline sets the **Outcome** (`success`), adds the chart link, and saves the Run so a Follow-up or the chart image can find it later.

The response holds the plan, the applied Filters, the chart, the Evidence, the Assumptions (the defaults the service applied, e.g. "a trial counts once per country"), the checks and timings.

## 4. The modules

All code is in `src/clinical_trials_viz/`. Grouped by job:

| Job | Modules | In one line |
|---|---|---|
| **Entry points** | `api.py`, `cli.py`, `web/index.html` | The HTTP API, the `serve` and `ask` commands, the web page |
| **The workflow** | `pipeline.py` | Runs the fixed steps for each Run and owns its Outcome |
| **The model steps** | `planning.py`, `planner.py`, `ledger.py` | Split and plan (with fallback to a second model); the ledger counts every model call and enforces the limits |
| **Reading the question** | `validate.py`, `filters.py`, `conflicts.py`, `clarify.py` | The semantic gate; everything one Filter means; the Conflict rule; Clarification options built from data |
| **Getting the data** | `cohort.py`, `ctgov/client.py`, `ctgov/trial.py` | Complete retrieval and the drug match check; the API client (paging, rate limit, retries, cache); the typed trial record |
| **Answering** | `answer.py`, `analyze.py`, `network.py`, `spec_builder.py`, `verify.py` | From a Query Plan and its Cohort to a verified answer; counting; networks; the chart and its Evidence; the verifier |
| **When there is no answer** | `suggest.py`, `failures.py` | Corrections counted live for an empty result; every failure mapped to an Outcome and a structured error |
| **Drawing** | `render.py` | The specification translated into Vega-Lite, and drawn as PNG/SVG |
| **Rules** | `catalog.py` | The versioned Capability Catalog: operations, Dimensions, limits, the fields a request can pin |
| **Contracts** | `models/request.py`, `models/plan.py`, `models/response.py`, `models/spec.py` | The request, the Query Plan, the response, the Visualization Specification |
| **Keeping Runs** | `runs.py`, `idempotency.py` | Run Records (files locally, Postgres when hosted); safe retries with an `Idempotency-Key` |
| **Hosted mode** | `access.py`, `shared_state.py`, `breaker.py`, `config.py`, `telemetry.py` | API keys and limits; state shared through Redis; circuit breakers; settings; OpenTelemetry tracing |

Outside the package:
- `tests/`: the offline tests, which mock the API with saved real records;
- `evals/`: 46 questions for scoring the planner;
- `examples/`: real runs with their full JSON and charts;
- `deploy/`: the AWS scripts.

## 5. How the trickier parts work

**Clarifications.** The service asks only when there is no sensible default, and always offers choices built by code, never by the model:
- **a name matching several organizations:** "Merck" is Merck Sharp & Dohme and Merck KGaA (`clarify.sponsor_ambiguity`, after retrieval);
- **a drug class:** "PD-1 inhibitors" lists the drugs most often found in matching trials (`clarify.drug_class_options`);
- **a structured field contradicting the question** (`conflicts.py`).

The user's choice comes back as a structured field with `previous_run_id`, and that answer is final for the question.

**Follow-ups.** A Follow-up sends `previous_run_id`. The model sees the earlier plan and decides whether the new message refines it ("only phase 3") or starts over. A refinement keeps the earlier structured fields. The client holds the conversation, so the service needs no chat memory.

**Questions that ask several things.** The split step separates them, and rewrites each to stand alone ("show their phases" becomes "show the phases of recruiting Keytruda trials in Germany"). Each **Part** gets its own plan, chart, citations and Outcome, so one failing part leaves the others answered. A comparison ("A vs B") is one question, not two.

**Comparisons.** Each side is fetched separately. Trials in both sides go into one **Overlap Group** ("Both") rather than being counted twice.

**Citations.** Every row lists its `trial_ids`. The shared `evidence` map holds each cited trial once, with its link and the exact source values that placed it in the answer (field path → value).

**When there is no answer:**
- **No trial matches:** the answer says how the question was read, then re-counts with each Filter removed and offers those as one-click corrections ("without the country filter: about 126 trials"). A drug name no trial lists gets a spelling hint (`suggest.py`).
- **The registry can't answer it** (efficacy, advice): the reply explains why and offers related questions it could answer.

**Failures.** Every exception becomes exactly one Outcome with a structured error `{code, message, retryable}` (`failures.classify`):
- if the primary model fails, the fallback model answers, and the response carries a warning;
- the API client retries with backoff, honouring `Retry-After`;
- a chart that cannot be drawn keeps the answer and only withholds the image.

**Hosted mode.** The same code runs on AWS when `DEPLOYMENT=hosted`. Redis then shares the API budget, the page cache and retry keys between instances; Postgres keeps the Run history; API keys and an hourly limit protect it. `deploy/aws/deploy.sh` sets it all up in one command, and `teardown.sh` removes it.

## 6. Where to make common changes

| To… | Change |
|---|---|
| Add a way to group trials (a **Dimension**) | an entry in `catalog.DIMENSIONS`, and the line in `ctgov/trial.py` that reads its value from a record (plus the API field in `catalog.TRIAL_FIELDS`); counting, charts, citations and the verifier follow |
| Add a Filter | its fields on `AppliedFilters` in `filters.py`, an entry in `FILTER_DEFS`, its parameters in `api_params` and its check in `violations`; a structured request field too, if callers should pin it (`models/request.py`, `catalog.PINNED_FIELDS`) |
| Add a chart type | the builder in `spec_builder.py`, its choice in `spec_builder.chart_type_for` and `answer._build`, its Vega-Lite in `render.py`, its checks in `verify.py` |
| Change what a clarification offers | `clarify.py` (data-built options) or `conflicts.py` (field conflicts) |
| Change a model or a limit | settings in `config.py` and `.env` (models are `provider:model` strings); the call limits in `ledger.CAPS` |
| Change the model's instructions | `planner.py`: the planning prompt and `SPLIT_INSTRUCTIONS`; then run the eval |

After any change:

```bash
uv run pytest                    # the offline tests (a few seconds)
uv run ruff check src tests && uv run pyright
uv run python -m evals.run       # the planner eval, when a prompt or the catalog changed (needs a model key)
```

## 7. Running it

```bash
uv sync
cp .env.example .env             # add OPENAI_API_KEY and/or ANTHROPIC_API_KEY
uv run clinical-trials-viz serve # the web page at http://127.0.0.1:8000, the API reference at /docs
uv run clinical-trials-viz ask "How many recruiting Keytruda trials are there?"
```

## 8. Where to read more

- [README](../README.md): request and response schemas, design decisions and tradeoffs, failure handling, limitations, how correctness was checked.
- [`docs/harness-design.md`](harness-design.md): the agreed design rules (counting rules, the workflow, the verifier) and why.
- [`docs/hosted-deployment.md`](hosted-deployment.md): the AWS version, its costs and its teardown.
- [`docs/research/api-data-guide.md`](research/api-data-guide.md): what the ClinicalTrials.gov data looks like, measured.
- [`CONTEXT.md`](../CONTEXT.md): the glossary.
