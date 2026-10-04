# Harness Design Approach

**Status:** Agreed direction (2026-10-03; updated the same day with the API spike results and the request/clarification design). Supersedes the overlapping parts of the earlier proposal (`take-home-system-design.md`, kept in the repo, not in the zip) where they conflict (no checkpointer, no Postgres for the local build, no model routing).
**Method:** The author's own design notes (agent system design, harness and loops, memory, a worked walkthrough): goals → scope → rules → data → architecture.
**Evidence:** [research/harness-system-design-research.md](research/harness-system-design-research.md), [research/query-to-visualization-workflow-design.md](research/query-to-visualization-workflow-design.md), [research/api-data-guide.md](research/api-data-guide.md) (API spike).

## 1. Measurable goals ("numbers before boxes")

Targets agreed 2026-10-03, except where marked.

| Goal | Target | Measured by | Result (2026-10-04) |
|---|---|---|---|
| No made-up numbers | 100%, guaranteed by the design | The model never outputs values, IDs or citations; code produces all of them | Met by design: the model returns only a plan; every count, cited trial and citation comes from code and the registry |
| Interpretation accuracy | ≥ 90% correct plans on a fixed set of ~30–40 test questions | Plan-level evals | 46 questions: `gpt-5.4-mini` 46/46, 45/46 and 45/46 in three runs (2026-10-04, with the split step); `claude-haiku-4-5` 95% on the earlier 42 (`evals/results/`) |
| Citations add up | 100% of data points | Verifier recounts every bar, bucket or edge from its cited trials | Every answer passes the verifier before it is returned; tamper tests prove it rejects wrong counts and citations |
| Honest failure | 0 runs that report success after incomplete data | Tests that inject failures | Failure-injection tests for the model, ClinicalTrials.gov, charts and storage (`tests/test_failures.py`) |
| Latency | No target (this is a demo); measured and reported. Hard cap: undecided locally; ~30 s when hosted ([hosted-deployment.md](hosted-deployment.md)) | Timing spans per stage | 3.3–6.2 s for the five submitted examples on 2026-10-04 (planning, split included, 2.0–3.0 s; retrieval 0.6–3.1 s) |
| Cost | ≤ 3 model calls per query (planning); plus one split call, added 2026-10-04 | Counted on every run | Enforced; a single question usually takes 2 calls |

Paging (≤ 1000 trials per page) limits how much can be fetched before the deadline. Past the cap, return `scope_required`; never sample silently.

## 2. Scope: five composable operations

| Operation | Covers |
|---|---|
| `aggregate` (count by a dimension, optional series) | trends, phase or intervention-type distributions, comparisons, countries |
| `bin` | histograms (enrollment) |
| `per_trial` | scatter plots, timelines |
| `relate` | sponsor ↔ drug and drug ↔ drug networks |
| `compare` | `aggregate` over two to five comparison groups: one "only" group per side plus one overlap group ("in more than one"); e.g. A vs B → A only, B only, both |

The example questions in the assignment are examples, not the full list. Any question that combines these operations must work, including:
- **Questions about one study:** the cohort is one NCT ID (or a few). This is a filter, not a new operation; e.g. "countries for NCT…" is `aggregate` over that study's sites.
- **Follow-ups and corrections:** "now only Phase 3", "I meant lead sponsors". See section 5, *Input and clarification*.

**Networks** use entities that ClinicalTrials.gov contains: drugs, sponsors, conditions (later: investigators, sites). Gene networks are not possible; the source has no gene data. The visual model follows the author's reference (an interactive gene-network viewer, not shipped): node colour by entity type, edge colour by edge type with a legend, a minimum-weight threshold (minimum shared trials), and clicking a node or edge shows the trials behind it (the citations).

Out of scope: medical conclusions, free-form API queries, generated SQL or Python, other data sources, gene or molecular data.

## 3. Rules written before any agent (become the capability catalog)

- A **trial** is any registered study, whatever its study type (interventional, observational, expanded access); no study type is excluded by default.
- A trial count is the number of distinct NCT IDs.
- A trial counts once per country, however many sites it has there.
- "Year" means study start date unless stated otherwise; partial or estimated dates are labelled.
- Top-N is applied after full retrieval, never on a partial fetch.
- Two drugs in the same trial is co-listing, not combination therapy, unless arm-level evidence supports it.
- Missing, unknown and not-applicable are separate states.

