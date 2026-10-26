"""The answer module, through its interface: a Query Plan, the applied Filters and the Cohort in; a
verified answer, nothing to draw, or a failed Verification out. Real fixture records, no API."""

from dataclasses import replace

import pytest

from clinical_trials_viz import answer as answer_module
from clinical_trials_viz.answer import Answered, NothingToDraw, Unverified, answer
from clinical_trials_viz.catalog import DIMENSIONS, ENROLLMENT_FIELD, Dimension
from clinical_trials_viz.models.plan import AnswerPlan, ComparisonSide, Filters, NetworkKind, Operation, PerTrialView
from clinical_trials_viz.models.response import AppliedFilters
from clinical_trials_viz.models.spec import VisualizationType
from clinical_trials_viz.spec_builder import ARM_FIELD, START_FIELD

FILTERS = AppliedFilters(drugs=["pembrolizumab"])


def plan(**fields) -> AnswerPlan:
    return AnswerPlan(filters=Filters(drugs=["pembrolizumab"]), **fields)


@pytest.mark.parametrize(
    ("fields", "chart"),
    [
        ({"operation": Operation.AGGREGATE}, VisualizationType.SINGLE_VALUE),
        ({"operation": Operation.AGGREGATE, "group_by": Dimension.PHASE}, VisualizationType.BAR_CHART),
        ({"operation": Operation.AGGREGATE, "group_by": Dimension.START_YEAR}, VisualizationType.TIME_SERIES),
        (
            {"operation": Operation.AGGREGATE, "group_by": Dimension.START_YEAR, "series_by": Dimension.PHASE},
            VisualizationType.TIME_SERIES,
        ),
        ({"operation": Operation.BIN}, VisualizationType.HISTOGRAM),
        ({"operation": Operation.PER_TRIAL}, VisualizationType.TABLE),
        ({"operation": Operation.PER_TRIAL, "view": PerTrialView.TIMELINE}, VisualizationType.TIMELINE),
        ({"operation": Operation.PER_TRIAL, "view": PerTrialView.SCATTER}, VisualizationType.SCATTER_PLOT),
        ({"operation": Operation.RELATE, "network": NetworkKind.SPONSOR_DRUG}, VisualizationType.NETWORK_GRAPH),
    ],
)
def test_every_operation_comes_back_verified(trials, fields, chart):
    result = answer(plan(**fields), FILTERS, trials)
    assert isinstance(result, Answered), result
    assert result.spec.type is chart
    assert result.verification.passed
    cited = {i for d in result.spec.datums() for i in d["trial_ids"]}
    assert cited == set(result.evidence)


def test_a_comparison_answers_from_one_cohort_per_side(trials):
    sides = {"pembrolizumab": trials[:30], "nivolumab": trials[20:]}
    compare = plan(
        operation=Operation.COMPARE,
        group_by=Dimension.PHASE,
        compare_sides=[ComparisonSide(drug="pembrolizumab"), ComparisonSide(drug="nivolumab")],
    )
    result = answer(compare, FILTERS, sides)
    assert isinstance(result, Answered) and result.verification.passed
    assert result.spec.type is VisualizationType.GROUPED_BAR_CHART
    assert {d["group"] for d in result.spec.data} == {"pembrolizumab only", "nivolumab only", "Both"}


def test_the_evidence_cites_what_each_chart_relies_on(trials):
    """The wiring the pipeline used to assemble by hand: networks cite both entities, per-trial
    charts the dates and enrollment they plot."""
    network = answer(plan(operation=Operation.RELATE, network=NetworkKind.SPONSOR_DRUG), FILTERS, trials)
    assert isinstance(network, Answered)
    entry = next(iter(network.evidence.values()))
    assert DIMENSIONS[Dimension.LEAD_SPONSOR].source_field in entry.fields
    assert DIMENSIONS[Dimension.DRUG].source_field in entry.fields

    scatter = answer(plan(operation=Operation.PER_TRIAL, view=PerTrialView.SCATTER), FILTERS, trials)
    assert isinstance(scatter, Answered)
    fields = next(iter(scatter.evidence.values())).fields
    assert START_FIELD in fields and ENROLLMENT_FIELD in fields

    combos = answer(plan(operation=Operation.RELATE, network=NetworkKind.DRUG_DRUG), FILTERS, trials)
    if isinstance(combos, Answered):  # the saved page may have too few same-arm pairs for a link
        assert all(ARM_FIELD in e.fields for e in combos.evidence.values())


def test_a_trend_with_no_start_dates_has_nothing_to_draw(trials):
    undated = [replace(t, start_date=None, start_date_type=None) for t in trials]
    result = answer(plan(operation=Operation.AGGREGATE, group_by=Dimension.START_YEAR), FILTERS, undated)
    assert isinstance(result, NothingToDraw)
    assert "report a start date" in result.reason and "50 trials" in result.reason


def test_a_network_with_no_shared_trials_has_nothing_to_draw(trials):
    result = answer(plan(operation=Operation.RELATE, network=NetworkKind.SPONSOR_DRUG), FILTERS, trials[:1])
    assert isinstance(result, NothingToDraw)
    assert "no two entities share" in result.reason


def test_a_wrong_count_comes_back_unverified(trials, monkeypatch):
    real = answer_module.breakdown_spec

    def miscounted(*args, **kwargs):
        spec = real(*args, **kwargs)
        spec.data[0]["trial_count"] += 1
        return spec

    monkeypatch.setattr(answer_module, "breakdown_spec", miscounted)  # an internal seam of the module
    result = answer(plan(operation=Operation.AGGREGATE, group_by=Dimension.PHASE), FILTERS, trials)
    assert isinstance(result, Unverified)
    assert "counts_match_citations" in [c.name for c in result.verification.checks if not c.passed]
