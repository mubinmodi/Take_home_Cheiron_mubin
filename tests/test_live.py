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


@pytest.mark.parametrize(
    "filters",
    [
        {"conditions": ["melanoma"], "phases": ["PHASE3"]},
        {"conditions": ["asthma"], "statuses": ["RECRUITING"], "countries": ["Germany"]},
        {"conditions": ["psoriasis"], "start_year_from": 2020, "start_year_to": 2021},
        {"conditions": ["glioblastoma"], "study_types": ["INTERVENTIONAL"]},
        {"conditions": ["lung cancer"], "sponsor": "Merck Sharp & Dohme LLC", "sponsor_exact": True,
         "exact_sponsors": ["Merck Sharp & Dohme LLC"]},
    ],
)  # fmt: skip
async def test_live_every_trial_the_api_selects_passes_the_local_check(filters):
    """A Filter's retrieval and its local check must agree: every trial retrieval keeps passes the check.
    (For an exact sponsor the API's search is looser, e.g. it also returns "ArQule, a subsidiary of Merck
    Sharp & Dohme LLC"; retrieval then keeps exact names only, which is what this checks.)"""
    from clinical_trials_viz.cohort import fetch_cohort
    from clinical_trials_viz.ctgov.client import CtGovClient, RateLimiter
    from clinical_trials_viz.filters import AppliedFilters

    applied = AppliedFilters(**filters)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    async with httpx.AsyncClient(timeout=60) as http:
        client = CtGovClient(http, settings.ctgov_base_url, RateLimiter(30), settings.max_pages)
        trials = (await fetch_cohort(client, applied)).trials
    assert trials, "retrieval found nothing for this Filter; pick one with trials"
    failing = {t.nct_id: applied.violations(t) for t in trials if applied.violations(t)}
    assert not failing, list(failing.items())[:3]
