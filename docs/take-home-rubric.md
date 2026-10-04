# ClinicalTrials.gov take-home: requirements and review rubric

**Authority:** [Transcribed assignment](take-home-assignment.md), visually checked against all six images on 2026-10-03.  
**Purpose:** Use during design, implementation, review, and submission.  
**Current assessment:** Reviewed 2026-10-04 against code, tests and real outputs: [rubric-review.md](rubric-review.md). Capturing a requirement or proposing an architecture is not evidence of implemented behavior.

## Classification and review procedure

- **Required:** Explicit assignment obligation.
- **Scored:** An explicit design goal or evaluation criterion; quality has degrees.
- **Bonus:** Explicit additional credit consideration, with no published point allocation.
- **Optional/example:** Allowed choices or illustrations, not mandatory features.
- **Derived:** Our proposed way of satisfying or testing a requirement. It is not wording from the employer.

For each relevant ID below, record `unassessed`, `planned`, `implemented-unverified`, `verified`, `partial`, or `deferred`, with an evidence path and limitation. Required items cannot be deferred and simultaneously described as a complete submission. Verify against outputs and source records, not a README claim alone. A concrete review record should contain:

```text
ID | status | code/doc evidence | executed validation evidence | limitation
```

Read the source when interpreting an ID. Map a proposed feature to at least one ID before committing it. Audit the required deliverables separately from the weighted quality score. Bonuses cannot hide an unmet core requirement.

## Requirement register

### Assignment context — image 1, header

- **CTX-01 — ~24-hour expectation (constraint).** Make tradeoffs and achieved scope explicit; the source does not give an exact deadline. Evidence: scoped plan and honest limitations.
- **CTX-02 — Any language, libraries, AI tools, and internet access (permission).** Technology choices are open. Evidence: justified stack and tool disclosure; no mandatory framework may be inferred.
- **CTX-03 — Backend converting clinical-trial questions into structured visualizations (required).** Evidence: a runnable service with complete question-to-data-to-spec runs.

### Core behavior and source — image 1, sections 1–2

- **FUN-01 — Interpret the user's question (required).** Evidence: query meaning, selected data, and calculation align on unseen paraphrases; a single hard-coded demo is insufficient.
- **FUN-02 — Retrieve relevant ClinicalTrials.gov data (required).** Evidence: recorded API requests/responses used by an actual run, including query/filter context.
- **FUN-03 — Assess visualization need and suitable type (required).** Evidence: documented decision behavior and compatible outputs for different analytical questions. Resolve the section 1/4 tension explicitly.
- **FUN-04 — Produce a visualization specification answering the question (required).** Evidence: the output's values and encodings answer the input; prose or a chart image alone is insufficient.
- **FUN-05 — Reliable frontend rendering without a required frontend (required output property; optional frontend).** Evidence: a consumer can render from the documented response, with no undocumented transforms or missing fields.
- **SRC-01 — ClinicalTrials.gov Data API is authoritative (required).** Evidence: submitted analytical examples derive their values from it. Synthetic fixtures may test behavior but must be labeled and cannot substitute for actual source-backed examples.
- **SRC-02 — Any needed endpoints/fields may be used (permission).** The provided reference is <https://clinicaltrials.gov/data-api/api>. Evidence: field/endpoint choices justified by supported questions.

### Request — images 1–2, section 3.1

- **IN-01 — Required `query` string containing a natural-language clinical-trial question (required).** Evidence: schema plus successful query-only input; defined behavior for missing/non-string/empty input.
- **IN-02 — Additional structured fields are candidate-defined (optional/examples).** Examples are `drug_name`, `condition/disease`, `trial_phase`, `sponsor`, `country/location`, `start_year`, `end_year`, and other useful fields. These are examples of concepts, not a mandate for slash-containing property names. Evidence if adopted: each field affects interpretation and retrieval as documented.
- **IN-03 — Document field names, types, requiredness, and validation (required).** Evidence: complete request documentation consistent with actual validation.
- **IN-04 — Example using `query` plus `drug_name: Pembrolizumab` (illustrative).** It demonstrates resolving “this drug” from structured context. If structured context is supported, test that behavior and define conflicts with explicit query text. The exact drug/property is not otherwise mandated.

### Response — images 2–3, section 3.2

