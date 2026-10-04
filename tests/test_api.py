"""End-to-end through the HTTP API: scripted planner, mocked ClinicalTrials.gov, real everything else."""

import httpx
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

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
from tests.conftest import ScriptedPlanner, ask

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
        # The model sees three tools; multi-part messages are split by code before they reach it.
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

    from clinical_trials_viz.config import MODEL_TIERS, disallowed_openai_models

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


async def test_scatter_plot_end_to_end(make_client, trials):
    from clinical_trials_viz.spec_builder import duration_months

    plan = AnswerPlan(
        operation=Operation.PER_TRIAL, view=PerTrialView.SCATTER, filters=Filters(drugs=["pembrolizumab"])
    )
    async for client in make_client(ScriptedPlanner(plan)):
        body = (await client.post("/v1/query", json={"query": "Enrollment against duration"})).json()
        assert body["outcome"] == "success", body.get("message")
        spec = body["visualization"]
        assert spec["type"] == "scatter_plot" and spec["metadata"]["y_scale"] == "symlog"
        by_id = {t.nct_id: t for t in trials}
        for point in spec["data"]:
            trial = by_id[point["nct_id"]]
            assert point["enrollment"] == trial.enrollment
            assert point["duration_months"] == duration_months(trial) >= 0
        assert body["verification"]["passed"]
        chart = await client.get(f"/v1/runs/{body['run_id']}/chart.png")
        assert chart.status_code == 200 and chart.content.startswith(b"\x89PNG")


async def test_sponsor_clarification_offers_all_of_these_and_accepts_it(make_client, trials, monkeypatch):
    from collections import Counter

    from clinical_trials_viz import catalog, clarify

    # In the saved records no sponsor reaches the default 10% share, so lower it for this test.
    monkeypatch.setattr(clarify, "SPONSOR_AMBIGUITY_MIN_SHARE", 0.02)
    assert catalog.SPONSOR_AMBIGUITY_MIN_SHARE == 0.1
    by_sponsor = Counter(t.lead_sponsor for t in trials)
    plan = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(sponsor="Cancer"), group_by=Dimension.PHASE)
    async for client in make_client(ScriptedPlanner(plan, plan)):
        first = (await client.post("/v1/query", json={"query": "Phases of Cancer centre trials"})).json()
        assert first["outcome"] == "clarification_required"
        options = first["clarification"]["options"]
        everything = options[-1]
        assert everything["label"].startswith("All of these")
        assert everything["value"] == [o["value"] for o in options[:-1]]
        assert everything["trial_count"] == sum(by_sponsor[name] for name in everything["value"])

        answer = {"query": "Phases of Cancer centre trials", "sponsor": everything["value"],
                  "previous_run_id": first["run_id"]}  # fmt: skip
        second = (await client.post("/v1/query", json=answer)).json()
        assert second["outcome"] == "success", second.get("message")
        assert second["applied_filters"]["exact_sponsors"] == everything["value"]
        assert second["visualization"]["metadata"]["cohort_size"] == everything["trial_count"]
        assert second["verification"]["passed"]


async def test_idempotency_key_replays_without_running_again(make_client):
    planner = ScriptedPlanner(TREND, TREND)
    async for client in make_client(planner):
        body = {"query": "Keytruda trials per year"}
        headers = {"Idempotency-Key": "retry-123"}
        first = await client.post("/v1/query", json=body, headers=headers)
        again = await client.post("/v1/query", json={"query": "  Keytruda trials per year "}, headers=headers)
        assert first.status_code == again.status_code == 200
        assert again.json()["run_id"] == first.json()["run_id"]
        assert again.json() == first.json()
        assert again.headers["Idempotent-Replayed"] == "true"
        assert "Idempotent-Replayed" not in first.headers
        assert len(planner.calls) == 1  # the model was not called again

        reused = await client.post("/v1/query", json={"query": "something else"}, headers=headers)
        assert reused.status_code == 422

        no_key_a = (await client.post("/v1/query", json=body)).json()
        assert no_key_a["run_id"] != first.json()["run_id"]  # without a key every POST is a new run


async def test_idempotency_key_in_progress_is_409_and_failures_can_retry(make_client):
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()

    class SlowPlanner(ScriptedPlanner):
        async def plan(self, *args, **kwargs):
            started.set()
            await release.wait()
            return await super().plan(*args, **kwargs)

    async for client in make_client(SlowPlanner(TREND, TREND)):
        headers = {"Idempotency-Key": "slow-1"}
        first = asyncio.create_task(client.post("/v1/query", json={"query": "q"}, headers=headers))
        await started.wait()
        busy = await client.post("/v1/query", json={"query": "q"}, headers=headers)
        assert busy.status_code == 409
        release.set()
        assert (await first).status_code == 200

        missing = {"query": "q2", "previous_run_id": "run_" + "0" * 32}
        assert (await client.post("/v1/query", json=missing, headers={"Idempotency-Key": "k2"})).status_code == 404
        # A request that failed before producing a run does not consume its key: a corrected retry runs.
        retry = await client.post("/v1/query", json={"query": "q"}, headers={"Idempotency-Key": "k2"})
        assert retry.status_code == 200 and retry.json()["outcome"] == "success"


