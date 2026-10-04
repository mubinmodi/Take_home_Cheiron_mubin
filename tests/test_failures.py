"""Failure handling, end to end through the HTTP API: every failure ends in a clear Outcome with a
structured error, one failing part never sinks the others, and a chart that cannot be drawn never
takes the answer down with it."""

import asyncio
import time

import httpx
import pytest
from pydantic_ai import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from clinical_trials_viz import api as api_module
from clinical_trials_viz import pipeline as pipeline_module
from clinical_trials_viz import render as render_module
from clinical_trials_viz.catalog import Dimension
from clinical_trials_viz.ctgov import client as ctgov_client
from clinical_trials_viz.models.plan import AnswerPlan, Filters, Operation
from clinical_trials_viz.models.response import Verification, VerificationCheck
from clinical_trials_viz.planner import LLMPlanner, UnconfiguredPlanner
from tests.conftest import ScriptedPlanner, ask

TREND = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(drugs=["Keytruda"]), group_by=Dimension.START_YEAR)
HISTOGRAM = AnswerPlan(operation=Operation.BIN, filters=Filters(drugs=["pembrolizumab"]))
TWO_PARTS = "Show Keytruda trials per year, and show the enrollment distribution of pembrolizumab trials"


def answering(model_name: str) -> FunctionModel:
    def plan(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        args = {"operation": "aggregate", "filters": {"drugs": ["Keytruda"]}, "group_by": "start_year"}
        return ModelResponse(parts=[ToolCallPart("answer_plan", args)], model_name=model_name)

    return FunctionModel(plan, model_name=model_name)


def failing(model_name: str, status: int | None) -> FunctionModel:
    """A provider that answers with an HTTP error (or, with no status, a connection error)."""

    def plan(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if status is None:
            raise ModelAPIError(model_name, "Connection error.")
        raise ModelHTTPError(status, model_name, body={"error": "secret upstream details"})

    return FunctionModel(plan, model_name=model_name)


@pytest.fixture
def no_sleep(monkeypatch):
    """ClinicalTrials.gov retries back off with real sleeps; skip them."""

    async def instant(_):
        return None

    monkeypatch.setattr(ctgov_client.asyncio, "sleep", instant)


# --- The planning model (LLM API) ---------------------------------------------------------------


async def test_primary_model_failure_falls_back_and_says_so(make_client):
    planner = LLMPlanner(failing("primary-model", 503), answering("fallback-model"))
    body = await ask(make_client, planner, query="Keytruda trials per year")
    assert body["outcome"] == "success"
    assert body["planner_model"] == "fallback-model"
    assert any("primary planning model failed (primary-model: HTTP 503)" in w for w in body["warnings"])


async def test_every_model_failing_is_a_retryable_upstream_error(make_client):
    planner = LLMPlanner(failing("primary-model", 503), failing("fallback-model", None))
    body = await ask(make_client, planner, query="Keytruda trials per year")
    assert body["outcome"] == "upstream_error"
    assert body["error"]["code"] == "planner_unavailable" and body["error"]["retryable"] is True
    assert "primary-model: HTTP 503" in body["message"] and "fallback-model" in body["message"]
    assert "secret upstream details" not in body["message"]  # provider response bodies are never shown


async def test_rejected_credentials_are_not_retryable(make_client):
    planner = LLMPlanner(failing("primary-model", 401), failing("fallback-model", 404))
    body = await ask(make_client, planner, query="Keytruda trials per year")
    assert body["outcome"] == "upstream_error"
    assert body["error"]["code"] == "planner_rejected" and body["error"]["retryable"] is False
    assert "Check the API keys" in body["message"]


async def test_a_hanging_model_hits_the_planning_deadline(make_client):
    async def hang(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    planner = LLMPlanner(FunctionModel(hang, model_name="slow-model"), deadline=0.05)
    started = time.perf_counter()
    body = await ask(make_client, planner, query="Keytruda trials per year")
    assert time.perf_counter() - started < 2
    assert body["outcome"] == "upstream_error"
    assert body["error"]["code"] == "planner_timeout" and body["error"]["retryable"] is True


async def test_a_model_that_returns_no_plan(make_client):
    def chat(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("I think a bar chart would work.")])

    body = await ask(make_client, LLMPlanner(FunctionModel(chat)), query="Keytruda trials per year")
    assert body["outcome"] == "internal_error"
    assert body["error"]["code"] == "planner_invalid_output" and body["error"]["retryable"] is True


async def test_no_model_configured(make_client):
    body = await ask(make_client, UnconfiguredPlanner("No planner model is configured."), query="anything")
    assert body["outcome"] == "internal_error"
    assert body["error"] == {
        "code": "planner_not_configured",
        "message": "No planner model is configured.",
        "retryable": False,
    }


async def test_a_part_the_model_cannot_plan_does_not_stop_the_others(make_client):
    class SecondCallFails(ScriptedPlanner):
        async def plan(self, *args, **kwargs):
            if self.calls:
                self.calls.append({})
                raise ModelHTTPError(503, "primary-model")
            return await super().plan(*args, **kwargs)

    body = await ask(make_client, SecondCallFails(TREND), query=TWO_PARTS)
    assert body["outcome"] == "success" and body["visualization"]["type"] == "time_series"
    second = body["additional_answers"][0]
    assert second["outcome"] == "upstream_error" and second["error"]["code"] == "planner_unavailable"


# --- Bugs and verification ------------------------------------------------------------------------


async def test_a_bug_in_one_part_is_isolated_and_logged_with_the_run_id(make_client, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("histogram bug")

    monkeypatch.setattr(pipeline_module, "histogram_spec", broken)
    body = await ask(make_client, ScriptedPlanner(TREND, HISTOGRAM), query=TWO_PARTS)
    assert body["outcome"] == "success"
    second = body["additional_answers"][0]
    assert second["outcome"] == "internal_error" and second["error"]["code"] == "internal"
    assert body["run_id"] in second["message"]
    assert any(body["run_id"] in r.getMessage() and r.exc_info for r in caplog.records)


async def test_a_failed_verification_withholds_the_answer_with_its_checks(make_client, monkeypatch):
    def failed(*args, **kwargs):
        return Verification(passed=False, checks=[VerificationCheck(name="counts_match_citations", passed=False)])

    monkeypatch.setattr(pipeline_module, "verify", failed)
    body = await ask(make_client, ScriptedPlanner(TREND), query="Keytruda trials per year")
    assert body["outcome"] == "internal_error" and "visualization" not in body
    assert body["error"]["code"] == "verification_failed"
    assert "counts_match_citations" in body["error"]["message"]


async def test_unhandled_errors_still_answer_in_json(make_client, monkeypatch):
    async for client in make_client(ScriptedPlanner()):
        pipeline = client._transport.app.state.pipeline  # type: ignore[attr-defined]

        def broken(run_id):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(pipeline.runs, "load", broken)
        response = await client.get("/v1/runs/run_" + "0" * 32)
        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "internal"


async def test_a_corrupted_run_record_is_not_found(make_client, settings):
    async for client in make_client(ScriptedPlanner(TREND)):
        run_id = (await client.post("/v1/query", json={"query": "q"})).json()["run_id"]
        (settings.runs_dir / f"{run_id}.json").write_text("{ not json")
        response = await client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 404 and response.json()["detail"]["code"] == "run_not_found"


async def test_an_unsaved_run_still_answers_without_dead_links(make_client, monkeypatch):
    def full_disk(*args, **kwargs):
        raise OSError("No space left on device")

    async for client in make_client(ScriptedPlanner(TREND)):
        monkeypatch.setattr(client._transport.app.state.pipeline.runs, "save", full_disk)  # type: ignore[attr-defined]
        body = (await client.post("/v1/query", json={"query": "q"})).json()
        assert body["outcome"] == "success" and "chart_url" not in body
        assert any("could not be saved" in w for w in body["warnings"])


# --- ClinicalTrials.gov ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (httpx.Response(400, text="bad filter"), "source_rejected", False),
        (httpx.Response(503, text="maintenance"), "source_unavailable", True),
        (httpx.Response(429, text="slow down"), "source_rate_limited", True),
        (httpx.Response(200, text="<html>maintenance</html>"), "source_invalid_response", True),
    ],
)
async def test_source_failures_carry_a_code(make_client, ctgov, no_sleep, response, code, retryable):
    ctgov.get("/studies").mock(return_value=response)
    body = await ask(make_client, ScriptedPlanner(TREND), query="Keytruda trials per year")
    assert body["outcome"] == "upstream_error"
    assert body["error"]["code"] == code and body["error"]["retryable"] is retryable


# --- Plotting -------------------------------------------------------------------------------------


async def test_a_chart_that_cannot_compile_keeps_the_answer_and_drops_the_link(make_client, monkeypatch):
    def invalid(spec):
        raise ValueError("invalid Vega-Lite specification")

    monkeypatch.setattr(render_module.vl_convert, "vegalite_to_vega", invalid)
    body = await ask(make_client, ScriptedPlanner(TREND), query="Keytruda trials per year")
    assert body["outcome"] == "success" and body["verification"]["passed"]
    assert body["visualization"]["data"] and body["evidence"]  # the specification and citations stand
    assert "chart_url" not in body
    assert any("chart image could not be prepared" in w for w in body["warnings"])


async def test_a_drawing_failure_is_a_json_500(make_client, monkeypatch):
    def crash(spec, fmt):
        raise RuntimeError("renderer crashed")

    async for client in make_client(ScriptedPlanner(TREND)):
        body = (await client.post("/v1/query", json={"query": "q"})).json()
        monkeypatch.setattr(api_module, "render", crash)
        response = await client.get(f"/v1/runs/{body['run_id']}/chart.png")
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["code"] == "render_failed" and "specification" in detail["message"]
        assert (await client.get(f"/v1/runs/{body['run_id']}")).status_code == 200  # the run itself is fine


async def test_a_slow_drawing_times_out(make_client, settings, monkeypatch):
    def slow(spec, fmt):
        time.sleep(0.5)
        return b""

    settings.render_timeout_seconds = 0.05
    async for client in make_client(ScriptedPlanner(TREND)):
        body = (await client.post("/v1/query", json={"query": "q"})).json()
        monkeypatch.setattr(api_module, "render", slow)
        response = await client.get(f"/v1/runs/{body['run_id']}/chart.png")
        assert response.status_code == 504
        assert response.json()["detail"] == {
            "code": "render_timeout",
            "message": "Drawing the chart took longer than 0.05 s.",
            "retryable": True,
        }


async def test_the_vega_lite_endpoint_fails_in_json_too(make_client, monkeypatch):
    def broken(spec):
        raise KeyError("x")

    async for client in make_client(ScriptedPlanner(TREND)):
        body = (await client.post("/v1/query", json={"query": "q"})).json()
        monkeypatch.setattr(api_module, "to_vega_lite", broken)
        response = await client.get(f"/v1/runs/{body['run_id']}/vega-lite.json")
        assert response.status_code == 500 and response.json()["detail"]["code"] == "render_failed"


def test_cli_summary_shows_the_error_code_and_warnings():
    from clinical_trials_viz.cli import summarize

    failed = {
        "run_id": "run_x", "outcome": "upstream_error", "model_calls": 0, "timings_ms": {},
        "message": "The planning model is unavailable (m: HTTP 503). Please try again shortly.",
        "error": {"code": "planner_unavailable", "message": "…", "retryable": True},
        "warnings": ["Chart image not saved: render failed [render_failed]"],
    }  # fmt: skip
    text = summarize(failed)
    assert "Error code: planner_unavailable (retrying later may help)" in text
    assert "Chart image not saved" in text