- **OUT-01 — Structured response describing a visualization (required).** Evidence: machine-parseable, schema-valid successful responses.
- **OUT-02 — Visualization `type` (required).** Evidence: a documented discriminator identifying the visualization type; the example enum spellings are illustrative.
- **OUT-03 — Human-readable `title` (required).** Evidence: titles identify what is shown, agree with the data, and do not invent claims.
- **OUT-04 — Field-to-visual-channel `encoding` (required).** Evidence: defined x/y/series or nodes/edges bindings; all referenced fields exist and have appropriate types.
- **OUT-05 — Renderable `data` points (required).** Evidence: a frontend can obtain every displayed datum from the response contract. Actual placement may vary, but completeness and references must be explicit.
- **OUT-06 — Rendering metadata (required as applicable).** Cover units, sorting, time granularity, grouping choices, and any other information needed to render correctly. Evidence: two independent consumers need not guess these choices.
- **OUT-07 — Notes on assumptions, applied filters, or interpretation (optional).** Our quality target includes these whenever they affect the meaning of the answer; that target is derived.
- **OUT-08 — Document the response schema (required).** Evidence: a frontend engineer can implement a renderer from the docs. For a wrapper around Vega-Lite or another grammar, explain how it supplies `type`, `title`, `encoding`, `data`, and metadata; the wrapper name alone does not meet the requirement.

### Visualization coverage — image 3, section 4

- **VIS-01 — Successful analytical answer is a visualization via structured specification (required).** Evidence: supported query classes produce visual specs; text may accompany them.
- **VIS-02 — Aim for multiple visualization types (scored).** The named examples are bar, grouped bar, timeline/time series, scatter, histogram, and network. Every named type is a candidate for exploration, not an individually mandatory deliverable.
- **VIS-03 — Broad query coverage through one coherent approach (scored).** Evidence: reusable interpretation/data/transformation/output contracts handle different classes and combinations without separate question-string hacks.
- **VIS-04 — Richer meaningful visualizations score higher (scored).** Network examples include drugs, sponsors, conditions, investigators, and sites. Evidence: useful, source-supported relationships and well-defined weights. A network is a coverage differentiator, not described as a separate numerical bonus.

### Deep citations — images 3–5, sections 5 and 7

- **CIT-01 — Per-datum references to contributing trial records (bonus).** Applies to bars, time buckets, and node/edge weights as applicable. Evidence: every implemented citation-bearing datum resolves to its contributing records; coverage gaps are disclosed.
- **CIT-02 — Each reference contains `nct_id` (bonus contract).** Evidence: valid IDs corresponding to the source snapshots actually used.
- **CIT-03 — Exact source excerpt or specific field/value supporting the datum (bonus contract).** Evidence: match against captured API data; a paraphrase, bare search link, or general trial title without supporting information is insufficient.
- **CIT-04 — Implement as much as reasonable within the timebox (bonus qualification).** Evidence: honest statement of which chart/query families have complete traceability. No official bonus percentage is supplied.

### Submission — image 4, section 6

- **SUB-01 — Submit a zip (required).** Evidence: final archive extracted and checked. A repository URL alone is not the requested submission format.
- **SUB-02 — Include all source needed to run the service (required).** Evidence: archive contents plus successful clean setup using declared dependencies/configuration.
- **SUB-03 — README: install, configure, start (required).** Evidence: another person can follow those steps; required keys and environment settings are described without shipping secrets.
- **SUB-04 — README: input and output schemas (required).** Evidence: discoverable schema documentation agrees with service behavior.
- **SUB-05 — README: design decisions and tradeoffs (required).** Evidence: explain alternatives, decisions, and consequences specific to this project.
- **SUB-06 — README: limitations and improvements with more time (required).** Evidence: honest known gaps, data limitations, and feasible next steps.
- **SUB-07 — 3–5 example queries with actual generated JSON outputs (required).** Evidence: saved input/output pairs from the implemented system, reproducible run instructions, and source context. Source-document illustrative JSON does not qualify.
- **SUB-08 — Small UI, deployed endpoint, or short video (optional demo; simple frontend explicitly called a bonus in section 3.2).** Evidence if provided: working demonstration with the real backend outputs. All three formats are listed; the source does not require all of them.

### Integrity and engineering evidence — image 5, section 8

