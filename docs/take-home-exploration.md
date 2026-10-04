# Exploration agenda: strongest ClinicalTrials.gov take-home

**Priority:** This assignment is the current project objective and the focus of this chat.  
**Read first:** [Assignment](take-home-assignment.md) and [rubric](take-home-rubric.md).  
**Status:** Design exploration; no implementation completion claims.  
**Research input:** [Workflow research](research/query-to-visualization-workflow-design.md) is a proposal to test against the rubric.

**Architecture proposal:** [System design](take-home-system-design.md) translates the research into module interfaces, data/evidence contracts, error behavior, storage, and a build order. It remains subject to the source and evaluation checks below.

## Working ambition

Explore all five appendix query families, all nine example questions, every named chart form, complete per-datum citations, and a small evidence-inspection demo. The final scope should maximize demonstrated quality within the actual available time. A feature earns inclusion by satisfying the assignment or improving correctness, coverage, usability, explainability, or reproducibility. The approximate 24-hour expectation remains visible; no submission deadline or extra-hours budget has been supplied.

The user's broader agent-development portfolio goal remains relevant when a capability serves this assignment. The earlier Clinical Evidence Watch safety/regulatory product is historical. Frameworks, databases, vector search, or multiple agents are options only when an identified requirement justifies them.

## 1. Establish what each question actually means

**Rubric:** FUN-01, AI-01, IO-01, Q-01–Q-09.

Explore trial versus study, drug versus intervention, search match versus exact identity, start year versus registration year, phase categories, lead sponsors versus collaborators, study versus site status, and cohort overlap. Define the default or clarification trigger for each ambiguity. In particular, “combination” must be supported by treatment-arm meaning, not merely two names in a study.

**Reviewable output:** A compact domain/metric dictionary and resolved interpretations for all nine appendix questions. Preserve examples that require clarification or exceed source capability.

## 2. Prove the source contracts

**Rubric:** SRC-01/02, SD-03, CODE-02, CIT-03.

Use official documentation and small live requests to establish the exact fields, query expressions, projection, pagination, timestamps, and missing/array/date behaviors needed. Capture real records demonstrating multi-phase studies, multi-country sites, overlapping interventions, and relevant arms. Explore API-at-request-time, a response cache, and a local analytical snapshot according to data volume and reproducibility needs.

**Reviewable output:** Source field map, saved fixtures, source capability/limitation notes, and one auditable calculation per distinct transformation. Prove API behavior before turning an assumption into a schema or promise.

## 3. Compare planning and execution approaches

**Rubric:** SD-01/02, AI-01/02/03, COV-02.

Compare a constrained typed-plan pipeline, a bounded tool-calling agent, and a hybrid that inspects data before choosing a compatible view. Evaluate on the same held-out questions and failure cases. Research favors a compact semantic plan plus controlled execution, but that remains a design hypothesis. Distinguish provider failure, plan-validation failure, and missing user intent; they need different recovery actions. Evaluate the previously requested OpenAI fallback against the same plan contract.

**Reviewable output:** A decision with correctness/coverage/latency/cost evidence, tool boundaries, recovery limits, and a simple execution trace. Adopt additional agents only if their benefit is demonstrated.

## 4. Design one extensible analytical language

**Rubric:** SD-02, COV-01/02, IO-01.

Explore reusable cohort selection, filters, dimensions, measures, time buckets, grouping, binning, comparison, entity-pair construction, ordering, and top-N. Determine which combinations are meaningful and available from the source. A coherent approach may use distinct typed variants for aggregates, per-trial points, and networks while sharing retrieval and evidence infrastructure.

**Reviewable output:** Request and plan contracts with supported operations and explicit invalid combinations. Show how one new question combines existing operations without an exact-text branch.

## 5. Explore chart coverage and renderer contracts

**Rubric:** OUT-01–08, VIS-01–04, COV-03, IO-02, CHART-01–05.

Investigate bars, grouped bars, time series, individual trial timelines, scatter plots, histograms, and networks. Prefer charts that answer a real question with available data. Compare native Vega-Lite, an application schema with adapters, and a grammar that also supports networks. Confirm the required type/title/encoding/data semantics and rendering metadata for every variant.

