"""End-to-end through the HTTP API: scripted planner, mocked ClinicalTrials.gov, real everything else."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from clinical_trials_viz.api import create_app
from clinical_trials_viz.catalog import Dimension
from clinical_trials_viz.config import Settings
from clinical_trials_viz.models.plan import (
    AnswerPlan,
    ClarificationReason,
    ClarifyPlan,
    ComparisonSide,
    Filters,
    Operation,
    UnsupportedPlan,
)
from clinical_trials_viz.planner import LLMPlanner
from tests.conftest import ScriptedPlanner


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(runs_dir=tmp_path / "runs", otel_exporter="none", ctgov_requests_per_minute=1000)


@pytest.fixture
def make_client(settings, ctgov):
    async def make(planner) -> AsyncIterator[httpx.AsyncClient]:
        app = create_app(settings, planner=planner, http=httpx.AsyncClient())
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            yield client

    return make


async def ask(make_client, planner, **body: Any) -> dict[str, Any]:
    async for client in make_client(planner):
        response = await client.post("/v1/query", json=body)
        assert response.status_code == 200, response.text
        return response.json()
    raise AssertionError


TREND = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(drugs=["Keytruda"]), group_by=Dimension.START_YEAR)


async def test_trend_question_end_to_end(make_client):
    planner = ScriptedPlanner(TREND)
    async for client in make_client(planner):
        body = (await client.post("/v1/query", json={"query": "How have Keytruda trials changed over time?"})).json()
        assert body["outcome"] == "success"
        spec = body["visualization"]
        assert spec["type"] == "time_series"
        assert spec["metadata"]["cohort_size"] == 46  # 4 of 50 search matches excluded by the match check
        assert body["verification"]["passed"]
        cited = {i for d in spec["data"] for i in d["trial_ids"]}
        assert cited == set(body["evidence"])
        assert any("excluded" in a for a in body["assumptions"])
        assert any("'pembrolizumab'" in a for a in body["assumptions"])

        chart = await client.get(body["chart_url"].replace("http://localhost:8000", ""))
        assert chart.status_code == 200 and chart.content.startswith(b"\x89PNG")
        record = await client.get(f"/v1/runs/{body['run_id']}")
        assert record.json()["plan"]["group_by"] == "start_year"


async def test_structured_field_resolves_this_drug(make_client):
    plan = AnswerPlan(operation=Operation.AGGREGATE, group_by=Dimension.PHASE)
    body = await ask(make_client, ScriptedPlanner(plan), query="Phases for this drug?", drug_name="Pembrolizumab")
    assert body["applied_filters"]["drugs"] == ["Pembrolizumab"]
    assert "drugs" in body["applied_filters"]["from_request"]
    assert body["visualization"]["type"] == "bar_chart"


async def test_single_value_and_table(make_client):
    count = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(drugs=["pembrolizumab"]))
    body = await ask(make_client, ScriptedPlanner(count), query="How many pembrolizumab trials?")
    assert body["visualization"]["type"] == "single_value"
    assert body["visualization"]["data"][0]["trial_count"] == 46

    listing = AnswerPlan(operation=Operation.PER_TRIAL, filters=Filters(drugs=["pembrolizumab"]))
    body = await ask(make_client, ScriptedPlanner(listing), query="List pembrolizumab trials")
    assert body["visualization"]["type"] == "table"
    assert "chart_url" not in body


async def test_compare_has_overlap_group(make_client):
    plan = AnswerPlan(
        operation=Operation.COMPARE,
        group_by=Dimension.PHASE,
        compare_sides=[ComparisonSide(drug="pembrolizumab"), ComparisonSide(drug="Keytruda")],
    )
    body = await ask(make_client, ScriptedPlanner(plan), query="Compare phases for pembrolizumab vs Keytruda")
    assert body["outcome"] == "success"
    spec = body["visualization"]
    assert spec["type"] == "grouped_bar_chart"
    assert spec["metadata"]["series_order"] == ["pembrolizumab only", "Keytruda only", "Both"]
    # Same drug under two names: every trial lands in the overlap group.
    assert all(d["trial_count"] == 0 for d in spec["data"] if d["group"] != "Both")


async def test_drug_class_clarification_options_come_from_data(make_client):
    plan = ClarifyPlan(reason=ClarificationReason.DRUG_CLASS, field="drug", term="PD-1 inhibitors", question="?")
    body = await ask(make_client, ScriptedPlanner(plan), query="Trends for PD-1 inhibitors")
    assert body["outcome"] == "clarification_required"
    clarification = body["clarification"]
    assert clarification["multi_select"] and clarification["field"] == "drug_name"
    assert clarification["options"][0]["value"] == "pembrolizumab"
    assert all(o["trial_count"] for o in clarification["options"])


async def test_unsupported_and_not_yet_available(make_client):
    body = await ask(
        make_client, ScriptedPlanner(UnsupportedPlan(reason="Efficacy is not in the registry.")), query="Does it work?"
    )
    assert body["outcome"] == "unsupported_query"
    histogram = AnswerPlan(operation=Operation.BIN, group_by=None)
    body = await ask(make_client, ScriptedPlanner(histogram), query="Enrollment histogram")
    assert body["outcome"] == "unsupported_query"


async def test_invalid_plan_is_repaired_once(make_client):
    bad = AnswerPlan(operation=Operation.COMPARE, compare_sides=[ComparisonSide(drug="pembrolizumab")])
    planner = ScriptedPlanner(bad, TREND)
    body = await ask(make_client, planner, query="Compare pembrolizumab")
    assert body["outcome"] == "success"
    assert body["model_calls"] == 2
    assert planner.calls[1]["repair"] is not None
    assert "compare needs 2 to 5" in planner.calls[1]["repair"][1][0]


async def test_follow_up_receives_previous_plan(make_client):
    async for client in make_client(ScriptedPlanner(TREND, TREND)):
        first = (await client.post("/v1/query", json={"query": "Keytruda trials per year"})).json()
        planner = client._transport.app.state.pipeline.planner  # type: ignore[attr-defined]
        second = await client.post("/v1/query", json={"query": "only phase 3", "previous_run_id": first["run_id"]})
        assert second.status_code == 200
        assert planner.calls[1]["previous"] == TREND
        missing = await client.post("/v1/query", json={"query": "x", "previous_run_id": "run_" + "0" * 32})
        assert missing.status_code == 404


async def test_scope_required_instead_of_sampling(make_client, ctgov):
    ctgov.get("/studies").respond(json={"totalCount": 45_000, "studies": [], "nextPageToken": "t"})
    plan = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(conditions=["cancer"]), group_by=Dimension.PHASE)
    body = await ask(make_client, ScriptedPlanner(plan), query="Cancer trials by phase")
    assert body["outcome"] == "scope_required"
    assert "45,000" in body["message"]


async def test_upstream_error(make_client, ctgov):
    ctgov.get("/studies").respond(status_code=400, text="bad request")
    body = await ask(make_client, ScriptedPlanner(TREND), query="Keytruda per year")
    assert body["outcome"] == "upstream_error"


async def test_no_data_only_after_complete_retrieval(make_client, ctgov):
    ctgov.get("/studies").respond(json={"totalCount": 0, "studies": []})
    plan = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(conditions=["nothing"]))
    body = await ask(make_client, ScriptedPlanner(plan), query="Trials for nothing")
    assert body["outcome"] == "no_data"


async def test_schema_and_health(make_client):
    async for client in make_client(ScriptedPlanner()):
        schema = (await client.get("/v1/schema")).json()
        assert "query" in schema["request"]["required"]
        assert (await client.get("/health")).json() == {"status": "ok"}


async def test_llm_planner_wiring_with_tool_output(make_client):
    """The real pydantic-ai planner, with a function standing in for the provider."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert {t.name for t in info.output_tools} == {"answer_plan", "clarify_plan", "unsupported_plan"}
        args = {"operation": "aggregate", "filters": {"drugs": ["Keytruda"]}, "group_by": "start_year"}
        return ModelResponse(parts=[ToolCallPart("answer_plan", args)])

    body = await ask(make_client, LLMPlanner(FunctionModel(model)), query="Keytruda trials per year")
    assert body["outcome"] == "success"
    assert body["model_calls"] == 1
