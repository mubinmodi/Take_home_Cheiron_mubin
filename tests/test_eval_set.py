"""The eval set itself must be valid: requests parse, and expected values use catalog names."""

import pytest

from clinical_trials_viz.catalog import Dimension, OverallStatus, Phase
from clinical_trials_viz.models.plan import (
    AnswerPlan,
    ClarificationReason,
    ClarifyPlan,
    ComparisonSide,
    Filters,
    Operation,
    UnsupportedPlan,
)
from clinical_trials_viz.models.request import QueryRequest
from evals.run import load_cases, mismatches

CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case_is_well_formed(case):
    QueryRequest(query=case["question"], **case.get("fields", {}))
    expect = case["expect"]
    if expect["kind"] == "multi":
        assert 2 <= len(expect["parts"]) <= 3
        for part in expect["parts"]:
            _check_answer(part)
    elif expect["kind"] == "answer":
        _check_answer(expect)
    elif expect["kind"] == "clarify":
        ClarificationReason(expect["reason"])
    else:
        assert expect["kind"] == "unsupported"


def _check_answer(expect):
    Operation(expect["operation"])
    for key in ("group_by", "series_by"):
        if expect.get(key):
            Dimension(expect[key])
    Filters.model_validate({k: v for k, v in expect.get("filters", {}).items() if v is not None})
    for side in expect.get("compare_sides", []):
        ComparisonSide.model_validate(side)


def test_ids_are_unique():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))


def test_scoring_compares_names_loosely_and_values_strictly():
    plan = AnswerPlan(
        operation=Operation.AGGREGATE,
        group_by=Dimension.PHASE,
        filters=Filters(conditions=["Breast Cancer"], statuses=[OverallStatus.RECRUITING], phases=[Phase.PHASE3]),
    )
    assert mismatches(plan, {"kind": "answer", "group_by": "phase", "filters": {"conditions": ["breast cancer"]}}) == []
    assert mismatches(plan, {"group_by": "start_year"})
    assert mismatches(plan, {"filters": {"statuses": ["COMPLETED"]}})
    assert mismatches(
        ClarifyPlan(reason=ClarificationReason.DRUG_CLASS, field="drug", question="?"), {"kind": "answer"}
    )
    assert mismatches(UnsupportedPlan(reason="x"), {"kind": "unsupported"}) == []