Rules decided from the API spike ([api-data-guide.md](research/api-data-guide.md) section 5):
- **Multi-phase studies** (e.g. `PHASE1/PHASE2`, 16% of studies) count under each listed phase. This matches the API's own phase filter, so the count check works.
- **Drug match:** retrieve with `query.intr` (synonym-expanded: Keytruda, MK-3475 and pembrolizumab return the same trials), then keep a trial only if the drug is one of its interventions (by intervention MeSH term, or by name / other name when MeSH is missing). About 11% of search matches mention the drug only in other text. The excluded count goes into `assumptions[]`.
- **Condition match:** trust the API's `query.cond` search; no match check. This asymmetry is deliberate: "trials of drug X" means the trial gives drug X, while "trials for condition Y" is fuzzier (a solid-tumour basket trial that includes lung cancer arguably is a lung cancer trial).
- **Drug classes** ("PD-1 inhibitors"): no class list exists in the source, so the service asks. Code runs the text search for the class name, counts the drugs most often found in the matches, and offers them as a multi-select clarification labelled "drugs most often found in trials mentioning …" (not a verified class membership).
- **"Drug"** means intervention types `DRUG`, `BIOLOGICAL` and `COMBINATION_PRODUCT`. Pembrolizumab is recorded as `BIOLOGICAL` in about a third of its trials.
- **Drug identity** is the intervention MeSH term; when it is missing, a cleaned raw name (lower case, dose and ® removed), marked as unresolved. Condition grouping uses condition MeSH terms, not raw text.
- **Countries:** current site locations only; this matches the API's `LocationCountry` filter. Sites moved to `removedCountries` are read but not counted (planned: reporting them in `assumptions[]`; not built).
- **Drug ↔ drug networks:** an edge means "given in the same arm" (arm-to-intervention links exist for 98% of studies). Drugs only co-listed in different arms do not form a combination edge. Supplements (`DIETARY_SUPPLEMENT`), saline and placebo are left out of drug networks.
- **Network size:** top 15 lead sponsors and top 25 drugs by trial count; edges need at least 2 supporting trials. All cut-offs are reported in `assumptions[]`.
- **Dates:** month-only dates (8%) count in their year; `ESTIMATED` dates are labelled; future start years stay visible.
- **Enrollment:** `ESTIMATED` values (43%) are labelled or shown separately in histograms.

The **capability catalog** is one versioned file. It feeds the planner prompt, the plan validator, the README and the tests. It also holds the domain ambiguity taxonomy (date meaning, sponsor role, drug identity, combination vs co-listing, missing comparison entity) with a default-or-ask decision for each.

## 4. Data: source contract

**Done (2026-10-03).** Results in [research/api-data-guide.md](research/api-data-guide.md); saved responses in [research/api-spike/](research/api-spike/).
- Filter → parameter mapping verified for drug, condition, sponsor (lead / collaborator / either), status, phase, start-year range, country, distance and NCT ID.
- Local counts reproduce the API's `countTotal` exactly (start year, phase, country, status), so the verifier's count check is feasible.
- Paging: `pageSize` ≤ 1000, `nextPageToken`; ~1.1 s per page with a trimmed `fields` list (2,968 trials in 3.4 s).
- `/stats/field/values` counts the whole database only and rejects query parameters; code does all filtered counting.
- Allowed values come from `/studies/enums`; field paths are contract-tested against `/studies/metadata`.
- Data is fetched from the live API at request time (decided), with a cache keyed on the `/version` data timestamp. No local copy of the database.
- Still unverified: the rate limit (no rate-limit headers were returned).

## 5. Architecture: three layers

### Harness

The model has two jobs: split the message into its separate questions, and turn each question into a typed plan. The plan is either an executable plan, a request for clarification, or "unsupported".

```
request → validate input → load earlier run (follow-ups) → split into parts (model; code as fallback)
   every part: PLAN (LLM) → semantic gate ─┬─ ok → field-conflict check → fetch every page (completeness gate)
                  ↑ repair once ───────────┘       → sponsor ambiguity check → analyze → build chart
                    (structured validator errors)  → verify → respond
              ambiguous / unsupported / no data → explicit outcome (clarification options built by code, or reason)
```

*Updated 2026-10-04 to match the build:* there is no separate dry-plan compile step (filters become API parameters during retrieval, in `cohort.build_params`); a split step (one model call; the code splitter is its fallback) separates multi-part messages before planning (all parts are planned and gated, then each is answered in turn); and code asks two clarifications of its own, when a structured field contradicts the question and when a sponsor name matches several lead sponsors.

