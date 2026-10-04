# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A take-home assignment: a backend service that turns a natural-language clinical-trials question (plus optional structured fields) into a frontend-renderable visualization specification, backed by the ClinicalTrials.gov Data API v2, with deep citations to source records as a bonus. Target: ~24 hours of work, scored on system design 35%, AI/agent design 20%, code quality 20%, query/visualization coverage 15%, input/output design 10%.

This folder is a clean start. It replaces earlier work in `/Users/mubinmodi/Data Agent project/AI_Data_Agent` (a ride-hailing SQL agent, then a "Clinical Evidence Watch" product). **Do not pull code, glossary terms or decisions from that repository.** Only the documents copied here are current.

## Read first, in this order

1. `docs/take-home-assignment.md`: the assignment, transcribed in full. The primary source.
2. `docs/take-home-rubric.md`: requirement IDs, scoring and submission checklist.
3. `docs/harness-design.md`: the agreed design approach (goals, scope, counting rules, harness, loop, verifier, work order).
4. `docs/hosted-deployment.md`: infrastructure and dependencies for a hosted version.
5. `CONTEXT.md`: the domain glossary (Question, Query Plan, Cohort, Trial, Drug, Citation, Run, Outcome…). Use these terms in code and docs.
6. As needed: `docs/take-home-system-design.md` (earlier, more detailed proposal; `harness-design.md` wins where they conflict), `docs/take-home-exploration.md` (open questions), `docs/research/` (cited research).

## Settled decisions

- A fixed workflow with one model step (typed `QueryPlan` via structured output), not an autonomous agent loop.
- Code owns API requests, counting, chart construction and citations; the model never outputs numbers, NCT IDs or citations, and never sees trial records.
- One plan repair, at most one provider fallback, at most 3 model calls per run; explicit terminal outcomes. Planner uses pydantic-ai `FallbackModel`: OpenAI primary (`gpt-5.4-mini`), Anthropic fallback (`claude-haiku-4-5`, same tier) by default; Gemini (`google:…`, `GOOGLE_API_KEY`) can fill either slot; equivalent models per tier and the allowed OpenAI model list are in `config.py` (`MODEL_TIERS`, `ALLOWED_OPENAI_MODELS`); models chosen by configuration; tool-based output mode; fallback on provider errors only; its retries count toward the 3-call limit.
- Our own visualization specification is the API contract (Vega-Lite is only the renderer): `type`, `title`, `encoding`, `data`, metadata; chart types plus `single_value` and `table`. Each datum cites `trial_ids`; one shared `evidence` map holds each trial once. The renderer translates our spec into Vega-Lite with finished values only (no Vega-Lite aggregate/bin/timeUnit; counting stays in code) and exports PNG/SVG via `vl-convert`; networks get positions from `networkx` and are drawn as Vega-Lite points + rules. The interactive page uses the same Vega-Lite translation.
- No RAG, vector database, long-term memory, run checkpointer or multi-agent design.
- Plain Python pipeline (LangGraph optional, not required): each stage is an ordinary testable function returning structured errors `{code, message, retryable}`.
- Every run ends in exactly one outcome: `success`, `no_data` (only after complete retrieval), `clarification_required`, `unsupported_query`, `scope_required`, `upstream_error`, `internal_error`. Never sample silently past the paging cap; return `scope_required`.
- Counting rules, the five operations (`aggregate`, `bin`, `per_trial`, `relate`, `compare`) and the ambiguity taxonomy live in one versioned capability catalog that feeds the planner prompt, plan validator, README and tests (`harness-design.md` sections 2–3).
- The model extracts filters from the question; structured request fields are optional overrides and carry clarification answers. Clarification is multiple-choice and asked only when no sensible default exists. Follow-ups and corrections send `previous_run_id`; the earlier plan is loaded from that run's bundle.
- Domain rules from the design review: a Trial is any study type; drug matches are strict (drug must be an intervention), condition matches trust the API search; drug classes trigger a multi-select clarification built from data; comparisons use per-side "only" groups plus one overlap group (max 5 sides); networks keep top 15 lead sponsors / top 25 drugs, edges ≥ 2 trials. Clarification options are always built by code from data, never by the model.
- Multi-part Questions are split by code (`validate.separate_requests`); each Part is planned alone as a normal question (`planning.py`), answered in `additional_answers`, charts at `?part=N`; the model never produces a multi-part plan. `series_by` crosses two dimensions in one chart.
- Web page at `GET /` (`web/index.html`, vanilla JS + vega-embed from CDN; click a mark → its Citation via `_datum` from `GET /v1/runs/{id}/vega-lite.json`). Follow-ups have their own labelled panel under each answer (one-click examples); a trail lists the conversation, marks each follow-up as refined or new, and reopens earlier answers; the client holds the conversation. Refining Follow-ups inherit the earlier request's structured fields. Endpoints: `POST /v1/query`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/chart.png|.svg`, `GET /v1/schema`, `GET /health`. The response links the image via `chart_url`. `POST /v1/query` honours an optional `Idempotency-Key` header (replay same request; 422 on reuse with a different request; 409 while in progress; 24 h TTL). Run bundles are JSON files locally. No MCP server, no Neo4j.
- Failures (`failures.classify`): every exception becomes one Outcome plus `error {code, message, retryable}` (`ErrorCode` in `models/response.py`), logged with the run ID; provider response bodies never reach the client. Model SDK clients use a 20 s timeout and one retry (SDK defaults are 600 s and two); planning has a 60 s deadline. A primary-model failure that the fallback rescues is a warning on a `success`, and `planner_model` names the answering model. Parts of a multi-part Question fail independently. Charts are compiled at query time (no drawing): failure withholds `chart_url` with a warning; image drawing runs in a thread pool with a time limit. HTTP errors are JSON `{"detail": {code, message, retryable}}`. The web page shows a clickable data table when the chart library cannot load.
- Data comes from the live API at request time (cache keyed on the `/version` data timestamp); no local database copy. Networks use drugs, sponsors and conditions (no gene data exists in the source).
- Local build has no Postgres or checkpointer. Hosted target: container + Redis (cache and shared rate limit) + Postgres (run history) + object storage (run bundles) + tracing.

