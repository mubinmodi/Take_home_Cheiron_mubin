# Rubric review

**Date:** 2026-10-04. **Against:** [take-home-rubric.md](take-home-rubric.md) (requirement IDs) and the [assignment](take-home-assignment.md).
**Method:** each item checked against code, tests and outputs of real runs, not against README claims alone. No overall score is given: the employer publishes no subcriterion weights or scale.

## Required items

| ID | Status | Evidence | Limitation |
|---|---|---|---|
| CTX-03 | verified | Runs locally (`uv run clinical-trials-viz serve`) and on AWS; five real runs in `examples/` | — |
| FUN-01 | verified | One typed plan per question (`planner.py`); planner eval on 42 questions: gpt-5.4-mini 42/42, claude-haiku-4-5 40/42 (`evals/results/`) | Residual planner variance on one comparison (README §8) |
| FUN-02 | verified | Filters compiled to API parameters (`cohort.py`), every page fetched (`ctgov/client.py`); each response's `source` records API version, data timestamp, matches, cohort size and requests | Raw API pages are not stored (run bundles deferred) |
| FUN-03 | verified | README §2, "When the answer is a chart"; the chart type is chosen by code (`spec_builder.py`) | — |
| FUN-04 | verified | The verifier checks the chart answers the plan and recounts every datum (`verify.py`); `examples/` | — |
| FUN-05 | verified | Per-type contract in README §4; `GET /v1/runs/{id}/vega-lite.json`; the web page renders every type from the response | — |
| SRC-01 | verified | Live API at request time; examples carry data timestamp 2026-10-02T09:00:04; test fixtures are saved real records (`tests/fixtures/`) | — |
| IN-01 | verified | `query` required; missing, empty, whitespace-only, non-string, over-long and unknown-field requests each return 422 with a message (checked 2026-10-04) | — |
| IN-03 | verified | README §3 field table; `GET /v1/schema` | — |
| IN-04 | verified | Example 01 (`drug_name: Pembrolizumab` resolves "this drug"); `tests/test_api.py`; conflicts: eval `clarify-03` and `tests/test_contracts.py` | Spotting a conflict is the planner's job; an unflagged one is reported as an assumption |
| OUT-01 – OUT-06 | verified | `models/spec.py`; README §4; the verifier's `encoded_fields_exist` check | — |
| OUT-08 | verified | README §4, "Visualization Specification" (our own spec; Vega-Lite only renders it) | — |
| VIS-01 | verified | Every successful answer carries a visualization (single numbers as `single_value`, lists as `table`) | — |
| SUB-01 | verified | Zip built with `git archive`, unpacked into an empty folder, installed, tested, and a live question answered (see the checklist below) | Rebuild after any later commit |
| SUB-02 | verified | `pyproject.toml` + `uv.lock`; clean `uv sync` from the zip | — |
| SUB-03 | verified | README §1; `.env.example` (no secrets shipped) | — |
| SUB-04 | verified | README §3 and §4; `/v1/schema` | — |
| SUB-05 | verified | README §6 | — |
| SUB-06 | verified | README §8 | — |
| SUB-07 | verified | `examples/`: five real runs (trend, geography, comparison, network, clarification), regenerated 2026-10-04 | — |
| INT-02 | verified | README §10 | — |
| INT-03 | verified | README §9 | — |
| INT-04 | verified | README §10 | The author should confirm it matches their own account |

## Bonus and optional items

| ID | Status | Evidence | Limitation |
|---|---|---|---|
| CIT-01 – CIT-04 | verified | Every Datum of every chart type carries `trial_ids`; `evidence` holds each cited trial once with the source field values that placed it; the verifier recounts and re-derives them | Tables cite only the 100 rows they show (`metadata.total_rows` gives the full count) |
| SUB-08 | verified | Web page at `GET /` (same response contract); deployed on AWS (API key required) | No video |
| OUT-07 | verified | `assumptions` and `applied_filters` on every answer | — |

## Scored subcriteria

| ID | Evidence | Main remaining gap |
|---|---|---|
| SD-01 | API spike before design (`docs/research/api-data-guide.md`); decisions with reasons (`docs/harness-design.md`, README §6) | — |
| SD-02 | Clear module contracts (README code map); extension walkthrough (README §2, "Extending") | The extension is described, not shown as a worked change |
| SD-03 | Complete paging or `scope_required`, never sampling; retries honouring `Retry-After`; missing values as "Not reported"; multi-valued fields counted once per trial per bucket; tests for each | No stored raw pages for exact replay |
| AI-01 | The model never outputs numbers, IDs or citations; every answer is verified; eval questions are not in the prompt | Few adversarial phrasings in the eval |
| AI-02 | Semantic gate with one repair (`validate.py`); tamper tests prove the verifier rejects wrong counts and citations | — |
| AI-03 | One typed plan; clarifications built from data; bounded outcomes; `model_calls` and `timings_ms` on every response | The plan is the only planning trace |
| CODE-01 | Focused modules, README code map, ruff and pyright clean | — |
| CODE-02 | 168 offline tests, including tamper tests and failure injection; regression tests confirmed to fail on the old code; live tests; archive smoke test | — |
| COV-01 | README §5 table: all nine appendix questions with their eval cases, plus supported and unsupported boundaries | — |
| COV-02 | Five composable operations × dimensions × filters, multi-part questions and crossed charts, held out from the prompt | — |
| COV-03 | Nine chart types, including two network kinds with documented weights and size limits | No investigator or site networks |
| IO-01 | Typed request and response, one error shape, conflict handling, count units (`units: trials`), date meaning (start year), all tested | — |
| IO-02 | Every type documented and renderable from the response alone; the web page renders all of them, in light and dark themes | — |

## Appendix questions

All nine (Q-01 to Q-09) are supported; README §5 maps each to its chart, the decisions that define it, and its eval cases.

## Submission checklist

- [x] Required IDs have verified evidence (table above).
- [x] All thirteen scored subcriteria reviewed with evidence; no overall score.
- [x] Support boundaries for query families and chart types (README §5).
- [x] Citation coverage per chart type (README §4; every Datum carries `trial_ids`).
- [x] README has setup, configuration, startup, both schemas, decisions, tradeoffs, limitations, improvements, tools, validation, and designed vs generated work.
- [x] Three to five real examples from different families (`examples/`).
- [x] The web page and the deployed endpoint use the same response contract.
- [x] The zip contains the source and declared dependencies, no credentials or local data, and runs after extraction.
- [x] Walkthrough from source data to calculation, datum and citation, plus a limitation (README §5).
