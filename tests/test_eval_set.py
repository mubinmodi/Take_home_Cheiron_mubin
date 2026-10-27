"""The eval sets themselves must be valid: requests parse, expected values use catalog names, and the
held-out set stays apart from the working set and the prompt."""

import pytest

from clinical_trials_viz.catalog import Dimension, OverallStatus, Phase
from clinical_trials_viz.models.plan import (
    AnswerPlan,
    ClarificationReason,
    ClarifyPlan,
    ComparisonSide,
    Filters,
    MultiAnswerPlan,
    Operation,
    UnsupportedPlan,
)
from clinical_trials_viz.models.request import QueryRequest
from evals.run import HELDOUT, load_cases, mismatches

CASES = load_cases()
HELDOUT_CASES = load_cases(HELDOUT)
ALL_CASES = CASES + HELDOUT_CASES


@pytest.mark.parametrize("case", ALL_CASES, ids=[c["id"] for c in ALL_CASES])
def test_case_is_well_formed(case):
    QueryRequest(query=case["question"], **case.get("fields", {}))
    expect = case["expect"]
    if expect["kind"] == "multi":
        assert 2 <= len(expect["parts"]) <= 3
        for part in expect["parts"]:
            _check_single({"kind": "answer", **part})
    else:
        _check_single(expect)


def _check_single(expect):
    if expect["kind"] == "answer":
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
    for options in _filter_options(expect.get("filters", {})):
        Filters.model_validate(options)
    for side in expect.get("compare_sides", []):
        ComparisonSide.model_validate(side)


def _filter_options(filters):
    """Every concrete filter set an expectation allows: each `one_of` value, and each `excludes` list
    (which must name real values too)."""
    plain = {k: v for k, v in filters.items() if v is not None and not isinstance(v, dict)}
    yield plain
    for key, want in filters.items():
        if isinstance(want, dict):
            assert set(want) <= {"one_of", "excludes"}, want
            for value in want.get("one_of", []) + ([want["excludes"]] if "excludes" in want else []):
                yield {**plain, key: value}


def test_ids_are_unique_across_both_sets():
    ids = [c["id"] for c in ALL_CASES]
    assert len(ids) == len(set(ids))


def test_heldout_questions_are_not_in_the_working_set():
    assert not {c["question"].lower() for c in HELDOUT_CASES} & {c["question"].lower() for c in CASES}


def test_heldout_questions_are_not_in_the_planner_prompt():
    from clinical_trials_viz.planner import SPLIT_INSTRUCTIONS, _instructions

    prompt = (_instructions() + SPLIT_INSTRUCTIONS).lower()
    assert not [c["id"] for c in HELDOUT_CASES if c["question"].lower() in prompt]


def test_scoring_accepts_alternatives_and_exclusions():
    plan = AnswerPlan(
        operation=Operation.AGGREGATE,
        filters=Filters(drugs=["pembrolizumb"], statuses=[OverallStatus.COMPLETED, OverallStatus.TERMINATED]),
    )
    assert mismatches(plan, {"filters": {"drugs": {"one_of": [["pembrolizumb"], ["pembrolizumab"]]}}}) == []
    assert mismatches(plan, {"filters": {"drugs": {"one_of": [["pembrolizumab"]]}}})
    assert mismatches(plan, {"filters": {"statuses": {"excludes": ["RECRUITING"]}}}) == []
    assert mismatches(plan, {"filters": {"statuses": {"excludes": ["COMPLETED"]}}})
    unfiltered = AnswerPlan(operation=Operation.AGGREGATE)
    assert mismatches(unfiltered, {"filters": {"statuses": {"excludes": ["RECRUITING"]}}})  # a dropped negation


def test_a_multi_part_expectation_can_expect_an_unsupported_part():
    plan = MultiAnswerPlan(
        requests=["a", "b"], parts=[AnswerPlan(operation=Operation.AGGREGATE), UnsupportedPlan(reason="x")]
    )
    assert mismatches(plan, {"kind": "multi", "parts": [{"operation": "aggregate"}, {"kind": "unsupported"}]}) == []
    assert mismatches(plan, {"kind": "multi", "parts": [{"operation": "aggregate"}, {"operation": "aggregate"}]})


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