## Current state

- Working version (2026-10-04): FastAPI service with the full pipeline for `aggregate` (single value, bar, time series), `compare` (grouped bar with overlap group) and `per_trial` (table), data-built clarifications, verifier, Vega-Lite PNG/SVG rendering and run records. `relate` returns a `network_graph`: `sponsor_drug` (two-column image) or `drug_drug` same-arm combinations (circular image). `bin` returns an enrollment `histogram` (actual vs estimated). `per_trial` returns a `table`, a `timeline` or a `scatter_plot` (enrollment vs duration), by `view`. Optional `keywords` filter (free text) for topics that are neither drug nor condition; sponsor categories are never sponsor names (gate check). All tickets under `.scratch/clinical-trials-viz-service/` are done.
- The six assignment screenshots are in `docs/assignment-images/`.
- API spike done: findings in `docs/research/api-data-guide.md`, saved responses in `docs/research/api-spike/`. Goals, scope and counting rules are decided (`harness-design.md` sections 1–3); section 1 records the measured results.
- Run with real models (OpenAI primary, Anthropic fallback); planner eval: gpt-5.4-mini 100%, claude-haiku-4-5 95% on 42 questions (`evals/results/`). Expected plans reviewed by the user (ticket 08 done).
- README and five live example outputs (`examples/`, regenerated 2026-10-04 with the current response fields). The web page and the image renderer both use Vega-Lite 6.4 (`render.VEGA_LITE_VERSION`; exact CDN versions pinned in `web/index.html`). Next: the hosted version as decided in `docs/hosted-deployment.md`, then the final zip.

## Commands

```bash
uv sync                                        # install
cp .env.example .env                           # then add OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY
uv run clinical-trials-viz serve               # API on http://127.0.0.1:8000 (docs at /docs)
uv run clinical-trials-viz ask "How many recruiting Keytruda trials are there?"   # summary; chart saved to data/charts/
uv run clinical-trials-viz ask "..." --json --previous RUN_ID --fields '{"drug_name": "Keytruda"}'  # full response, follow-up, fields
uv run pytest                                  # offline tests (live API tests deselected)
uv run pytest -m live                          # tests against the real ClinicalTrials.gov API
uv run python -m evals.run [case-id ...]       # score the planner on evals/questions.json (needs an API key)
uv run pytest tests/test_api.py::test_compare_has_overlap_group   # one test
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
```

## Code map (`src/clinical_trials_viz/`)

`pipeline.py` runs the fixed workflow and owns Outcomes: `planning.py` (split Parts, plan each with `planner.py`, the only model step, gate and repair) → `validate.py` (merge structured fields, semantic gate, one repair) → `cohort.py` (compile Filters to API params, retrieve all pages, drug match check) → `analyze.py` (all counting) → `spec_builder.py` (chart type chosen by code from the plan, spec + evidence) → `verify.py` (gate) → `runs.py` (run record). `network.py` counts networks. `clarify.py` builds clarification options from data. `failures.py` maps any exception to an Outcome and structured error. `cli.py` holds `serve` and `ask`. `catalog.py` is the capability catalog. `ctgov/` is the API client and the typed `Trial` record. `render.py` turns a spec into Vega-Lite and PNG/SVG. Tests use real records saved in `tests/fixtures/` and mock the API with `respx`.

## Stack

Python 3.13 with `uv`, FastAPI, pydantic v2, async `httpx`, pydantic-ai, `vl-convert-python`, OpenTelemetry; `ruff`, `pyright`, `pytest` + `pytest-asyncio`, `respx`, `hypothesis`. No CI (decided). Full run bundles (raw responses, replay) are deferred; runs are saved as small run records (plan + response).

## Working rules

- Don't claim a requirement is met until tests or real API-backed outputs prove it.
- ClinicalTrials.gov is the only authoritative source for analytical values.
- Follow the work order in `harness-design.md` section 7 and run the eval suite after each step.
- Ask the user before committing to scope expansions; they prefer recommendations with reasons.

## Agent skills

### Issue tracker

Issues and specs are local markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`, recorded as a `Status:` line in each issue file. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