- **INT-01 — AI tools and online resources allowed (permission).** Engineering judgment and design reasoning still matter.
- **INT-02 — README identifies tools used, if any (required disclosure).** Evidence: factual description of assistance and tooling actually used.
- **INT-03 — README describes how correctness was validated (required disclosure).** Evidence: commands, result summaries, source checks, and tests supporting claims.
- **INT-04 — README distinguishes deliberate design/implementation from generated/adapted work (required disclosure).** Evidence: concrete examples of decisions, adaptations, and checks; avoid blanket claims that everything was manually authored.
- **INT-05 — Thoughtful construction, testing, and iteration are rewarded (scored qualitative expectation).** Evidence: discovered failure → deliberate change → demonstrated improvement. Private model reasoning is not required; concise engineering explanations are sufficient.

## Official scoring and our derived quality rubric

The five category weights below are copied from image 5 and sum to 100%. The employer provides no subcriterion weights, rating scale, pass mark, or bonus arithmetic. The following **internal** anchors turn its thirteen bullets into reviewable evidence. They are proposals for our review, not a prediction of the evaluator's exact grading.

Internal rating scale: **0** absent/contradicted; **1** mostly claims or brittle happy path; **2** adequate on documented scope with material gaps; **3** strong evidence across supported cases and relevant failures; **4** compelling, independently reproducible evidence with clear limitations and maintainable design.

If a planning score is useful: `category points = official category weight × mean(its subcriterion ratings) / 4`. Equal weighting within each category is our assumption. Mark unassessed items `N/A`; publish no overall score until all thirteen are assessed. Track bonus coverage separately without inventing points.

### System Design — 35%

- **SD-01 — Clear, rational design decisions.** Strong: component responsibilities and alternatives are explained in relation to requirements. Exceptional: critical assumptions are tested with a source/API or execution experiment and the decision reflects that evidence. Evidence: architecture, decision notes, prototype results.
- **SD-02 — Maintainable structure and extensibility.** Strong: source access, interpretation, calculations, output, and verification have clear contracts. Exceptional: demonstrate adding a dimension or chart without duplicating entire workflows. Evidence: module interfaces and one extension walkthrough.
- **SD-03 — Sensible real-world API-data handling.** Strong: pagination, failures, missing values, array fields, and partial results are handled explicitly. Exceptional: saved data and metadata support reproducibility and tests detect silent incomplete retrieval or misleading aggregates. Evidence: contract tests and adverse fixtures.

### AI / Agent Design — 20%

- **AI-01 — Avoid hallucination-prone steps.** Strong: factual values and citations are grounded in source data; unsupported inputs are handled explicitly. Exceptional: adversarial/paraphrased requests and evidence checks expose semantic failures, including plausible but wrong plans. Evidence: planner evals and output verification.
- **AI-02 — Validation or constraints.** Strong: validate both data shape and permitted meaning, with bounded recovery. Exceptional: demonstrate rejection of schema-valid but semantically invalid plans and fabricated references. Evidence: negative tests and a visible failure/recovery trace.
- **AI-03 — Sensible planning/reasoning and appropriate tools.** Strong: the planning process explains its interpretation and uses appropriate retrieval/computation capabilities. Exceptional: ambiguity and tool failure lead to useful, bounded outcomes whose cost/latency is measured. Evidence: concise plan/decision traces. Multiple agents, free-form code execution, or a particular framework are not required.

### Code Quality — 20%

- **CODE-01 — Readability, organization, documentation.** Strong: focused modules, consistent naming/types, clear contracts, and accurate docs. Exceptional: a reviewer can locate a requirement's implementation and tests quickly; unused scaffolding and misleading claims are absent. Evidence: code review and documentation cross-check.
- **CODE-02 — Correctness and robustness.** Strong: tests independently verify calculations, contracts, and failures. Exceptional: targeted fault injection or mutation proves important checks fail when behavior breaks, and the packaged service runs cleanly. Evidence: test results with independent expected values, live examples, archive smoke test.

### Query and Visualization Coverage — 15%

