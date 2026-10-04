"""Command line: `serve` runs the API; `ask` answers one question in-process."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

MAX_ROWS = 15


def main() -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # pydantic-ai prints a promotional banner otherwise
    parser = argparse.ArgumentParser(prog="clinical-trials-viz")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    ask = sub.add_parser("ask", help="answer one question and print a summary (or --json for the full response)")
    ask.add_argument("query")
    ask.add_argument("--fields", default="{}", help='structured fields as JSON, e.g. \'{"drug_name": "Keytruda"}\'')
    ask.add_argument("--previous", metavar="RUN_ID", help="follow up on (or answer a clarification for) a run")
    ask.add_argument("--json", action="store_true", help="print the full JSON response")
    ask.add_argument("--no-chart", action="store_true", help="do not save the chart image")
    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "clinical_trials_viz.api:create_app", factory=True, host=args.host, port=args.port, reload=args.reload
        )
        return
    body = {"query": args.query, **json.loads(args.fields)}
    if args.previous:
        body["previous_run_id"] = args.previous
    asyncio.run(_ask(body, full_json=args.json, save_chart=not args.no_chart))


async def _ask(body: dict[str, Any], *, full_json: bool, save_chart: bool) -> None:
    from httpx import ASGITransport, AsyncClient

    from clinical_trials_viz.api import create_app
    from clinical_trials_viz.config import get_settings

    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://local", timeout=120) as client,
    ):
        response = await client.post("/v1/query", json=body)
        if response.status_code != 200:
            print(f"HTTP {response.status_code}: {response.text}")
            return
        result = response.json()
        chart_path = None
        if save_chart and result.get("chart_url"):
            image = await client.get(f"/v1/runs/{result['run_id']}/chart.png")
            if image.status_code == 200:
                chart_path = get_settings().runs_dir.parent / "charts" / f"{result['run_id']}.png"
                chart_path.parent.mkdir(parents=True, exist_ok=True)
                chart_path.write_bytes(image.content)
    print(json.dumps(result, indent=2) if full_json else summarize(result, chart_path))


def summarize(r: dict[str, Any], chart_path: Path | None = None) -> str:
    """A short, readable view of a QueryResponse."""
    lines: list[str] = []
    seconds = sum(r.get("timings_ms", {}).values()) / 1000
    api_requests = (r.get("source") or {}).get("api_requests", 0)
    spec = r.get("visualization")
    kind = f" · {spec['type']}" if spec else ""
    lines.append(f"{r['outcome']}{kind} · {r.get('model_calls', 0)} model call(s) · "
                 f"{api_requests} API request(s) · {seconds:.1f} s")  # fmt: skip
    if r.get("message") and r["outcome"] != "clarification_required":
        lines.append(r["message"])

    if spec:
        lines += ["", spec["title"], *_data_lines(spec)]
    if clarification := r.get("clarification"):
        lines += ["", clarification["question"]]
        for i, option in enumerate(clarification.get("options", []), 1):
            lines.append(f"  {i}. {option['label']}")
        hint = "several values as a JSON list" if clarification.get("multi_select") else "one value"
        lines.append(f"Answer with --previous {r['run_id']} --fields '{{\"{clarification['field']}\": ...}}' ({hint})")

    if filters := r.get("applied_filters"):
        shown = {k: v for k, v in filters.items() if v and k not in ("sponsor_role", "sponsor_exact", "from_request")}
        if shown:
            lines += ["", "Filters: " + "; ".join(f"{k}={_fmt(v)}" for k, v in shown.items())]
    if assumptions := r.get("assumptions"):
        lines += ["Assumptions:", *(f"  - {a}" for a in assumptions)]
    if verification := r.get("verification"):
        failed = [c["name"] for c in verification["checks"] if not c["passed"]]
        status = "passed" if verification["passed"] else f"FAILED ({', '.join(failed)})"
        lines.append(f"Verification: {status} ({len(verification['checks'])} checks) · "
                     f"{len(r.get('evidence', {}))} trials cited")  # fmt: skip
    if chart_path:
        lines.append(f"Chart: {chart_path}")
    lines.append(f"Run: {r['run_id']} (full response: --json)")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def _data_lines(spec: dict[str, Any]) -> list[str]:
    enc, data = spec["encoding"], spec["data"]
    if spec["type"] == "single_value":
        return [f"  {data[0]['trial_count']:,} trials"]
    if spec["type"] == "network_graph":
        labels = {n["id"]: n["label"] for n in data["nodes"]}
        rows = [f"  {labels[e['source']][:40]} — {labels[e['target']][:40]}  {e['trial_count']:>5,}"
                for e in data["edges"][:MAX_ROWS]]  # fmt: skip
        more = len(data["edges"]) - MAX_ROWS
        return [f"  {len(data['nodes'])} nodes, {len(data['edges'])} links (heaviest first):", *rows] + (
            [f"  … {more} more links"] if more > 0 else []
        )
    if spec["type"] == "timeline":
        rows = [f"  {d['start']} → {d['end']}  {d['trial'][:90]}" for d in data[:MAX_ROWS]]
        return rows + ([f"  … {len(data) - MAX_ROWS} more"] if len(data) > MAX_ROWS else [])
    if spec["type"] == "table":
        rows = [f"  {d['nct_id']}  {d.get('start_date') or '':<10}  {d['title'][:80]}" for d in data[:MAX_ROWS]]
        total = spec["metadata"].get("total_rows") or len(data)
        return rows + ([f"  … {total - MAX_ROWS} more"] if total > MAX_ROWS else [])
    x = enc["x"]["field"]
    color = (enc.get("color") or {}).get("field")
    if color:  # split charts: empty combinations add noise in a text summary
        data = [d for d in data if d["trial_count"]]
    labels = [f"{d[x]} / {d[color]}" if color else str(d[x]) for d in data]
    width = min(max(map(len, labels), default=0), 50)
    rows = [f"  {label[:50]:<{width}}  {d['trial_count']:>6,}" for label, d in zip(labels, data, strict=True)]
    if len(rows) > MAX_ROWS * 2:
        rows = [*rows[: MAX_ROWS * 2], f"  … {len(rows) - MAX_ROWS * 2} more rows"]
    return rows