async def test_refining_follow_up_keeps_earlier_structured_fields_new_topic_does_not(make_client, trials):
    sponsor = next(t.lead_sponsor for t in trials if t.lead_sponsor)
    first_plan = AnswerPlan(operation=Operation.AGGREGATE, group_by=Dimension.PHASE)
    refine = AnswerPlan(relation="refine", operation=Operation.AGGREGATE, group_by=Dimension.START_YEAR)
    new_topic = AnswerPlan(relation="new", operation=Operation.AGGREGATE, group_by=Dimension.PHASE)
    async for client in make_client(ScriptedPlanner(first_plan, refine, new_topic)):
        first = (await client.post("/v1/query", json={"query": "phases", "sponsor": [sponsor]})).json()
        assert first["applied_filters"]["exact_sponsors"] == [sponsor]

        refined = (
            await client.post("/v1/query", json={"query": "by year instead", "previous_run_id": first["run_id"]})
        ).json()
        assert refined["applied_filters"]["exact_sponsors"] == [sponsor]  # the Clarification-style answer survives
        assert any(a.startswith("Kept from the earlier question: sponsor") for a in refined["assumptions"])
        record = (await client.get(f"/v1/runs/{refined['run_id']}")).json()
        assert record["request"]["sponsor"] == [sponsor]  # saved, so a further Follow-up inherits it too

        fresh = (
            await client.post("/v1/query", json={"query": "something else", "previous_run_id": first["run_id"]})
        ).json()
        assert not fresh["applied_filters"]["exact_sponsors"]


async def test_vega_lite_endpoint_maps_marks_to_datums_and_page_is_served(make_client):
    async for client in make_client(ScriptedPlanner(TREND, SPONSOR_DRUG)):
        trend = (await client.post("/v1/query", json={"query": "per year"})).json()
        vl = (await client.get(f"/v1/runs/{trend['run_id']}/vega-lite.json")).json()
        rows = trend["visualization"]["data"]
        for value in vl["data"]["values"]:
            assert rows[value["_datum"]]["start_year"] == value["start_year"]

        network = (await client.post("/v1/query", json={"query": "network"})).json()
        vl = (await client.get(f"/v1/runs/{network['run_id']}/vega-lite.json")).json()
        datums = network["visualization"]["data"]["nodes"] + network["visualization"]["data"]["edges"]
        edge_layer, node_layer = vl["layer"][0]["data"]["values"], vl["layer"][1]["data"]["values"]
        assert all("source" in datums[e["_datum"]] for e in edge_layer)
        assert all(datums[n["_datum"]]["trial_count"] == n["trial_count"] for n in node_layer)

        page = await client.get("/")
        assert page.status_code == 200 and "Clinical Trials Explorer" in page.text
        assert ".innerHTML" not in page.text  # registry text is always inserted as text, never as markup


async def test_multi_part_question_is_split_by_code_and_each_part_planned_alone(make_client):
    parts = [
        AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(sponsor="Pfizer")),
        AnswerPlan(
            operation=Operation.AGGREGATE, filters=Filters(conditions=["lung cancer"]), group_by=Dimension.PHASE
        ),
    ]
    planner = ScriptedPlanner(*parts)
    question = "How many trials does Pfizer sponsor, and what phases are lung cancer trials in?"
    async for client in make_client(planner):
        body = (await client.post("/v1/query", json={"query": question})).json()
        # Each request reached the model on its own, with the full message only as context.
        assert [c["question"] for c in planner.calls] == [
            "How many trials does Pfizer sponsor",
            "what phases are lung cancer trials in",
        ]
        assert all(c["context"] == question for c in planner.calls)
        assert body["model_calls"] == 2
        assert body["plan"]["kind"] == "multi" and len(body["plan"]["parts"]) == 2
        assert body["assumptions"][0].startswith("The question asks 2 separate things")
        second = body["additional_answers"][0]
        assert second["visualization"]["type"] == "bar_chart"
        assert second["applied_filters"]["conditions"] == ["lung cancer"] and not second["applied_filters"].get(
            "sponsor"
        )
        assert second["verification"]["passed"]
        assert second["chart_url"].endswith("chart.png?part=1")
        chart = await client.get(f"/v1/runs/{body['run_id']}/chart.png", params={"part": 1})
        assert chart.status_code == 200 and chart.content.startswith(b"\x89PNG")
        assert (await client.get(f"/v1/runs/{body['run_id']}/vega-lite.json", params={"part": 1})).status_code == 200
        assert (await client.get(f"/v1/runs/{body['run_id']}/chart.png", params={"part": 2})).status_code == 404


async def test_parts_fail_independently(make_client, ctgov, page):
    parts = [TREND, AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(conditions=["cancer"]))]

    def studies(request):
        if "query.cond" in request.url.params:  # the second part matches too many trials
            return httpx.Response(200, json={"totalCount": 99_000, "studies": [], "nextPageToken": "t"})
        return httpx.Response(200, json=page)

    ctgov.get("/studies").mock(side_effect=studies)
    body = await ask(
        make_client, ScriptedPlanner(*parts), query="Keytruda trials per year; and show cancer trials by phase"
    )
    assert body["outcome"] == "success"
    assert body["additional_answers"][0]["outcome"] == "scope_required"


