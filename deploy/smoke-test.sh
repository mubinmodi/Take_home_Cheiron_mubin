#!/usr/bin/env bash
# Check a deployed service end to end: health, a question, a follow-up and a chart image.
#   deploy/smoke-test.sh https://clinical-trials-viz-xxxx.a.run.app
# Asks for an API key (input hidden) unless API_KEY is exported.
set -euo pipefail
URL=${1:?usage: deploy/smoke-test.sh SERVICE_URL}
URL=${URL%/}
[ -n "${API_KEY:-}" ] || { read -rsp "API key: " API_KEY; echo; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

curl -fsS "$URL/health" && echo
ask() {  # ask BODY: POST /v1/query, print a one-line summary, keep the response
  curl -fsS -X POST "$URL/v1/query" -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" -d "$1" \
    > "$WORK/response.json"
  python3 -c 'import json, sys; r = json.load(open(sys.argv[1])); v = r.get("visualization") or {}; print(r["outcome"], v.get("type"), v.get("title"), r.get("chart_url"))' "$WORK/response.json"
}
ask '{"query": "Which countries have the most recruiting trials for melanoma?"}'
RUN_ID=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["run_id"])' "$WORK/response.json")
CHART=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["chart_url"])' "$WORK/response.json")
ask "{\"query\": \"only phase 3\", \"previous_run_id\": \"$RUN_ID\"}"
curl -fsS -o "$WORK/chart.png" -w "chart: HTTP %{http_code}, %{size_download} bytes, %{content_type}\n" "$CHART"
