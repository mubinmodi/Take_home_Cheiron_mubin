"""Regenerate the example outputs from the live service: request, full response and chart per example.

    uv run python -m examples.generate

Needs a model API key (see .env.example) and network access to ClinicalTrials.gov.
Outputs are written exactly as the service returns them; nothing is edited by hand.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

HERE = Path(__file__).parent

EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    ("01-trend-this-drug", {"query": "How has the number of trials for this drug changed over time?",
                            "drug_name": "Pembrolizumab"}),
    ("02-countries-recruiting-melanoma", {"query": "Which countries have the most recruiting trials for melanoma?"}),
    ("03-compare-phases-semaglutide-tirzepatide",
     {"query": "Compare phases for trials involving semaglutide vs tirzepatide"}),
    ("04-network-sponsors-drugs-glioblastoma", {"query": "Show a network of sponsors and drugs for glioblastoma trials"}),
    ("05-clarification-merck", {"query": "What phases are Merck's trials in?"}),
]  # fmt: skip


async def main() -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    from clinical_trials_viz.api import create_app

    app = create_app()
    index = []
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8000", timeout=180) as client,
    ):
        for name, request in EXAMPLES:
            folder = HERE / name
            folder.mkdir(exist_ok=True)
            response = (await client.post("/v1/query", json=request)).json()
            (folder / "request.json").write_text(json.dumps(request, indent=2) + "\n")
            (folder / "response.json").write_text(json.dumps(response, indent=2) + "\n")
            chart = None
            if response.get("chart_url"):
                image = await client.get(f"/v1/runs/{response['run_id']}/chart.png")
                if image.status_code == 200:
                    (folder / "chart.png").write_bytes(image.content)
                    chart = "chart.png"
            spec = response.get("visualization") or {}
            index.append({
                "example": name, "question": request["query"], "outcome": response["outcome"],
                "type": spec.get("type"), "title": spec.get("title"),
                "cited_trials": len(response.get("evidence", {})),
                "verification": (response.get("verification") or {}).get("passed"),
                "data_timestamp": (response.get("source") or {}).get("data_timestamp"),
                "model_calls": response.get("model_calls"), "chart": chart,
            })  # fmt: skip
            print(f"{name}: {response['outcome']} {spec.get('type') or ''}")
    (HERE / "index.json").write_text(json.dumps(index, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