async def test_follow_ups_are_not_split(make_client):
    planner = ScriptedPlanner(TREND, TREND)
    async for client in make_client(planner):
        first = (await client.post("/v1/query", json={"query": "Keytruda per year"})).json()
        await client.post(
            "/v1/query", json={"query": "only phase 3, and show it by country", "previous_run_id": first["run_id"]}
        )
        assert len(planner.calls) == 2 and planner.calls[1]["context"] is None


async def test_series_by_crosses_two_dimensions_in_one_verified_chart(make_client, trials):
    from clinical_trials_viz.ctgov.trial import dimension_values

    plan = AnswerPlan(operation=Operation.AGGREGATE, group_by=Dimension.START_YEAR, series_by=Dimension.PHASE)
    async for client in make_client(ScriptedPlanner(plan)):
        body = (await client.post("/v1/query", json={"query": "phases per year"})).json()
        assert body["outcome"] == "success", body.get("message")
        spec = body["visualization"]
        assert spec["type"] == "time_series" and spec["encoding"]["color"]["field"] == "phase"
        assert (
            spec["metadata"]["series_order"][0] == "Early Phase 1" or spec["metadata"]["series_order"][0] == "Phase 1"
        )
        by_id = {t.nct_id: t for t in trials}
        for d in spec["data"]:
            for nct_id in d["trial_ids"]:
                assert d["phase"] in dimension_values(by_id[nct_id], Dimension.PHASE)
        assert body["verification"]["passed"]
        assert "protocolSection.designModule.phases" in next(iter(body["evidence"].values()))["fields"]
        chart = await client.get(f"/v1/runs/{body['run_id']}/chart.png")
        assert chart.status_code == 200


async def test_series_by_rules_are_gated(make_client):
    bad = AnswerPlan(operation=Operation.AGGREGATE, group_by=Dimension.PHASE, series_by=Dimension.PHASE)
    planner = ScriptedPlanner(bad, TREND)
    await ask(make_client, planner, query="x")
    assert "series_by must differ from group_by" in planner.calls[1]["repair"][1][0]


async def test_clarification_for_one_part_is_answered_as_that_part_alone(make_client):
    from clinical_trials_viz.models.plan import ClarificationReason, ClarifyPlan

    clarify = ClarifyPlan(reason=ClarificationReason.MISSING_REFERENCE, field="drug", question="Which drug?")
    phases = AnswerPlan(
        operation=Operation.AGGREGATE, filters=Filters(conditions=["lung cancer"]), group_by=Dimension.PHASE
    )
    planner = ScriptedPlanner(clarify, phases, TREND)
    async for client in make_client(planner):
        first = (
            await client.post(
                "/v1/query", json={"query": "Show trials of this drug per year, and show lung cancer trials by phase"}
            )
        ).json()
        assert first["outcome"] == "clarification_required"
        assert first["additional_answers"][0]["outcome"] == "success"
        part = first["plan"]["requests"][0]
        answer = {"query": part, "drug_name": "Keytruda", "previous_run_id": first["run_id"]}
        second = (await client.post("/v1/query", json=answer)).json()
        assert second["outcome"] == "success" and not second.get("additional_answers")
        assert planner.calls[2]["question"] == part and planner.calls[2]["previous"] is None  # planned fresh


async def test_keywords_are_free_text_and_reported(make_client, ctgov):
    plan = AnswerPlan(
        operation=Operation.AGGREGATE,
        filters=Filters(conditions=["COVID-19"], keywords=["vaccine"]),
        group_by=Dimension.STATUS,
    )
    body = await ask(make_client, ScriptedPlanner(plan), query="Status breakdown of COVID-19 vaccine trials")
    assert body["outcome"] == "success"
    sent = ctgov.calls[-1].request.url.params
    assert sent["query.cond"] == "(COVID-19)" and sent["query.term"] == "(vaccine)"
    assert any("free text" in a and "'vaccine'" in a for a in body["assumptions"])
    assert "'vaccine'" in body["visualization"]["title"]


async def test_sponsor_categories_are_not_sponsor_names(make_client):
    bad = AnswerPlan(
        operation=Operation.COMPARE,
        compare_sides=[ComparisonSide(sponsor="Industry"), ComparisonSide(sponsor="Academic")],
    )
    good = AnswerPlan(
        operation=Operation.COMPARE,
        group_by=Dimension.SPONSOR_CLASS,
        compare_sides=[ComparisonSide(condition="Parkinson's disease"), ComparisonSide(condition="ALS")],
    )
    planner = ScriptedPlanner(bad, good)
    body = await ask(
        make_client, planner, query="Industry vs academic sponsorship: compare Parkinson's disease and ALS"
    )
    errors = planner.calls[1]["repair"][1]
    assert any("'Industry' is a sponsor category" in e and "sponsor_class" in e for e in errors)
    assert body["outcome"] == "success" and body["visualization"]["type"] == "grouped_bar_chart"
