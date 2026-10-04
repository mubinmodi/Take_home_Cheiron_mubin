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
    NetworkKind,
    Operation,
    PerTrialView,
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


def test_build_planner_uses_only_models_with_keys(monkeypatch):
    """Swapping providers is configuration: any of OpenAI, Anthropic or Gemini fills either slot."""
    from clinical_trials_viz.planner import LLMPlanner, UnconfiguredPlanner, build_planner

    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert isinstance(build_planner("openai:gpt-5.4-mini", "google:gemini-3.5-flash"), UnconfiguredPlanner)

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    assert isinstance(build_planner("openai:gpt-5.4-mini", "google:gemini-3.5-flash"), LLMPlanner)
    assert isinstance(build_planner("google:gemini-3.5-flash", None), LLMPlanner)


def test_model_tiers_use_known_models_and_allowed_openai(monkeypatch):
    import typing

    from pydantic_ai.models import KnownModelName

    from clinical_trials_viz.config import MODEL_TIERS, Settings, disallowed_openai_models

    known = set(typing.get_args(KnownModelName.__value__))
    names = [name for tier in MODEL_TIERS.values() for name in tier.values()]
    assert set(names) <= known
    assert disallowed_openai_models(*names) == []
    assert disallowed_openai_models("openai:gpt-6-sol", "google:gemini-3.5-flash") == ["openai:gpt-6-sol"]
    monkeypatch.delenv("PLANNER_PRIMARY", raising=False)
    monkeypatch.delenv("PLANNER_FALLBACK", raising=False)
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    assert defaults.planner_primary == MODEL_TIERS["mini"]["openai"]
    assert defaults.planner_fallback == MODEL_TIERS["mini"]["anthropic"]


def test_cli_summary_is_short_and_points_to_the_full_response():
    from clinical_trials_viz.cli import summarize

    response = {
        "run_id": "run_x", "outcome": "success", "model_calls": 1, "timings_ms": {"plan": 1000.0},
        "source": {"api_requests": 3},
        "visualization": {"type": "single_value", "title": "Number of trials: Keytruda", "encoding": {},
                          "data": [{"trial_count": 117, "trial_ids": ["NCT1"]}], "metadata": {}},
        "applied_filters": {"drugs": ["Keytruda"], "sponsor_role": "lead"},
        "assumptions": ["'Keytruda' was matched as 'pembrolizumab'."],
        "verification": {"passed": True, "checks": [{"name": "a", "passed": True}]},
        "evidence": {"NCT1": {}},
    }  # fmt: skip
    text = summarize(response)
    assert "117 trials" in text and "Filters: drugs=Keytruda" in text and "--json" in text
    assert "NCT1" not in text  # evidence is not dumped


SPONSOR_DRUG = AnswerPlan(
    operation=Operation.RELATE, network=NetworkKind.SPONSOR_DRUG, filters=Filters(drugs=["pembrolizumab"])
)


async def test_sponsor_drug_network_end_to_end(make_client):
    async for client in make_client(ScriptedPlanner(SPONSOR_DRUG)):
        body = (await client.post("/v1/query", json={"query": "Network of sponsors and drugs"})).json()
        assert body["outcome"] == "success", body.get("message")
        spec = body["visualization"]
        assert spec["type"] == "network_graph"
        nodes, edges = spec["data"]["nodes"], spec["data"]["edges"]
        ids = {n["id"] for n in nodes}
        assert edges and all(e["source"] in ids and e["target"] in ids for e in edges)
        assert all(e["trial_count"] >= 2 for e in edges)
        assert sum(n["kind"] == "sponsor" for n in nodes) <= 15
        assert sum(n["kind"] == "drug" for n in nodes) <= 25
        assert not {"placebo", "standard of care"} & {n["label"] for n in nodes}
        assert body["verification"]["passed"]
        assert {c["name"] for c in body["verification"]["checks"]} >= {
            "network_matches_source",
            "counts_match_citations",
        }
        assert any("top 15 lead sponsors" in a for a in body["assumptions"])
        cited = next(iter(body["evidence"].values()))["fields"]
        assert "protocolSection.sponsorCollaboratorsModule.leadSponsor.name" in cited
        assert "derivedSection.interventionBrowseModule.meshes.term" in cited
        chart = await client.get(f"/v1/runs/{body['run_id']}/chart.png")
        assert chart.status_code == 200 and chart.content.startswith(b"\x89PNG")