- **COV-01 — Breadth of supported query types.** Strong: multiple distinct analytical families plus varied fields/entities and paraphrases. Exceptional: demonstrated breadth across the appendix families with explicit supported/partial/unsupported boundaries. Evidence: coverage cases Q-01–Q-09 and documented additions.
- **COV-02 — Multiple question classes without one-off hacks.** Strong: reusable operations compose across supported dimensions/filters. Exceptional: held-out combinations work without adding prompt or code branches for their exact strings. Evidence: unseen examples and architecture review.
- **COV-03 — Richer visualizations, including meaningful networks.** Strong: several suitable chart forms with correct semantics. Exceptional: justified richer views, including source-supported network relationships if implemented, have meaningful nodes, edges, weights, and readable constraints. Evidence: specs, render checks, relationship audits. Adding arbitrary chart types is not evidence of quality.

### Input/Output Design — 10%

- **IO-01 — Well-structured, unambiguous schemas.** Strong: typed documented inputs, outputs, validation, and failure responses. Exceptional: conflict handling, count units, date meanings, and variant compatibility are explicit and independently tested. Evidence: schema checks and consumer examples.
- **IO-02 — Frontend-friendly visualization specification.** Strong: type/title/encoding/data and applicable metadata are complete. Exceptional: all supported variants can be rendered by a generic consumer, with linked evidence inspection if included. Evidence: schema validation and render smoke tests; a full UI remains optional.

## Appendix coverage: preserve all nine source examples

All nine are **illustrative, non-exhaustive**, from image 6. The user wants ambitious exploration: evaluate all nine before choosing final supported scope. Our intended charts, semantics, and checks below are derived. Every case currently has status **unassessed**.

- **Q-01 — “How has the number of trials for [drug] changed per year since 2015?”** Candidate: time series. Set the date meaning explicitly; “number changed” does not specify start year versus registration year. Check inclusive range, incomplete current year, missing dates, and distinct IDs.
- **Q-02 — “How many trials started each year for [condition]?”** Candidate: time series. Use trial start date; preserve actual/estimated meaning and date precision. Check missing years and chronological order.
- **Q-03 — “How are [condition] trials distributed across phases?”** Candidate: bar. Decide how multi-phase records, not-applicable, and unknown phases appear. Test a phase 1/2 record so the denominator and category totals are explainable.
- **Q-04 — “What are the most common intervention types for [drug/condition] trials?”** Candidate: sorted bar. Define distinct trials per type versus intervention count; deduplicate repeated types within a trial. Define ties, top-N, and omitted categories.
- **Q-05 — “Compare phases for trials involving Drug A vs Drug B.”** Candidate: grouped bar. Document membership in both cohorts; a trial involving both drugs is not two distinct global trials. Keep phases, filters, and denominators comparable.
- **Q-06 — “Compare sponsor categories across two conditions.”** Candidate: grouped bar. Define sponsor category and lead-sponsor versus collaborator role. Document overlapping condition cohorts and missing classes.
- **Q-07 — “Which countries have the most recruiting trials for [condition]?”** Candidate: ranked bar or geographic view. Define study recruitment status versus site recruitment status, count each trial once per country, and disclose multi-country membership.
- **Q-08 — “Show a network of sponsors ↔ drugs for [condition] trials.”** Candidate: bipartite network. Define sponsor role, drug filtering, identity rules, and edge weight (e.g., distinct shared trials); preserve evidence for both endpoints and the relationship. A trial-level sponsor/intervention association does not establish a sponsor's commercial ownership of a drug.
- **Q-09 — “Which drugs frequently co-occur in combination studies (drug ↔ drug network)?”** Candidate: drug network. Define “frequently” and what qualifies as a combination. Co-listing drugs in a trial can represent comparator arms; arm-level semantics must support a combination claim. Otherwise clarify or explicitly label the result as co-listing, which provides only partial support for this question.

### Named visualization types beyond specific appendix examples

- **CHART-01 — Bar and grouped bar:** show category/order/series semantics; a grouped bar is not a stacked bar with an unexplained change in meaning.
- **CHART-02 — Timeline/time series:** a series of counts and a timeline of individual trials are different outputs. The assignment lists both possibilities without mandating both.
- **CHART-03 — Scatter:** explore enrollment versus trial duration, subject to field availability, date precision, and actual/estimated distinctions. Two values must describe the same trial; include units and missing-data accounting.
- **CHART-04 — Histogram:** explore enrollment distribution, with declared bins, boundaries, units, exclusions, and actual/estimated enrollment policy. Distinct records must contribute to the right bin.
- **CHART-05 — Network:** investigate drugs, sponsors, conditions, investigators, and sites as possible entities. All are examples; identity resolution and supported edge meaning determine feasibility, especially for people and sites.

