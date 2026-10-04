"""Shared fixtures: real ClinicalTrials.gov records (saved 2026-10-02) and a scripted planner."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clinical_trials_viz.ctgov.trial import Trial, parse_trial
from clinical_trials_viz.models.plan import QueryPlan
from clinical_trials_viz.planner import PlannerResult

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://clinicaltrials.gov/api/v2"


@pytest.fixture(scope="session")
def page() -> dict[str, Any]:
    """50 pembrolizumab search matches: 45 list it as an intervention, 5 only mention it."""
    return json.loads((FIXTURES / "pembrolizumab_page.json").read_text())


@pytest.fixture(scope="session")
def trials(page: dict[str, Any]) -> list[Trial]:
    return [parse_trial(s) for s in page["studies"]]


@pytest.fixture
def ctgov(page: dict[str, Any]) -> Iterator[respx.MockRouter]:
    """Mock ClinicalTrials.gov: every search returns the fixture page."""
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.get("/version").respond(json={"apiVersion": "2.0.5", "dataTimestamp": "2026-10-02T09:00:04"})
        router.get("/stats/field/values").respond(
            json=[{"topValues": [{"value": v} for v in ("United States", "Germany", "South Korea")]}]
        )
        router.get("/studies").respond(json=page)
        yield router


class ScriptedPlanner:
    """Returns pre-written plans in order; stands in for the model in tests."""

    def __init__(self, *plans: QueryPlan):
        self.plans = list(plans)
        self.calls: list[dict[str, Any]] = []

    async def plan(self, question, structured, previous_plan, *, repair=None, max_calls=3) -> PlannerResult:
        self.calls.append({"question": question, "structured": structured, "previous": previous_plan, "repair": repair})
        return PlannerResult(self.plans.pop(0), 1, "scripted", [])


@pytest.fixture
def http() -> httpx.AsyncClient:
    return httpx.AsyncClient()
