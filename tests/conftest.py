"""Shared fixtures: real ClinicalTrials.gov records (saved 2026-10-02) and a scripted planner."""

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clinical_trials_viz.api import create_app
from clinical_trials_viz.config import Settings
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

    async def plan(
        self, question, structured, previous_plan, *, repair=None, max_calls=3, context=None
    ) -> PlannerResult:
        self.calls.append(
            {
                "question": question,
                "structured": structured,
                "previous": previous_plan,
                "repair": repair,
                "context": context,
            }
        )
        return PlannerResult(self.plans.pop(0), 1, "scripted", [])


@pytest.fixture
def http() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@pytest.fixture
def settings(tmp_path) -> Settings:
    # Not the developer's .env: tests must not depend on local settings.
    return Settings(  # type: ignore[call-arg]
        _env_file=None, runs_dir=tmp_path / "runs", otel_exporter="none", ctgov_requests_per_minute=1000
    )


@pytest.fixture
def make_client(settings, ctgov):
    """An HTTP client for the app, with the given planner and mocked ClinicalTrials.gov.

    Unhandled errors come back as HTTP 500 responses (as for a real client) instead of being raised."""

    async def make(planner) -> AsyncIterator[httpx.AsyncClient]:
        app = create_app(settings, planner=planner, http=httpx.AsyncClient())
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            yield client

    return make


async def ask(make_client, planner, **body: Any) -> dict[str, Any]:
    async for client in make_client(planner):
        response = await client.post("/v1/query", json=body)
        assert response.status_code == 200, response.text
        return response.json()
    raise AssertionError
