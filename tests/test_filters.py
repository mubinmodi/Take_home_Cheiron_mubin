"""The Filter module, through its interface, held to the behaviour recorded before it existed
(tests/fixtures/filter_behaviour.json, from the earlier build_params, filter_violations, describe_filters,
filter_dimensions, merge_filters, find_conflicts and suggest code), plus the sponsor rules it now owns."""

import json
from pathlib import Path

import pytest

from clinical_trials_viz.conflicts import find_conflicts
from clinical_trials_viz.filters import AppliedFilters
from clinical_trials_viz.models.plan import AnswerPlan, Filters, Operation
from clinical_trials_viz.models.request import QueryRequest

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "filter_behaviour.json").read_text())


@pytest.mark.parametrize("case", RECORDED["cases"], ids=lambda c: json.dumps(c["filters"])[:60])
def test_each_filter_combination_behaves_as_before(case, trials):
    filters = AppliedFilters(**case["filters"])
    drugs = filters.drugs or [None]
    assert {str(d): filters.api_params(d) for d in drugs} == case["params"]
    assert {t.nct_id: filters.violations(t) for t in trials} == case["violations"]
    assert filters.describe() == case["describe"]
    assert [d.value for d in filters.cited_dimensions()] == case["cited_dimensions"]
    assert [
        {"name": r.name, "relaxed": r.without.model_dump(mode="json"), "dropped": r.dropped, "pinned": r.pinned}
        for r in filters.relaxations()
    ] == case["corrections"]


@pytest.mark.parametrize("case", RECORDED["merges"], ids=lambda c: json.dumps(c["fields"])[:60])
def test_merging_structured_fields_and_finding_conflicts_behave_as_before(case):
    plan = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(**case["plan"]))
    request = QueryRequest(query="q", **case["fields"])
    assert AppliedFilters.merge(plan, request).model_dump(mode="json") == case["merged"]
    assert [[c.request_field, c.question_value, c.field_value] for c in find_conflicts(plan, request)] == case[
        "conflicts"
    ]


def test_sponsor_questions_are_answered_by_the_filter():
    term = AppliedFilters(sponsor="Merck")
    exact = AppliedFilters(sponsor="Merck KGaA", sponsor_exact=True, exact_sponsors=["Merck KGaA"])
    any_role = AppliedFilters(sponsor="Merck", sponsor_role="any")
    assert term.sponsor_is_ambiguous_term() and not exact.sponsor_is_ambiguous_term()
    assert not any_role.sponsor_is_ambiguous_term()  # "any role" searches collaborators too: nothing to narrow
    assert exact.exact_sponsor_names() == {"merck kgaa"} and term.exact_sponsor_names() == set()


def test_a_comparison_side_becomes_its_own_filters():
    base = AppliedFilters(phases=["PHASE3"])
    side = base.for_side(sponsor="Merck")
    assert (side.sponsor, side.sponsor_exact, side.phases) == ("Merck", False, ["PHASE3"])
    assert base.sponsor is None  # the base is not changed
