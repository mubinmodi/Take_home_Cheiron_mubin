"""Live checks against the real ClinicalTrials.gov API. Run with: uv run pytest -m live"""

import httpx
import pytest

from clinical_trials_viz.api import create_app
from clinical_trials_viz.catalog import Dimension, OverallStatus, Phase
from clinical_trials_viz.config import Settings
from clinical_trials_viz.models.plan import AnswerPlan, ComparisonSide, Filters, Operation
from tests.conftest import ScriptedPlanner

pytestmark = pytest.mark.live


async def run(tmp_path, plan: AnswerPlan, query: str) -> dict:
    app = create_app(Settings(runs_dir=tmp_path, otel_exporter="none"), planner=ScriptedPlanner(plan))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=120) as client,
    ):
        return (await client.post("/v1/query", json={"query": query})).json()


async def test_live_trend(tmp_path):
    plan = AnswerPlan(
        operation=Operation.AGGREGATE,
        filters=Filters(drugs=["Keytruda"], phases=[Phase.PHASE3], statuses=[OverallStatus.RECRUITING]),
        group_by=Dimension.START_YEAR,
    )
    body = await run(tmp_path, plan, "Recruiting phase 3 Keytruda trials per year")
    assert body["outcome"] == "success", body.get("message")
    assert body["verification"]["passed"]
    assert body["source"]["data_timestamp"]


async def test_live_compare_countries(tmp_path):
    plan = AnswerPlan(
        operation=Operation.COMPARE,
        filters=Filters(statuses=[OverallStatus.RECRUITING]),
        group_by=Dimension.COUNTRY,
        compare_sides=[ComparisonSide(drug="pembrolizumab"), ComparisonSide(drug="nivolumab")],
        top_n=8,
    )
    body = await run(tmp_path, plan, "Recruiting pembrolizumab vs nivolumab trials by country")
    assert body["outcome"] == "success", body.get("message")
    assert body["verification"]["passed"]
