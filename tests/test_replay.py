"""Run bundles and exact replay (ticket 14)."""

from clinical_trials_viz.catalog import Dimension
from clinical_trials_viz.models.plan import AnswerPlan, ComparisonSide, Filters, Operation
from clinical_trials_viz.replay import BundleStore, replay
from clinical_trials_viz.runs import FileRunStore
from tests.conftest import ScriptedPlanner


async def _answer(make_client, planner, body):
    async for client in make_client(planner):
        response = await client.post("/v1/query", json=body)
        return response.json()


async def test_a_run_replays_offline_to_the_same_answer(make_client, settings, ctgov):
    plan = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(drugs=["pembrolizumab"]), group_by=Dimension.PHASE)
    body = await _answer(make_client, ScriptedPlanner(plan), {"query": "Pembrolizumab trials by phase"})
    assert body["outcome"] == "success"

    runs = FileRunStore(settings.runs_dir)
    record = await runs.load(body["run_id"])
    bundle = BundleStore(settings.bundles_dir).load(body["run_id"])
    assert record and bundle and any(r["path"] == "/studies" for r in bundle["responses"])

    calls_before = len(ctgov.calls)
    result = await replay(record, bundle, runs, settings.max_pages)
    assert result.differences == []
    assert len(ctgov.calls) == calls_before  # nothing went online
    assert result.replayed.visualization == result.original.visualization


async def test_replay_reports_a_changed_answer(make_client, settings):
    plan = AnswerPlan(
        operation=Operation.COMPARE,
        group_by=Dimension.PHASE,
        compare_sides=[ComparisonSide(drug="pembrolizumab"), ComparisonSide(drug="nivolumab")],
    )
    body = await _answer(make_client, ScriptedPlanner(plan), {"query": "Pembrolizumab vs nivolumab phases"})
    runs = FileRunStore(settings.runs_dir)
    record = await runs.load(body["run_id"])
    bundle = BundleStore(settings.bundles_dir).load(body["run_id"])
    assert record and bundle
    first = next(r for r in bundle["responses"] if r["path"] == "/studies")
    first["body"]["studies"] = first["body"]["studies"][:5]  # the registry "changed"
    result = await replay(record, bundle, runs, settings.max_pages)
    assert any("visualization differs" in d for d in result.differences)
