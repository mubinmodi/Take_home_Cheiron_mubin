# Example outputs

Real runs of the service against the live ClinicalTrials.gov API (data timestamp `2026-10-02T09:00:04`, run 2026-10-04, planner `openai:gpt-5.4-mini`). Each folder holds the exact `request.json`, the full `response.json` returned by `POST /v1/query`, and `chart.png` rendered by `GET /v1/runs/{run_id}/chart.png`. Nothing was edited by hand. Regenerate with `uv run python -m examples.generate` (results change as the registry updates).

| Example | Question | Outcome | Chart | Cited trials |
|---|---|---|---|---|
| [01-trend-this-drug](01-trend-this-drug/) | "How has the number of trials for this drug changed over time?" + `drug_name: Pembrolizumab` (the assignment's example request) | success | `time_series` | 2,629 |
| [02-countries-recruiting-melanoma](02-countries-recruiting-melanoma/) | "Which countries have the most recruiting trials for melanoma?" | success | `bar_chart` | 480 |
| [03-compare-phases-semaglutide-tirzepatide](03-compare-phases-semaglutide-tirzepatide/) | "Compare phases for trials involving semaglutide vs tirzepatide" | success | `grouped_bar_chart` | 832 |
| [04-network-sponsors-drugs-glioblastoma](04-network-sponsors-drugs-glioblastoma/) | "Show a network of sponsors and drugs for glioblastoma trials" | success | `network_graph` | 975 |
| [05-clarification-merck](05-clarification-merck/) | "What phases are Merck's trials in?" | `clarification_required` | — | — |

All four successes passed every verifier check. The responses are large because every Datum cites its trials and the shared `evidence` map holds each cited trial once with its source field values.
