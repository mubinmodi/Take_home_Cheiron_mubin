# Example outputs

Real runs of the service against the live ClinicalTrials.gov API (data timestamp `2026-10-02T09:00:04`, run 2026-10-04, planner `openai:gpt-5.4-mini`). Each folder holds the exact `request.json`, the full `response.json` returned by `POST /v1/query`, and `chart.png` rendered by `GET /v1/runs/{run_id}/chart.png`. Nothing was edited by hand. Each response's `chart_url` points at the local server the run was made on; the same chart is saved next to it as `chart.png`. Regenerate with `uv run python -m examples.generate` (results change as the registry updates).

**The five submitted examples** (the assignment asks for 3–5):

| Example | Question | Outcome | Chart | Cited trials |
|---|---|---|---|---|
| [01-trend-this-drug](01-trend-this-drug/) | "How has the number of trials for this drug changed over time?" + `drug_name: Pembrolizumab` (the assignment's example request) | success | `time_series` | 2,620 |
| [02-countries-recruiting-melanoma](02-countries-recruiting-melanoma/) | "Which countries have the most recruiting trials for melanoma?" | success | `bar_chart` | 480 |
| [03-compare-phases-semaglutide-tirzepatide](03-compare-phases-semaglutide-tirzepatide/) | "Compare phases for trials involving semaglutide vs tirzepatide" | success | `grouped_bar_chart` | 824 |
| [04-network-sponsors-drugs-glioblastoma](04-network-sponsors-drugs-glioblastoma/) | "Show a network of sponsors and drugs for glioblastoma trials" | success | `network_graph` | 975 |
| [05-clarification-merck](05-clarification-merck/) | "What phases are Merck's trials in?" | `clarification_required` | — | — |

**More outputs**, one for each remaining chart type (single values and tables are shown in the README and covered by tests):

| Example | Question | Outcome | Chart | Cited trials |
|---|---|---|---|---|
| [06-histogram-enrollment-breast-cancer](06-histogram-enrollment-breast-cancer/) | "What is the enrollment distribution of breast cancer trials?" | success | `histogram` | 16,873 |
| [07-timeline-recruiting-phase3-keytruda-germany](07-timeline-recruiting-phase3-keytruda-germany/) | "Show a timeline of recruiting phase 3 Keytruda trials in Germany" | success | `timeline` | 50 (of 55) |
| [08-scatter-enrollment-duration-semaglutide](08-scatter-enrollment-duration-semaglutide/) | "Plot enrollment against duration for completed semaglutide trials" | success | `scatter_plot` | 309 |
| [09-network-drug-combinations-pembrolizumab](09-network-drug-combinations-pembrolizumab/) | "Which drugs are most often combined with pembrolizumab?" | success | `network_graph` | 1,776 |

All eight successes passed every verifier check. The responses are large because every Datum cites its trials and the shared `evidence` map holds each cited trial once with its source field values.