## Derived correctness checklist

These are proposed evidence standards for SD-03, AI-01/02, CODE-02, IO-01/02, and CIT-01/03. They are not additional quoted employer requirements.

- Preserve the unit of analysis: unique trial, intervention, site, country membership, or relationship. Array expansion must not silently inflate counts.
- Define drug/condition matching, source search semantics, entity aliases, phase combinations, date fields, status fields, sponsor roles, and unknown values.
- Disclose overlapping cohorts and multi-membership categories; totals need not sum to a global trial count unless groups form a partition.
- Distinguish source search total, fetched count, eligible count, excluded count, and displayed count. Mark capped/failed retrieval as incomplete; a zero after failed retrieval is not “no matching trials.”
- Verify pagination termination, repeated tokens, bounded retries, timeouts, malformed responses, and schema changes using fixtures. Verify current API behavior against official documentation/live contract checks when implementing.
- Carry exact source values through normalization and aggregation. For a count, distinct cited trial membership should reproduce the value; for a sum, mean, duration, or weight, verify the actual transformation and denominator as well.
- Support zero-filled time buckets using documented complete retrieval and the bucket rule; a zero may have an empty contributor set. Do not invent a citation for an absent record.
- Snapshot or otherwise retain sufficient source data, request parameters, retrieval time, and transformation version to explain historical saved outputs as the API changes.
- Constrain model-generated plans and validate meaning as well as JSON. Request text and source narrative remain untrusted input to tool execution.
- Handle ambiguity, invalid input, unsupported queries, no data, upstream failure, and partial data as distinct documented outcomes. Determine their visualization behavior in the product contract.
- Verify chart fields, data types, ordering, units, category limits, node/edge IDs, and all references. Schema validity alone does not prove a chart answers the question.
- Keep recorded-fixture correctness tests separate from live model interpretation evals and live API smoke checks. Stub outputs cannot prove model interpretation quality.
- Use manually checked small fixtures and targeted negative cases. Avoid expected values computed by the same function under test, assertions such as only `answer is not empty`, and conditional assertions that pass when the feature returns nothing.
- Keep held-out evaluation questions separate from prompt examples. Record actual results, failures, costs/latencies, and the evaluated scope.

## Submission evidence checklist

- [ ] Required IDs have verified evidence, or the submission clearly identifies an incomplete requirement.
- [ ] All thirteen scored subcriteria have an evidence-based review; no unsupported overall score.
- [ ] Chosen query families and chart types have explicit support boundaries, including all assessed appendix cases.
- [ ] Deep-citation coverage is measured per implemented chart/query family; each claimed supported value can be traced and recomputed.
- [ ] README includes setup, configuration, startup, both schemas, decisions, tradeoffs, limitations, future improvements, tools used, validation, and deliberate versus generated/adapted work.
- [ ] Three to five main examples contain actual system-produced JSON, not edited illustrative numbers; choose different families. Additional failure/edge-case examples can be separate.
- [ ] Optional UI/endpoint/video uses the same backend response contract and accurately represents implemented behavior.
- [ ] Final zip includes required source and declared dependency/configuration information, excludes credentials/local clutter, and runs after extraction using README instructions.
- [ ] The final walkthrough demonstrates source data → calculation → visual datum → citation, and one meaningful failure or limitation.

## Decisions (all made; open when this rubric was written)

- Timebox: about 24 hours of work, with achieved scope stated in the README.
- Query and chart coverage: all nine appendix questions plus histogram, scatter, timeline and table (README §5).
- Interpretation and clarification rules, structured-field precedence: `docs/harness-design.md`; README §3.
- Network semantics: lead sponsor ↔ drug, and drug ↔ drug in the same arm (README §4–5).
- Rendering contract: our own specification, with Vega-Lite only as renderer.
- Model choices: OpenAI primary with an Anthropic fallback, Gemini optional (`config.py`).
- Evidence and paging: per-Datum citations with source values; complete retrieval up to 20,000 trials, otherwise `scope_required`.
- Optional demonstrations: a web page and a deployed endpoint on AWS.

The prior four-workflow recommendation is a baseline proposal. It is not the assignment's ceiling, an approved final scope, or evidence that networks/scatter/histograms can be ignored.