def test_verifier_rejects_a_trial_cited_for_an_edge_it_does_not_have(trials):
    from clinical_trials_viz.models.response import AppliedFilters
    from clinical_trials_viz.network import sponsor_drug_network
    from clinical_trials_viz.spec_builder import network_spec
    from clinical_trials_viz.verify import network_problems

    spec = network_spec(sponsor_drug_network(trials), AppliedFilters(), len(trials))
    edge = spec.data.edges[0]  # type: ignore[union-attr]
    sponsor = edge["source"].split(":", 1)[1]
    stranger = next(t for t in trials if t.lead_sponsor != sponsor)
    edge["trial_ids"].append(stranger.nct_id)
    problems = network_problems(spec.data, {t.nct_id: t for t in trials})  # type: ignore[arg-type]
    assert any(stranger.nct_id in p for p in problems)


async def test_network_needs_a_kind(make_client):
    no_kind = AnswerPlan(operation=Operation.RELATE)
    planner = ScriptedPlanner(no_kind, SPONSOR_DRUG)
    body = await ask(make_client, planner, query="network")
    assert planner.calls[1]["repair"] and "relate needs network" in planner.calls[1]["repair"][1][0]
    assert body["outcome"] == "success"


DRUG_DRUG = AnswerPlan(
    operation=Operation.RELATE, network=NetworkKind.DRUG_DRUG, filters=Filters(drugs=["pembrolizumab"])
)


async def test_drug_drug_network_links_only_same_arm_combinations(make_client, trials):
    from clinical_trials_viz.ctgov.trial import arm_drugs, drug_identities
    from clinical_trials_viz.network import same_arm_pairs

    async for client in make_client(ScriptedPlanner(DRUG_DRUG)):
        body = (await client.post("/v1/query", json={"query": "Which drugs are combined with pembrolizumab?"})).json()
        assert body["outcome"] == "success", body.get("message")
        spec = body["visualization"]
        assert spec["type"] == "network_graph" and not spec["metadata"]["bipartite"]
        assert {n["kind"] for n in spec["data"]["nodes"]} == {"drug"}
        assert all(e["kind"] == "same_arm" for e in spec["data"]["edges"])
        assert body["verification"]["passed"]
        assert any("same arm" in a for a in body["assumptions"])
        # Every cited trial's evidence names the arms that give each linked pair.
        by_id = {t.nct_id: t for t in trials}
        for edge in spec["data"]["edges"]:
            pair = " + ".join(sorted(x.split(":", 1)[1] for x in (edge["source"], edge["target"])))
            for nct_id in edge["trial_ids"]:
                arms = next(
                    v
                    for k, v in body["evidence"][nct_id]["fields"].items()
                    if k.startswith("protocolSection.armsInterventionsModule")
                )
                assert arms[pair]
                assert pair.split(" + ")[0] in drug_identities(by_id[nct_id])
        chart = await client.get(f"/v1/runs/{body['run_id']}/chart.png")
        assert chart.status_code == 200 and chart.content.startswith(b"\x89PNG")

    # Co-Listing is not a combination: a real trial whose drugs sit in different arms has no pair for them.
    colisted = [t for t in trials if len(drug_identities(t)) >= 2 and arm_drugs(t)
                and len(set(drug_identities(t))) > len({d for p in same_arm_pairs(t) for d in p})]  # fmt: skip
    assert colisted, "fixture should include a trial with drugs in different arms"


def test_verifier_rejects_a_same_arm_edge_without_a_shared_arm(trials):
    from clinical_trials_viz.models.response import AppliedFilters
    from clinical_trials_viz.network import drug_drug_network, same_arm_pairs
    from clinical_trials_viz.spec_builder import network_spec
    from clinical_trials_viz.verify import network_problems

    spec = network_spec(drug_drug_network(trials), AppliedFilters(), len(trials), NetworkKind.DRUG_DRUG)
    edge = spec.data.edges[0]  # type: ignore[union-attr]
    pair = tuple(sorted(x.split(":", 1)[1] for x in (edge["source"], edge["target"])))
    stranger = next(t for t in trials if pair not in same_arm_pairs(t))
    edge["trial_ids"].append(stranger.nct_id)
    assert any(stranger.nct_id in p for p in network_problems(spec.data, {t.nct_id: t for t in trials}))  # type: ignore[arg-type]


