"""The age-group Dimension, added as the worked example of extending the catalog (docs/project-guide.md §6)."""

import copy
from dataclasses import replace

from clinical_trials_viz.analyze import breakdown
from clinical_trials_viz.catalog import NOT_REPORTED, Dimension
from clinical_trials_viz.ctgov.trial import dimension_evidence, dimension_values, parse_trial
from clinical_trials_viz.models.plan import AnswerPlan, Filters, Operation
from tests.conftest import ScriptedPlanner, ask

AGES = [["ADULT", "OLDER_ADULT"], ["CHILD", "ADULT"], ["ADULT"], []]


def test_age_groups_are_read_from_eligibility():
    trial = parse_trial(
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT00000001"},
                "eligibilityModule": {"stdAges": ["CHILD", "ADULT"]},
            }
        }
    )
    assert dimension_values(trial, Dimension.AGE_GROUP) == ("Child (under 18)", "Adult (18–64)")
    assert dimension_evidence(trial, Dimension.AGE_GROUP) == ["CHILD", "ADULT"]
    assert dimension_values(replace(trial, std_ages=()), Dimension.AGE_GROUP) == (NOT_REPORTED,)


def test_age_groups_count_each_trial_under_each_group_in_age_order(trials):
    aged = [replace(t, std_ages=tuple(AGES[i % len(AGES)])) for i, t in enumerate(trials)]
    labels = [b.label for b in breakdown(aged, Dimension.AGE_GROUP, None).buckets]
    assert labels == ["Child (under 18)", "Adult (18–64)", "Older adult (65+)", NOT_REPORTED]
    adult = next(b for b in breakdown(aged, Dimension.AGE_GROUP, None).buckets if b.label == "Adult (18–64)")
    assert len(adult.trial_ids) == sum(1 for t in aged if "ADULT" in t.std_ages)


async def test_age_group_chart_is_cited_and_verified(make_client, ctgov, page):
    aged = copy.deepcopy(page)
    for i, study in enumerate(aged["studies"]):
        study["protocolSection"]["eligibilityModule"] = {"stdAges": AGES[i % len(AGES)]}
    ctgov.get("/studies").respond(json=aged)
    plan = AnswerPlan(
        operation=Operation.AGGREGATE, filters=Filters(drugs=["pembrolizumab"]), group_by=Dimension.AGE_GROUP
    )
    body = await ask(make_client, ScriptedPlanner(plan), query="Pembrolizumab trials by age group")
    assert body["outcome"] == "success"
    assert body["visualization"]["type"] == "bar_chart"
    assert body["verification"]["passed"]
    child = next(d for d in body["visualization"]["data"] if d["age_group"] == "Child (under 18)")
    cited = body["evidence"][child["trial_ids"][0]]["fields"]
    assert "CHILD" in cited["protocolSection.eligibilityModule.stdAges"]
