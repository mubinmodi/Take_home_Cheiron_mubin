"""Investigator ↔ drug and site ↔ drug networks (ticket 15)."""

import copy

import pytest

from clinical_trials_viz.ctgov.trial import clean_person_name
from clinical_trials_viz.models.plan import AnswerPlan, Filters, NetworkKind, Operation
from clinical_trials_viz.spec_builder import INVESTIGATOR_FIELD, SITE_FIELD
from tests.conftest import ScriptedPlanner

OFFICIALS = [["David A. Reardon, MD"], ["David Reardon, MD", "Novartis Pharmaceuticals"], ["Patrick Wen, MD, PhD"]]
FACILITIES = [["Dana-Farber Cancer Institute", "Duke  University Medical Center"], ["Dana-Farber Cancer Institute"]]


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("David A. Reardon, MD", "David Reardon"),
        ("Dr. Kevin A Camphausen, M.D.", "Kevin Camphausen"),
        ("Annick Desjardins, MD, FRCPC", "Annick Desjardins"),
        ("Novartis Pharmaceuticals", None),
        ("Clinical Trials", None),
        ("Call 1-877-CTLILLY (1-877-285-4559)", None),
        ("Bristol-Myers Squibb", None),
        ("Madonna", None),
    ],
)
def test_investigator_names_are_cleaned(raw, clean):
    assert clean_person_name(raw) == clean


def _with_people_and_sites(page):
    rich = copy.deepcopy(page)
    for i, study in enumerate(rich["studies"]):
        module = study["protocolSection"].setdefault("contactsLocationsModule", {})
        module["overallOfficials"] = [{"name": n} for n in OFFICIALS[i % len(OFFICIALS)]]
        module.setdefault("locations", []).extend({"facility": f} for f in FACILITIES[i % len(FACILITIES)])
    return rich


@pytest.mark.parametrize(
    ("kind", "entity", "field", "expected"),
    [
        (NetworkKind.INVESTIGATOR_DRUG, "investigator", "OverallOfficialName", "David Reardon"),
        (NetworkKind.SITE_DRUG, "site", "LocationFacility", "Dana-Farber Cancer Institute"),
    ],
)
async def test_people_and_site_networks_are_cited_and_verified(make_client, ctgov, page, kind, entity, field, expected):
    ctgov.get("/studies").respond(json=_with_people_and_sites(page))
    plan = AnswerPlan(operation=Operation.RELATE, network=kind, filters=Filters(drugs=["pembrolizumab"]))
    async for client in make_client(ScriptedPlanner(plan)):
        body = (await client.post("/v1/query", json={"query": f"Network of {entity}s and drugs"})).json()
        assert body["outcome"] == "success", body.get("message")
        assert body["verification"]["passed"]
        spec = body["visualization"]
        assert spec["metadata"]["node_kinds"] == [entity, "drug"] and spec["metadata"]["bipartite"]
        labels = [n["label"] for n in spec["data"]["nodes"] if n["kind"] == entity]
        assert expected in labels
        assert "Novartis Pharmaceuticals" not in labels  # an organization listed as an official is not a person
        assert field in ctgov.calls[-1].request.url.params["fields"]
        node = next(n for n in spec["data"]["nodes"] if n["label"] == expected)
        cited = body["evidence"][node["trial_ids"][0]]["fields"]
        assert expected in cited[INVESTIGATOR_FIELD if entity == "investigator" else SITE_FIELD]
        chart = await client.get(f"/v1/runs/{body['run_id']}/chart.png")
        assert chart.status_code == 200
