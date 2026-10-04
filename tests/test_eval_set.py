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
    if expect["kind"] == "answer":
        Operation(expect["operation"])
        if expect.get("group_by"):
            Dimension(expect["group_by"])
        Filters.model_validate(expect.get("filters", {}))
        for side in expect.get("compare_sides", []):
            ComparisonSide.model_validate(side)
    elif expect["kind"] == "clarify":
        ClarificationReason(expect["reason"])
    else:
        assert expect["kind"] == "unsupported"


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