- The model never sees trial records, only the question and the catalog, so trial text can't inject instructions.
- The model never calls tools. The pipeline calls typed functions with structured errors (`{code, message, retryable}`).
- `clarification_required` returns machine-readable multiple-choice options. When a sensible default exists, the system proceeds and records it in `assumptions[]`.

### Input and clarification

- **The question drives the filters.** The user can send only `query`. The model extracts the filters (drug, condition, phase, sponsor and role, country, status, years, NCT IDs, comparison groups) into the `QueryPlan`; code validates them against the catalog and enums and applies them.
- **Structured fields are optional.** They pin a filter without relying on extraction, resolve references such as "this drug", and carry clarification answers back. Unknown fields are rejected. Each field accepts one value or a list, so comparisons and multi-value answers fit. Every applied filter is echoed in the response.
- **Ask only when unclear**, like a multiple-choice pop-up. `clarification_required` holds one `field`, a short `question` and two or more `options` (`label` for display, `value` to send back). Ask when no sensible default exists: a reference with nothing to resolve it ("this drug" with no drug), a comparison with missing groups, or a name that matches different entities (e.g. "Merck": Merck Sharp & Dohme vs Merck KGaA). **Code builds the options from the data** (e.g. the distinct lead sponsors matching "Merck", with trial counts); the model only marks which filter is ambiguous and never writes option values. A clarification may allow several choices (drug classes). Do not ask when a default exists: year = start year, sponsor = lead sponsor, brand names resolved by the API's synonym search. Defaults go into `assumptions[]`.
- **Conflict:** if a structured field and the question text name different values for the same filter, return `clarification_required`; never pick one silently.
- **Follow-ups, corrections and clarification answers** send `previous_run_id`. The service loads the earlier `QueryPlan` from that run's bundle, and the model gets the new message plus the old plan and writes a corrected plan. This is still one model step and needs no chat memory: the client holds the conversation, and the response can show what changed between the two plans. The model decides whether the new message refines the earlier plan or starts a new question, and the response reports which.

### Response format

- **Our own visualization specification**, designed and documented by us; it is the API contract (Vega-Lite is not the contract, only the renderer below). Every response has `type`, `title`, `encoding`, `data` and rendering metadata (units, sort, time granularity, grouping), as the assignment requires. Its JSON Schema is published with the service.
- **Chart type is chosen by code from the plan** (operation + group_by), not by the model, so the same plan always gets the same chart.
- **Types:** `bar_chart`, `grouped_bar_chart`, `time_series`, `histogram`, `scatter_plot`, `timeline`, `network_graph` (nodes and edges), plus two non-chart answers: `single_value` (e.g. "how many recruiting trials…" → one cited number) and `table` (e.g. "list the trials…"). Choosing among them answers the assignment's "is a visualization needed" requirement.
- **Citations:** each datum carries `trial_ids`; one shared `evidence` map holds each trial once (title, link, and the exact field values that placed it in the datum).
- **Rendering (decided 2026-10-03):** the backend's deliverable is the specification. A renderer module translates our spec into Vega-Lite, using only the spec, which also proves the spec is complete. Vega-Lite receives finished values only; all counting, binning and aggregation stay in our code, because citations depend on it (no Vega-Lite `aggregate`, `bin` or `timeUnit` transforms). Static images via `vl-convert` (PNG/SVG, no browser); the same Vega-Lite can power an optional interactive page (tooltips, click a datum to see its trials) via `vega-embed`, re-evaluated against the time left. The response carries a `chart_url`; the image is rendered on first request, not embedded. Networks: Vega-Lite has no graph layout, so `networkx` computes node positions and the renderer draws nodes as points and edges as rules in Vega-Lite (recommended; confirm when building the network slice). *Built (2026-10-04):* the renderer places nodes itself from the spec (two columns for sponsor–drug, a circle for drug–drug), so `networkx` is not used.

### HTTP API and storage

| Endpoint | Returns |
|---|---|
| `POST /v1/query` | Outcome, visualization specification, evidence, assumptions, `run_id`, `chart_url` |
| `GET /v1/runs/{run_id}` | The saved run record: request, plan and response (full run bundles for replay are not built) |
| `GET /v1/runs/{run_id}/chart.png` and `.svg` | The rendered image, from the saved specification |
| `GET /v1/schema` | Request and response JSON Schemas |
| `GET /health` | Service status |

Local build (decided 2026-10-03): each Run is saved as a small **run record** (request, Query Plan, response) in one JSON file, enough for Follow-ups and `chart_url`. Full run bundles with raw API responses (replay) are deferred. Postgres and object storage only in the hosted version.