**Reviewable output:** Real source-backed examples and render checks for selected chart forms, plus reasons for any deferred type. Test category ordering, sparse timelines, large networks, and evidence inspection. The current lesson is teaching material, not a product demo.

## 6. Make network relationships defensible

**Rubric:** VIS-04, COV-03, Q-08/09, CIT-01–03.

Prioritize sponsor↔drug and drug↔drug questions because the appendix names them. Investigate condition, investigator, and site networks as further options. Specify node identities/types, edge meaning/direction, deduplication, weight units, supporting source fields, minimum support, and size caps. Drug aliases and sponsor spellings may require conservative normalization; retain raw identity and unresolved cases. Test whether arm records can substantiate combination relationships before promising them.

**Reviewable output:** One interpretable network with traceable edges and clearly stated limits. If only trial co-listing is feasible, record Q-09 as partial rather than relabeling it as proven combination therapy.

## 7. Build traceability into the data flow

**Rubric:** CIT-01–04, SD-03, CODE-02.

Compare inline citations and a deduplicated evidence index. Define how each bar, bin, point, time bucket, node, and edge connects to contributing records and exact fields/excerpts. Include source snapshot/version information and the transformation used. Explore how to represent complete contributor sets without excessive response duplication and how to explain zero counts.

**Reviewable output:** A verifier that can reproduce a displayed value from retained evidence and fail on an altered count, missing contributor, unsupported edge, or incorrect source value. An attractive bibliography alone does not satisfy the per-datum bonus.

## 8. Evaluate independently and iterate

**Rubric:** AI-01/02, CODE-02, INT-03/05, all weighted categories.

Build hand-checked data fixtures, held-out language examples, input/response schema tests, API failure tests, chart/render checks, and provenance checks. Separate source drift from regression by retaining snapshots. Probe duplicate sites, phase combinations, overlapping cohorts, missing dates, partial pages, adversarial text, and unsupported metrics. Use targeted mutation where it tests an actual failure hypothesis rather than maximizing test count.

**Reviewable output:** Evaluation results by query family and failure mode, evidence of one meaningful improvement, and a rubric review that points to executed checks.

## 9. Decide which extras improve the submission

**Rubric:** SUB-08, CIT-01–04, COV-01/03, SD-01, INT-05.

High-value candidates to assess:

- A small generic renderer where selecting a bar/point/edge reveals its trials and exact evidence.
- A downloadable response/evidence bundle and replay of a saved run for reproducibility.
- Explicit clarification and follow-up refinements that preserve cohort/filter meaning.
- Useful additional quantitative views (enrollment histograms or duration/enrollment scatter) once source semantics are verified.
- A compact execution trace showing interpretation, requests, record counts, exclusions, chart choice, and checks.
- Caching, bounded concurrency, model fallback, or query caps where measured latency or reliability warrants them.
- A short walkthrough or deployed endpoint if the packaged backend and evidence are already strong.

For each extra record the requirement it improves, proof it works, added complexity, and time cost. None is automatically an employer requirement or a promised bonus score. Generic memory, broad RAG, a vector database, many agents, and extra medical data sources need a demonstrated use case before entering the build.

## 10. Assemble and audit the final hand-in

**Rubric:** SUB-01–08, INT-02–05, all scored criteria.

Select 3–5 actual main runs to show diverse supported questions, charts, and citations. Complete all README disclosures, make limitations visible, and package the code as the requested zip. Validate from a clean extraction. If extras are present, verify they consume the same documented API outputs.

**Reviewable output:** Tested archive, accurate README, actual example JSON, optional demo, final requirement statuses, and an evidence-backed self-review. Report bonus coverage separately from the official 100% category weights.

## Next discussion

Begin with the nine example questions and their meanings, particularly the two network questions. Then prove their required source fields and relationships. Those findings should determine the product specification and architecture. This chat can explore alternatives deeply while keeping a clear record of evidence, accepted decisions, open questions, and measured scope.
