# Query-to-Visualization Workflow Design for ClinicalTrials.gov

> **Research proposal, not fixed scope.** The user wants to explore the complete take-home and useful extensions. Assess these recommendations against [the source assignment](../take-home-assignment.md), [rubric](../take-home-rubric.md), and [exploration agenda](../take-home-exploration.md). The four-workflow recommendation below is a baseline; networks and the other named chart forms remain explicit exploration priorities.

**Research date:** 2026-10-03  
**Scope:** A backend that turns a natural-language clinical-trial question into a structured, frontend-renderable visualization backed by the ClinicalTrials.gov API.  
**Method:** Primary sources only: official documentation and engineering posts, original papers/project pages, and first-party repositories. “Evidence” below summarizes those sources. “Recommendation” or “Inference” identifies the design conclusion drawn for this project.

## Executive conclusion

The strongest design is a **constrained compiler pipeline**, not an autonomous data agent:

```text
request -> structured planner -> semantic validator -> API request compiler
        -> ClinicalTrials.gov client -> provenance-preserving normalizer
        -> deterministic aggregation -> chart-policy compiler
        -> Vega-Lite/custom-network output -> validation -> response
```

Use one LLM call to convert the question into a small typed `QueryPlan`. Allow at most one repair call when that plan fails semantic validation. The model must not write API URLs, execute code, calculate counts, choose arbitrary fields, or manufacture citations. Ordinary code should perform every operation whose correctness can be tested.

This combines the useful boundary in Microsoft Data Formulator/Flint, the analytic intermediate representation in NL4DV, the declarative output contract of Vega-Lite, and the governed semantic layers and validation loops used by production analytics assistants. It also fits a 24-hour exercise: it demonstrates agentic planning without importing enterprise infrastructure the problem does not need.

## What the relevant systems establish

### Microsoft Data Formulator and Flint