### Loop

There is no open-ended agent loop; every loop is bounded:
- Plan repair: at most once.
- Provider fallback: at most once, only on provider or transport errors, never on semantic failure. Implemented with pydantic-ai's `FallbackModel`: OpenAI primary, Anthropic fallback by default, and Gemini (`google:…`) available for either slot (added 2026-10-03), all chosen by configuration strings (e.g. `openai:…`, `anthropic:…`, `google:…`) so a model can be changed without code changes. Use tool-based output mode (native structured output inside a fallback chain has an open issue, [pydantic/pydantic-ai#3104](https://github.com/pydantic/pydantic-ai/issues/3104)), set `fallback_on` to provider/transport errors only, and cap pydantic-ai's own retries so every attempt counts toward the 3-call limit.
- Total model calls: at most 3 for planning. *Changed 2026-10-04 (author):* one split call comes first (two if the primary fails): it lists the separate questions in a message and rewrites each to stand alone, because the code splitter dropped parts of messages it did not recognise. Planning keeps its 3.
- Paging stops at the source's end, the page or record cap, or the deadline.

Each run ends in exactly one outcome: `success`, `no_data` (only after complete retrieval), `clarification_required`, `unsupported_query`, `scope_required`, `upstream_error`, `internal_error`.

### Verifier (gate before every response)

*Built (2026-10-04):* the checks in `verify.py` are listed in README §2. Of the design below, the JSON Schema validation, label-length limits and the `countTotal` count check were not built; the count rules were instead verified once against `countTotal` during the API spike (section 1).

- **Validity:** the spec validates against our own published visualization JSON Schema, and every encoded field exists in `data`.
- **Legality:** the chart answers the validated plan (dimension, measure, filters, chart type, sort).
- **Readability:** category limits with top-N + "Other" (after complete retrieval), label lengths, no silent time gaps.
- **Citations:** each datum's cited trials reproduce its value, and each cited field value exists in the captured response.
- **Count check (where possible):** compare local bucket counts with the API's own `countTotal` for a matching filter. A mismatch is flagged; it may be a difference in counting rules rather than a bug.

### LLM ops

- **Trace:** one trace per run, with spans per stage, plus a run record (request, plan, response). A full run bundle with the raw API responses, replayable with no model or API calls, was designed but not built.
- **Evaluate:** binary pass/fail per stage. Process: was the plan right? Outcome: do the numbers match hand-checked fixtures?
- **Diagnose:** a wrong plan means changing the prompt or catalog. A right plan with wrong numbers is a bug; fix the code.
- **Release check:** the eval suite reruns on every prompt, model, schema or catalog change.

## 6. What from the notes does not apply

| Part of the notes | Decision | Reason |
|---|---|---|
| RAG / vector store | Skip | Structured data behind an API with exact filters; the catalog fits in the prompt |
| Semantic and episodic memory | Skip | A run builds on at most one earlier run, loaded from its bundle via `previous_run_id`; no conversation store. Procedural memory = catalog + prompts as files |
| Saving and resuming runs | Skip | Runs take seconds; nothing waits on a human |
| Approval gates | Becomes the verifier | All tools are read-only |
| Model routing | Skip | The split step and the planning step use the same configured models |
| Multi-agent, MCP, semantic caching | Skip | No benefit within 24 hours; list under future work (MCP was considered and dropped: the need was model swapping, met by pydantic-ai configuration) |
| Graph database (Neo4j / Cypher) | Skip | Networks are small and built per question in Python from the live API; a graph store would add a second data copy and generated queries |
| LangGraph | Optional (not used) | Plain Python is enough; each step stays an ordinary testable function |

## 7. Order to work in

1. Goals, scope and counting rules doc (sections 1–3, decided by the author 2026-10-03). Results against the section 1 goals measured 2026-10-04. Steps 1–7 are done.
2. API spike; save fixtures. **Done 2026-10-03** ([api-data-guide.md](research/api-data-guide.md)).
3. Typed models: `QueryRequest`, `QueryPlan`, `AnalysisResult` (each datum carries its contributing trials), `Visualization`, `QueryResponse`.
4. One end-to-end slice: trial count by start year, with citations, verification and replay.
5. Sponsor ↔ drug network, to settle how relationships and citations are represented.
6. Comparisons and countries; then histogram, scatter and timeline; drug ↔ drug last, after checking arm-level data.
7. Eval suite, README, 3–5 real example outputs.

Run the eval suite after each step before moving on.
