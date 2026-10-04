# Harness System Design — Research Round 2

**Date:** 2026-10-03
**Note:** research done before the design was settled. It weighs options and names tools that were not used; what was built is in the README and `docs/harness-design.md`.
**Question:** What do published engineering guides, research, and open-source systems suggest for designing a robust harness for the ClinicalTrials.gov query-to-visualization take-home?
**Builds on:** [query-to-visualization-workflow-design.md](query-to-visualization-workflow-design.md), which already covers NL4DV, LIDA, Data Formulator/Flint, Draco, Vega-Lite, Snowflake Cortex Analyst, Databricks Genie, Uber QueryGPT, Pinterest, dbt MetricFlow, and OpenAI Structured Outputs. This note adds only sources that the earlier note did not cover.

Status labels: **Adopt** = use directly. **Adapt** = take the idea, not the tool. **Skip** = reviewed and rejected for this project.

---

## 1. Workflow, not autonomous agent

**Sources**
- Anthropic, [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents): distinguishes *workflows* (LLMs orchestrated through predefined code paths) from *agents* (LLMs direct their own process). Prompt chaining with programmatic "gates" between steps suits tasks that decompose cleanly into fixed subtasks.
- HumanLayer, [12-Factor Agents](https://www.humanlayer.com/blog/12-factor-agents): Factor 4 "tools are just structured outputs", Factor 8 "own your control flow", Factor 10 "small, focused agents" (3–10 steps).
- Anthropic engineering on long-running harnesses ([engineering blog](https://www.anthropic.com/engineering), [InfoQ summary](https://infoq.com/news/2026/04/anthropic-three-agent-harness-ai/)): progress logs, feature lists, and planner/generator/evaluator splits exist for multi-hour sessions. These articles also note that harnesses encode assumptions about what the model can't do, and those assumptions go stale.

**Implication: Adopt.** The query sequence is known in advance, so the system is a prompt-chaining *workflow* with code gates, not an agent loop. "Tools are structured outputs" means the planner emits one typed object and the code dispatches on it. Long-running harness machinery (progress files, multi-agent planner/evaluator) is **Skip**: runs last seconds, not hours. Keep the harness thin and re-test its assumptions when you change models.

## 2. Plan in an intermediate representation over a semantic catalog

**Sources**
- Google, [Looker Conversational Analytics](https://docs.cloud.google.com/looker/docs/2606/conversational-analytics-overview): the data agent grounds on the LookML semantic model and "must determine which LookML fields to select and which filters, sorts, or limits to apply". Google claims the semantic layer cuts gen-AI query errors by up to two-thirds (vendor claim, not independently verified).
- Wren AI ([blog](https://getwren.ai/post/why-the-semantic-layer-is-essential-for-reliable-text-to-sql-and-how-wren-ai-brings-it-to-life), [project overview](https://jimmysong.io/en/ai/wrenai)): an open-source GenBI engine. Business definitions live in a versionable semantic model (MDL), and the pipeline runs *MDL planning → dry-plan validation → structured errors* before execution.
- Research on DSL/IR generation instead of raw SQL ([LLM/Agent-as-Data-Analyst survey](https://arxiv.org/pdf/2509.23988), [enterprise data-analysis agents](https://arxiv.org/pdf/2511.17676)): constrained intermediate representations reduce ambiguity and make semantic validation possible.

**Implication: Adopt.** Looker's agent output (fields + filters + sorts + limits) is almost exactly our `QueryPlan`. Our capability catalog plays the role of LookML/MDL. Add Wren's explicit **dry-plan validation** step: compile the plan to API parameters and check it *before* fetching, returning structured errors the repair attempt can read.

## 3. Treat ambiguity as a first-class outcome

**Sources**
- [nvBench 2.0](https://arxiv.org/abs/2503.12880) (NeurIPS 2025 Datasets & Benchmarks): ambiguous text-to-visualization queries have *multiple valid* interpretations. The benchmark traces each ambiguous query through step-wise reasoning to its candidate charts.
- [AmbiSQL](https://arxiv.org/abs/2508.15276): detects ambiguity using an explicit taxonomy plus examples, then asks **multiple-choice** clarification questions. The authors report 87.2% detection precision and a 50% exact-match improvement (their own benchmark).

**Implication: Adapt.** Write a domain ambiguity taxonomy into the catalog and have the planner classify against it:

| Ambiguity type | Example | Default or ask? |
|---|---|---|
| Date meaning | "trials per year": start, registration, or completion? | Default to start date; record it as an assumption |
| Sponsor role | lead sponsor or collaborator? | Default to lead; record it as an assumption |
| Drug identity | "Keytruda" vs "pembrolizumab" | Pass through to the API's intervention search; disclose it |
| Combination vs co-listing | "drugs used together" | Ask, or return co-listing with a label |
| Missing comparison entity | "compare against the other drug" | Ask |

`clarification_required` should return machine-readable **options** that a frontend can show as buttons, following the AmbiSQL pattern, not free text. When a sensible default exists, proceed and record it in `assumptions[]` rather than blocking the user.

## 4. Grade charts on three separate axes

**Source:** Microsoft Research, [VisEval](https://arxiv.org/abs/2407.00981) ([repo](https://github.com/microsoft/viseval)), IEEE VIS 2024. It evaluates NL2VIS output on **validity** (does it render), **legality** (does it answer the query), and **readability** (does it communicate clearly), using a set of heterogeneous checkers.

**Implication: Adopt the taxonomy for the output verifier, using deterministic checks:**
- *Validity*: the spec validates against the Vega-Lite JSON Schema / our network schema, and every encoded field exists in `data`.
- *Legality*: the chart answers the validated plan. It uses the right dimension and measure, applies every plan filter, has a chart type compatible with the operation, and its sort order matches the plan.
- *Readability*: rules such as ≤ N categories before falling back to top-N + "Other" (only after complete retrieval), no unreadable label lengths, and time axes with no silent gaps.

## 5. Evaluation method: error analysis, binary checks

**Source:** Hamel Husain's evals material ([eval-audit skill summary](https://tessl.io/registry/skills/github/hamelsmu/evals-skills/eval-audit)): build failure categories from reading real traces; prefer **binary pass/fail** over Likert scores; validate any LLM judge against human labels (TPR/TNR) before trusting it.

**Implication: Adopt.** The golden set should contain binary assertions per stage: plan correct, request correct, counts match the hand-checked oracle, chart legal, citations reproduce the value. An LLM judge is optional and only for things like title quality; everything else is code. Start the failure taxonomy from actual traces of the first slice instead of inventing it in advance.

## 6. Provider fallback

**Source:** Pydantic AI `FallbackModel` ([docs](https://pydantic.dev/docs/ai/models/base)): tries models in sequence on API errors (`fallback_on` is configurable). There is an open [issue](https://github.com/pydantic/pydantic-ai/issues/3104) about native structured output inside a fallback chain.

**Implication: Adapt.** Either use `FallbackModel` with tool-based (not native) structured output, or write a small ~30–50 line adapter you control. Rules either way:
- Fall back only on transport/provider failures (timeouts, 5xx, rate limits).
- Never fall back on refusals, ambiguity, or a semantic-validation failure.
- Every provider gets the same schema, the same validator, and the same golden set.
- Count all model calls against the same per-run ceiling.

## 7. ClinicalTrials.gov API features to build on (verified live 2026-10-03)

Checked against the [OpenAPI spec](https://clinicaltrials.gov/api/oas/v2) and live calls:

- **`/studies/enums`** returns the official enumerations (`Phase`, `Status`, `InterventionType`, `AgencyClass`, `StudyType`, …). **Adopt:** generate the catalog's allowed filter values from it, and add a contract test that fails if the source enums change.
- **`/studies/metadata`** returns the data-model field tree. **Adopt:** a contract test confirming every field path in the catalog still exists.
- **`countTotal=true`** with `pageSize=1` returns the server's own count for a filter. Live example: `query.intr=pembrolizumab` → `totalCount` 2968; adding `aggFilters=phase:3` → 367. **Adopt as an independent count oracle:** after local aggregation, re-ask the API for the count of each bucket where a matching filter exists, and flag mismatches.
  - Caveat: the API's phase-filter semantics, especially for combined phases such as `PHASE2|PHASE3`, may differ from our counting rule. A mismatch then means "semantics differ, document it", not automatically "bug". The oracle checks retrieval and membership; it doesn't replace the rules.
- **`/stats/field/values`** gives global value distributions but takes no query filters, so it is not usable as a per-question oracle. **Skip** for verification; useful for exploration.
- `pageSize` max is 1000; paginate with `nextPageToken`.

## 8. Existing ClinicalTrials.gov agent projects

- [agents100x/clinicaltrials-mcp](https://github.com/agents100x/clinicaltrials-mcp) (Python, httpx, MCP): five analyst-shaped tools. Formats JSON before it reaches the model, returns clinical text verbatim rather than paraphrased, and states defaults (e.g., excluding withdrawn trials) explicitly while allowing overrides.
- [cyanheads/clinicaltrialsgov-mcp-server](https://playbooks.com/mcp/cyanheads-clinicaltrials-gov) (TypeScript): search plus lookup, rate limiting, input validation.
- [davidfromkansas/clinical_trials_mcp_server](https://glama.ai/mcp/servers/davidfromkansas/clinical_trials_mcp_server): search with filters plus dataset statistics.

**Implication: Adapt their conventions; differentiate on analysis.** All of them are *lookup* tools for chat. None does population aggregation, chart compilation, or per-datum provenance, which is where this take-home scores. Worth borrowing: never paraphrase source text in citations (verbatim excerpts only), and make default exclusions explicit and overridable.

---

## Net design changes vs. the current proposal

1. Add a **dry-plan validation** stage (compile and check before fetching) with structured errors that feed the single repair attempt. *(Wren AI)*
2. Add a **domain ambiguity taxonomy** to the catalog; `clarification_required` returns multiple-choice options; defaults are recorded as `assumptions[]`. *(nvBench 2.0, AmbiSQL)*
3. Split the output verifier into **validity / legality / readability** checks. *(VisEval)*
4. Add a **server-side count oracle** using `countTotal` for buckets that have a matching API filter. *(ClinicalTrials.gov API)*
5. Generate catalog enums from **`/studies/enums`** and contract-test field paths against **`/studies/metadata`**.
6. Use **binary per-stage evals** built from trace error analysis. *(Hamel Husain)*
7. Unchanged: workflow, not agent; one model step; no RAG, memory, checkpointer, or multi-agent setup. *(Anthropic, 12-Factor Agents)*