async def test_rate_limited_requests_are_retried(make_client, ctgov, page, monkeypatch):
    import httpx as _httpx

    from clinical_trials_viz.ctgov import client as ctgov_client

    async def no_sleep(_):
        return None

    monkeypatch.setattr(ctgov_client.asyncio, "sleep", no_sleep)
    ctgov.get("/studies").mock(
        side_effect=[_httpx.Response(429, headers={"Retry-After": "1"}), _httpx.Response(200, json=page)]
    )
    body = await ask(make_client, ScriptedPlanner(TREND), query="Keytruda per year")
    assert body["outcome"] == "success"


async def test_enrollment_histogram_end_to_end(make_client, trials):
    plan = AnswerPlan(operation=Operation.BIN, filters=Filters(drugs=["pembrolizumab"]))
    async for client in make_client(ScriptedPlanner(plan)):
        body = (await client.post("/v1/query", json={"query": "Enrollment distribution"})).json()
        assert body["outcome"] == "success", body.get("message")
        spec = body["visualization"]
        assert spec["type"] == "histogram"
        assert spec["metadata"]["bins"][0] == {"label": "0", "min": 0, "max": 0}
        assert set(spec["metadata"]["series_order"]) <= {"Actual", "Estimated", "Type not reported"}
        assert sum(d["trial_count"] for d in spec["data"]) == spec["metadata"]["cohort_size"]  # one bin per trial
        assert body["verification"]["passed"]
        entry = next(iter(body["evidence"].values()))
        assert "protocolSection.designModule.enrollmentInfo" in entry["fields"]
        chart = await client.get(f"/v1/runs/{body['run_id']}/chart.png")
        assert chart.status_code == 200 and chart.content.startswith(b"\x89PNG")


def test_histogram_verifier_catches_a_trial_in_the_wrong_bin(trials):
    from clinical_trials_viz.analyze import enrollment_histogram
    from clinical_trials_viz.models.response import AppliedFilters
    from clinical_trials_viz.models.spec import VisualizationType
    from clinical_trials_viz.spec_builder import build_evidence, histogram_spec
    from clinical_trials_viz.verify import verify

    spec = histogram_spec(enrollment_histogram(trials), AppliedFilters(), len(trials))
    by_id = {t.nct_id: t for t in trials}
    full = [d for d in spec.rows() if d["trial_ids"]]
    moved = full[0]["trial_ids"].pop()
    full[0]["trial_count"] -= 1
    target = next(d for d in spec.rows() if d["enrollment_bin"] != full[0]["enrollment_bin"])
    target["trial_ids"].append(moved)
    target["trial_count"] += 1
    result = verify(spec, build_evidence(spec, by_id, None, AppliedFilters()), by_id, None,
                    VisualizationType.HISTOGRAM, AppliedFilters())  # fmt: skip
    assert not next(c for c in result.checks if c.name == "cited_values_match_source").passed


async def test_trial_timeline_end_to_end(make_client, trials):
    plan = AnswerPlan(
        operation=Operation.PER_TRIAL, view=PerTrialView.TIMELINE, filters=Filters(drugs=["pembrolizumab"])
    )
    async for client in make_client(ScriptedPlanner(plan)):
        body = (await client.post("/v1/query", json={"query": "Timeline of pembrolizumab trials"})).json()
        assert body["outcome"] == "success", body.get("message")
        spec = body["visualization"]
        assert spec["type"] == "timeline"
        rows = spec["data"]
        assert 0 < len(rows) <= 50
        assert all(r["start"] <= r["end"] for r in rows)
        assert [r["start"] for r in rows] == sorted(r["start"] for r in rows)
        assert spec["encoding"]["x2"]["field"] == "end"
        assert body["verification"]["passed"]
        assert any("start date to the primary completion date" in a for a in body["assumptions"])
        entry = body["evidence"][rows[0]["nct_id"]]
        assert "protocolSection.statusModule.primaryCompletionDateStruct" in entry["fields"]
        chart = await client.get(f"/v1/runs/{body['run_id']}/chart.png")
        assert chart.status_code == 200 and chart.content.startswith(b"\x89PNG")


async def test_view_only_for_per_trial(make_client):
    bad = AnswerPlan(operation=Operation.AGGREGATE, view=PerTrialView.TIMELINE)
    planner = ScriptedPlanner(bad, TREND)
    await ask(make_client, planner, query="x")
    assert "view is only allowed" in planner.calls[1]["repair"][1][0]