**Evidence.** Data Formulator introduces “concept binding”: users express higher-level concepts and bind them to visual channels while AI handles data transformation. Its interface lets users inspect transformed data and iterate rather than treating generation as an opaque result ([Microsoft Research project/paper](https://www.microsoft.com/en-us/research/publication/data-formulator-ai-powered-concept-driven-visualization-authoring/), [Data Formulator repository](https://github.com/microsoft/data-formulator)). Data Formulator 2 extends this into an iterative visualization-authoring agent with mixed natural-language and UI interaction ([original paper](https://arxiv.org/abs/2408.16119)).

Microsoft’s first-party Flint workflow guide draws an especially useful production boundary: the agent proposes semantic chart intent, while the host owns data execution, validation, state, controls, chart compilation, and rendering. It explicitly recommends that an agent emit a semantic chart request rather than raw Vega-Lite, ECharts, or renderer code ([Flint agent-workflow guide](https://github.com/microsoft/flint-chart/blob/main/docs/tutorials/agent-workflows.md)).

**Recommendation.** Adopt that responsibility split. Let the LLM propose an analytic plan; let the service compile the plan into ClinicalTrials.gov requests, aggregates, and a chart. A direct Flint dependency is unnecessary for this take-home.

### LIDA

**Evidence.** LIDA separates visualization work into data summarization, goal generation, visualization generation, editing, explanation, evaluation, and repair ([LIDA repository](https://github.com/microsoft/lida), [ACL demo paper](https://aclanthology.org/2023.acl-demo.11/)). Its implementation generates and executes visualization code, and its repository warns that generated code must run in a secure environment. It also notes that broad datasets often need curated preprocessing.

**Recommendation.** Borrow the staged architecture and bounded repair idea, but do not generate or execute chart code. Typed plans and deterministic transforms are safer, easier to test, and better aligned with the assignment’s structured-output requirement.

### NL4DV

**Evidence.** NL4DV converts a dataset plus natural-language query into a JSON analytic specification containing selected attributes, analytic tasks, and Vega-Lite recommendations. It also exposes ambiguity and dialogue metadata ([documentation](https://nl4dv.github.io/nl4dv/documentation.html), [repository](https://github.com/nl4dv/nl4dv), [original paper](https://arxiv.org/abs/2008.10723)). Its documentation warns that inferred data types should be verified or overridden because incorrect types produce incorrect visualizations. Later first-party research emphasizes interpretable mappings among query phrases, attributes, tasks, and design choices ([analytic-specification paper](https://arxiv.org/abs/2408.13391)).

**Recommendation.** Make the intermediate `QueryPlan` inspectable and include the interpreted intent, dimensions, metric, filters, assumptions, and unresolved ambiguities. Return a clarification response instead of silently guessing when an ambiguity affects the result.

### Vega-Lite and Draco

**Evidence.** Vega-Lite is a declarative JSON grammar in which marks and field-to-channel encodings describe a visualization; it supports transforms such as filtering, aggregation, binning, sorting, and temporal units ([official documentation](https://vega.github.io/vega-lite/docs/), [original paper](https://idl.cs.washington.edu/files/2017-VegaLite-InfoVis.pdf)). Its official JSON Schema supports machine validation, and `usermeta` can carry application metadata that Vega-Lite ignores ([schema repository](https://github.com/vega/schema), [specification docs](https://vega.github.io/vega-lite/docs/spec.html)). Draco formalizes visualization design as hard and soft constraints and ranks valid Vega-Lite designs ([Draco repository](https://github.com/uwdata/draco), [original paper](https://idl.cs.washington.edu/files/2019-Draco-InfoVis.pdf)).

**Recommendation.** Emit Vega-Lite for bars, grouped bars, time series, histograms, and scatter plots, and validate every spec against the official schema. Encode a small deterministic chart policy inspired by Draco rather than integrating Draco. Vega-Lite does not provide a general force-directed network-graph grammar; if networks are implemented, return a separate discriminated `network` payload with `nodes`, `edges`, and encoding metadata.

### Production analytics assistants

**Evidence — Uber.** QueryGPT found that generic schema retrieval degraded as the data estate grew. Uber moved toward curated domain workspaces, explicit intent classification, schema-selection stages, and evaluation against golden questions. It measures intent, table overlap, query executability, non-empty results, qualitative similarity, and latency ([QueryGPT engineering post](https://www.uber.com/us/en/blog/query-gpt/)). Finch similarly relies on curated marts and semantic metadata for a specific finance domain ([Finch engineering post](https://www.uber.com/au/en/blog/unlocking-financial-insights-with-finch/)).

**Evidence — Pinterest.** Pinterest enriches schemas with names, types, descriptions, and low-cardinality values, then prunes large schemas before generation. Its later design uses governed context, existing assets, literal-value profiling, pre-execution validation, conservative limits, bounded error recovery, and transparent source/warning metadata ([original Text-to-SQL post](https://medium.com/pinterest-engineering/how-we-built-text-to-sql-at-pinterest-30bad30dabff), [current context architecture](https://medium.com/pinterest-engineering/unified-context-intent-embeddings-for-scalable-text-to-sql-793635e60aac), [Querybook prompt repository](https://github.com/pinterest/querybook/blob/master/querybook/server/lib/ai_assistant/prompts/text_to_sql_prompt.py)).

**Evidence — Snowflake.** Cortex Analyst uses semantic models containing business names, synonyms, measures, filters, relationships, relevant literals, and verified queries. It constructs a logical query before mapping to physical schema and uses compiler errors in a bounded correction loop ([architecture post](https://www.snowflake.com/en/blog/engineering/snowflake-cortex-analyst-behind-the-scenes/), [official overview](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst)). Its API can return a suggestion when a request is ambiguous. Its evaluation system holds selected verified queries out of runtime context, executes results, and tracks correctness, regressions, and latency ([verified-query docs](https://docs.snowflake.com/en/user-guide/views-semantic/verified-query-repository), [evaluation docs](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst-evaluations)).

**Evidence — Databricks.** Genie spaces use a domain-scoped knowledge store containing tables, metric definitions, sample queries, instructions, and certified assets. Databricks recommends a narrow purpose, simplified documented datasets, executable expressions or examples over prose, minimal initial configuration, and a separate benchmark set ([GA architecture post](https://www.databricks.com/blog/aibi-genie-now-generally-available), [best-practice docs](https://docs.databricks.com/aws/en/genie-agents/best-practices), [concept docs](https://docs.databricks.com/aws/en/genie-agents/concepts)).

**Evidence — dbt.** MetricFlow compiles requests for governed metrics and dimensions into a dataflow plan, optimizes it, and renders engine-specific SQL while centralizing joins, ratios, and time grains ([MetricFlow repository](https://github.com/dbt-labs/metricflow), [dbt semantic-layer post](https://www.getdbt.com/blog/semantic-layer-as-the-data-interface-for-llms)).

**Inference.** These systems converge on a governed semantic layer, narrow domain context, an intermediate plan, deterministic compilation, bounded correction, and held-out evaluation. ClinicalTrials.gov has one known API and a manageable schema, so the equivalent should be a small hand-authored capability catalog. A vector database, schema RAG, SQL generator, or multi-agent table-selection system would add complexity without solving an MVP problem.

### OpenAI structured outputs, tools, and evaluation

**Evidence.** Structured Outputs constrains model responses to a supplied JSON Schema, but the official guide warns that schema-valid output can still contain semantic mistakes and that prompts must define out-of-scope behavior ([Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)). Function calling is a multi-step protocol in which the model requests a JSON-schema-defined tool, the application executes it, and the result is returned to the model; strict schemas are supported ([function-calling guide](https://developers.openai.com/api/docs/guides/function-calling)). OpenAI’s evaluation guidance recommends task-specific evals, logging, human calibration, and evaluating workflow stages independently; it cautions against adding multi-agent complexity before evaluation demonstrates a need ([evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices), [agent-evals guide](https://developers.openai.com/api/docs/guides/agent-evals)).

**Recommendation.** Use Structured Outputs for a single `QueryPlan`, preferably generated from the same Pydantic model used by the service. This plan is data, not a request for the model to execute tools. Add a provider interface only if fallback support is required; keep planning semantics provider-independent.

## ClinicalTrials.gov source contract

**Evidence.** ClinicalTrials.gov publishes an OpenAPI contract for API v2 and dedicated metadata, enum, search-area, version, and study endpoints ([API documentation](https://clinicaltrials.gov/data-api/api), [OpenAPI specification](https://clinicaltrials.gov/api/oas/v2)). The study search endpoint supports field projection, cursor pagination through `nextPageToken`, `countTotal` on the first page, page sizes up to 1,000, explicit sorting, structured filters, and Essie search expressions. The default result order is not a stable analytic order, so final chart rows must be explicitly sorted. The `/version` endpoint exposes the API version and source-data timestamp ([version endpoint](https://clinicaltrials.gov/api/v2/version)). A live check on 2026-10-03 returned API `2.0.5` and data timestamp `2026-10-02T09:00:04`; those observed values will change.

The registry contains information submitted by study sponsors and investigators; ClinicalTrials.gov does not itself establish that every submitted claim is medically valid ([official disclaimer](https://clinicaltrials.gov/about-site/disclaimer)). No numeric rate limit was found in the reviewed official API contract.

**Recommendation.** The client should:

- Compile only allow-listed filters and fields; never accept a model-written URL or free-form API expression.
- Request only fields needed by the chosen capability and avoid per-study follow-up calls.
- Follow `nextPageToken` until absent, tolerate an empty page with a token, and cap total records/pages.
- Apply connect/read timeouts and bounded retries with jitter for transport errors, `429`, and transient `5xx` responses; respect `Retry-After` when present. This is defensive engineering, not a claim about a documented ClinicalTrials.gov rate limit.
- Record canonical request parameters, retrieval time, API version, source-data timestamp, page count, records retrieved, retry count, and whether the result was truncated.
- Count distinct NCT IDs. For multi-valued fields, define the grain explicitly; for example, country counts should deduplicate `(NCT ID, country)` so multiple sites in one country do not inflate the trial count.
- Preserve raw date precision and actual/estimated status rather than silently turning partial or estimated dates into exact dates.

## Recommended contracts

The capability catalog should be code or a small typed configuration file, not prompt prose. Each capability declares its allowed metrics, dimensions, filters, ClinicalTrials.gov fields/search parameters, type, cardinality, normalization rule, compatible chart roles, and user-facing synonyms.

```json
{
  "intent": "distribution",
  "metric": "distinct_trial_count",
  "dimensions": [{"field": "phase"}],
  "filters": [{"field": "condition", "operator": "matches", "value": "melanoma"}],
  "time_range": null,
  "requested_chart": null,
  "sort": {"by": "distinct_trial_count", "direction": "descending"},
  "limit": 20,
  "assumptions": [],
  "clarification": null
}
```

For the MVP, support `distinct_trial_count` plus these dimensions: start year, phase, overall status, sponsor class/name, country, intervention name/type, condition, and study type. Structured request fields should override inferred fields after conflict validation.

The response should keep the rendering contract separate from audit metadata:

```json
{
  "interpretation": {"plan": {}, "assumptions": []},
  "visualization": {"kind": "vega_lite", "spec": {}},
  "data": [{"datum_id": "phase:PHASE3", "phase": "Phase 3", "trial_count": 41, "evidence_ids": ["e1"]}],
  "evidence_index": {"e1": {"nct_id": "NCT01234567", "json_pointer": "/protocolSection/designModule/phases/0", "value": "PHASE3"}},
  "source": {"api_version": "...", "data_timestamp": "...", "retrieved_at": "...", "request": {}, "truncated": false},
  "validation": {"status": "passed", "checks": []},
  "warnings": []
}
```

## Deep per-datum provenance

**Evidence.** W3C PROV models provenance through entities, activities, agents, and derivations ([PROV overview](https://www.w3.org/TR/prov-overview/)). JSON Pointer is the standard syntax for locating a precise value inside a JSON document ([RFC 6901](https://www.rfc-editor.org/info/rfc6901/)). Vega-Lite permits application metadata through `usermeta`, although it does not interpret that metadata.

**Recommendation.** Treat each fetched study snapshot as a source entity, normalization/aggregation as activities, and each chart datum as a derived entity. Give every datum a stable `datum_id`. Its evidence IDs should resolve to the contributing NCT ID, exact JSON Pointer, and exact source value or short excerpt. Store common references once in `evidence_index` rather than copying them into every datum. A provenance verifier should recompute every count from referenced distinct NCT IDs and reject dangling evidence IDs. Put only manifest identifiers in Vega-Lite `usermeta`; keep the full evidence envelope beside the spec.

This makes the bonus citation requirement meaningful: a bar, time bucket, node, or edge can be traced to the exact records and fields that produced it, rather than merely linking to a search page.

## Deterministic chart policy

Use simple tested rules after aggregation:

- temporal dimension + count -> line chart;
- one nominal/ordinal dimension + count -> sorted bar chart;
- one nominal dimension plus a comparison series -> grouped bar chart;
- one quantitative measure distribution -> histogram;
- two quantitative measures -> scatter plot;
- entity relationship pairs -> custom network payload, only if implemented.

The planner may preserve an explicit user chart request, but the compiler must reject incompatible mappings. Titles, axis labels, units, time granularity, sort order, empty-state text, and applied filters belong in the response contract. Return a valid empty visualization with `no_data` metadata when retrieval succeeds but no studies match.

## Validation and repair

Run validation at distinct boundaries:

1. **Request:** Pydantic types, date ranges, limits, and structured-field conflicts.
2. **Plan syntax:** Structured Outputs/Pydantic schema.
3. **Plan semantics:** catalog membership, allowed operators, dimension/metric compatibility, and supported intent.
4. **Execution:** safe compiled parameters, pagination cap, response shape, and source-version capture.
5. **Aggregation:** distinct-count and grain invariants, deterministic ordering, and no duplicate datum IDs.
6. **Visualization:** referenced fields exist, channel types match data types, and Vega-Lite passes its official JSON Schema.
7. **Provenance:** every evidence ID resolves and recomputation matches every displayed value.
8. **Response:** final response schema and size limits.

If step 3 fails, make one repair call containing the original question, invalid plan, capability catalog, and machine-readable errors. If the repaired plan still fails, return `clarification_required` or `unsupported_query`. API, aggregation, provenance, and chart-validation failures should not be sent to the model; they are code or service errors.

## Observability and evaluation

Log a trace ID, planner model and prompt/schema versions, plan, validation errors, repair count, compiled API parameters, pages/records/retries/latencies, aggregation name/version, chart rule, provenance-check result, and terminal status. Do not log secrets or unnecessary free text.

Create 10–15 held-out golden questions spanning time trends, phase/status distributions, comparisons, geography, no results, ambiguity, unsupported medical questions, typo/synonym handling, truncation, and API failure. Evaluate:

- plan intent, dimensions, metric, and filters;
- exact deterministic aggregate against a captured API fixture;
- chart kind and channel mappings;
- response and Vega-Lite schema validity;
- complete, recomputable provenance;
- clarification/no-data/error behavior;
- latency, API pages, and model calls.

Keep prompt examples separate from evaluation questions, following Snowflake and Databricks’ distinction between runtime examples and held-out benchmarks. Unit and integration tests should use recorded fixtures; one optional live contract test can check the current API shape before submission.

## Concrete 24-hour build recommendation

Build one `POST /v1/query` endpoint and four reliable workflows:

1. trial count by start year;
2. distribution by phase, status, sponsor class, or intervention type;
3. two-drug or two-condition comparison by phase or year;
4. recruiting-trial count by country.

Implement a network payload only after these workflows, provenance, and tests pass. A sensible order is: capability catalog and schemas; API client plus fixtures; planner and semantic validator; deterministic normalizers/aggregators; chart compiler; provenance verifier; endpoint, examples, and evaluations.

Use FastAPI/Pydantic, an OpenAI Structured Outputs planner behind a small interface, `httpx`, and Vega-Lite JSON. Inline chart data for the take-home so a frontend can render the response immediately. Include 3–5 actual example requests/responses and show one ambiguity, one no-data case, and one provenance trace.

### Omit from the 24-hour version

- multi-agent orchestration or a general ReAct loop;
- model-generated Python, SQL, Vega-Lite, URLs, filters, counts, or citations;
- PostgreSQL, pgvector, schema RAG, embeddings, and long-term memory;
- LIDA, Data Formulator, Draco, dbt, or warehouse dependencies—their design lessons are enough;
- an unrestricted Essie-query escape hatch;
- arbitrary chart types and a force-directed network renderer unless core paths are complete;
- claims about treatment efficacy, safety, causal effects, or trial quality;
- a full frontend, authentication, deployment platform, or elaborate cache;
- broad model-provider routing, beyond a small interface and one configured fallback if required.

The result will still show the important agent-development skills organically: constrained planning, schema-driven outputs, semantic grounding, deterministic tool execution, bounded repair, source-level provenance, guardrails, observability, and evaluation. Its strongest feature should be that every visual value can be reproduced without trusting the model.
