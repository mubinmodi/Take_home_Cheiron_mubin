"""Request validation, API parameter compilation, spec building, verification and rendering."""

import pytest
from pydantic import ValidationError

from clinical_trials_viz import clarify
from clinical_trials_viz.analyze import breakdown
from clinical_trials_viz.catalog import Dimension, OverallStatus, Phase
from clinical_trials_viz.cohort import build_params
from clinical_trials_viz.models.plan import AnswerPlan, Filters, Operation
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.models.response import AppliedFilters
from clinical_trials_viz.models.spec import VisualizationType
from clinical_trials_viz.render import NotRenderable, render, to_vega_lite
from clinical_trials_viz.spec_builder import breakdown_spec, build_evidence, single_value_spec, table_spec
from clinical_trials_viz.validate import check_plan
from clinical_trials_viz.verify import verify


class TestRequest:
    def test_query_only(self):
        assert QueryRequest(query="  How many trials?  ").query == "How many trials?"

    def test_single_values_become_lists(self):
        r = QueryRequest(query="q", drug_name="Keytruda", trial_phase="phase3", status="recruiting")
        assert r.drug_name == ["Keytruda"]
        assert r.trial_phase == [Phase.PHASE3]
        assert r.status == [OverallStatus.RECRUITING]

    @pytest.mark.parametrize(
        "fields",
        [
            {"query": ""},
            {"query": "q", "unknown_field": 1},
            {"query": "q", "nct_id": "NCT123"},
            {"query": "q", "start_year": 2022, "end_year": 2020},
            {"query": "q", "trial_phase": "PHASE9"},
        ],
    )
    def test_rejects_invalid(self, fields):
        with pytest.raises(ValidationError):
            QueryRequest(**fields)


def test_build_params_compiles_every_filter():
    f = AppliedFilters(
        conditions=["lung cancer"],
        phases=[Phase.PHASE2, Phase.PHASE3],
        statuses=[OverallStatus.RECRUITING],
        sponsor="Merck",
        countries=["United States"],
        start_year_from=2020,
        nct_ids=["NCT02578680"],
    )
    params = build_params(f, drug="Keytruda")
    assert params["query.intr"] == "Keytruda"
    assert params["query.cond"] == "(lung cancer)"
    assert params["filter.overallStatus"] == "RECRUITING"
    assert params["filter.ids"] == "NCT02578680"
    assert params["filter.advanced"] == (
        "AREA[LeadSponsorName](Merck) AND AREA[Phase](PHASE2 OR PHASE3) AND "
        'AREA[LocationCountry]("United States") AND AREA[StartDate]RANGE[2020-01-01,MAX]'
    )


def test_exact_and_any_sponsor_roles():
    exact = build_params(AppliedFilters(sponsor="Merck Sharp & Dohme LLC", sponsor_exact=True))
    assert exact["filter.advanced"] == 'AREA[LeadSponsorName]("Merck Sharp & Dohme LLC")'
    both = build_params(AppliedFilters(sponsor="x", sponsor_exact=True, exact_sponsors=["A Inc", "B AG"]))
    assert both["filter.advanced"] == 'AREA[LeadSponsorName]("A Inc" OR "B AG")'
    assert build_params(AppliedFilters(sponsor="Merck", sponsor_role="any")) == {"query.spons": "Merck"}


def _time_series(trials):
    spec = breakdown_spec(
        breakdown(trials, Dimension.START_YEAR, None), AppliedFilters(drugs=["pembrolizumab"]), len(trials)
    )
    return spec, build_evidence(spec, {t.nct_id: t for t in trials}, Dimension.START_YEAR, AppliedFilters())


def test_time_series_spec_verifies(trials):
    spec, evidence = _time_series(trials)
    assert spec.type is VisualizationType.TIME_SERIES
    result = verify(
        spec,
        evidence,
        {t.nct_id: t for t in trials},
        Dimension.START_YEAR,
        VisualizationType.TIME_SERIES,
        AppliedFilters(),
    )
    assert result.passed, result.checks


def test_verifier_catches_a_wrong_count(trials):
    spec, evidence = _time_series(trials)
    datum = next(d for d in spec.data if d["trial_count"])
    datum["trial_count"] += 1
    result = verify(
        spec,
        evidence,
        {t.nct_id: t for t in trials},
        Dimension.START_YEAR,
        VisualizationType.TIME_SERIES,
        AppliedFilters(),
    )
    assert not result.passed
    assert not next(c for c in result.checks if c.name == "counts_match_citations").passed


def test_verifier_catches_a_trial_in_the_wrong_bucket(trials):
    spec, evidence = _time_series(trials)
    a, b = [d for d in spec.data if d["trial_ids"] and d["start_year"] != "Not reported"][:2]
    moved = a["trial_ids"].pop()
    a["trial_count"] -= 1
    b["trial_ids"].append(moved)
    b["trial_count"] += 1
    result = verify(
        spec,
        evidence,
        {t.nct_id: t for t in trials},
        Dimension.START_YEAR,
        VisualizationType.TIME_SERIES,
        AppliedFilters(),
    )
    assert not next(c for c in result.checks if c.name == "cited_values_match_source").passed


def test_verifier_catches_a_chart_grouped_by_the_wrong_field(trials):
    """The plan asked for phases; a bar chart grouped by status must not pass."""
    by_id = {t.nct_id: t for t in trials}
    spec = breakdown_spec(breakdown(trials, Dimension.STATUS, None), AppliedFilters(), len(trials))
    evidence = build_evidence(spec, by_id, Dimension.STATUS, AppliedFilters())
    result = verify(spec, evidence, by_id, Dimension.PHASE, VisualizationType.BAR_CHART, AppliedFilters())
    assert not result.passed
    assert not next(c for c in result.checks if c.name == "answers_plan").passed


def test_verifier_catches_an_altered_citation_value(trials):
    spec, evidence = _time_series(trials)
    entry = next(iter(evidence.values()))
    entry.fields["protocolSection.statusModule.startDateStruct"] = {"date": "1999-01-01", "type": "ACTUAL"}
    result = verify(
        spec,
        evidence,
        {t.nct_id: t for t in trials},
        Dimension.START_YEAR,
        VisualizationType.TIME_SERIES,
        AppliedFilters(),
    )
    assert not next(c for c in result.checks if c.name == "evidence_matches_source").passed


def test_verifier_catches_an_altered_citation_link(trials):
    spec, evidence = _time_series(trials)
    entry = next(iter(evidence.values()))
    entry.url = "https://example.com/not-the-registry"
    by_id = {t.nct_id: t for t in trials}
    result = verify(spec, evidence, by_id, Dimension.START_YEAR, VisualizationType.TIME_SERIES, AppliedFilters())
    assert not next(c for c in result.checks if c.name == "evidence_matches_source").passed


def test_verifier_catches_a_trial_wrongly_counted_in_other(trials):
    by_id = {t.nct_id: t for t in trials}
    spec = breakdown_spec(breakdown(trials, Dimension.COUNTRY, 3), AppliedFilters(), len(trials))
    us = next(d for d in spec.data if d["country"] == "United States")
    other = next(d for d in spec.data if d["country"] == "Other")
    moved = next(i for i in us["trial_ids"] if by_id[i].countries == ("United States",))
    other["trial_ids"].append(moved)
    other["trial_count"] += 1
    evidence = build_evidence(spec, by_id, Dimension.COUNTRY, AppliedFilters())
    result = verify(spec, evidence, by_id, Dimension.COUNTRY, VisualizationType.BAR_CHART, AppliedFilters())
    assert not next(c for c in result.checks if c.name == "cited_values_match_source").passed


def test_verifier_catches_a_count_that_leaves_out_part_of_the_cohort(trials):
    cohort = {t.nct_id: t for t in trials[:2]}
    spec = single_value_spec(list(cohort.values()), AppliedFilters())
    datum = spec.data[0]
    datum["trial_ids"] = datum["trial_ids"][:1]
    datum["trial_count"] = 1
    evidence = build_evidence(spec, cohort, None, AppliedFilters())
    result = verify(spec, evidence, cohort, None, VisualizationType.SINGLE_VALUE, AppliedFilters())
    assert not next(c for c in result.checks if c.name == "cohort_covered").passed


def test_verifier_catches_a_bar_chart_that_drops_a_category(trials):
    by_id = {t.nct_id: t for t in trials}
    spec = breakdown_spec(breakdown(trials, Dimension.PHASE, None), AppliedFilters(), len(trials))
    spec.data = [d for d in spec.data if d["phase"] != "Phase 1"]
    evidence = build_evidence(spec, by_id, Dimension.PHASE, AppliedFilters())
    result = verify(spec, evidence, by_id, Dimension.PHASE, VisualizationType.BAR_CHART, AppliedFilters())
    assert not next(c for c in result.checks if c.name == "cohort_covered").passed


def test_evidence_cites_the_source_field(trials):
    spec, evidence = _time_series(trials)
    entry = evidence[spec.data[-1]["trial_ids"][0]]
    assert entry.url.startswith("https://clinicaltrials.gov/study/NCT")
    assert "protocolSection.statusModule.startDateStruct" in entry.fields


def test_renders_png_and_svg(trials):
    spec, _ = _time_series(trials)
    assert render(spec, "png").startswith(b"\x89PNG")
    assert b"<svg" in render(spec, "svg")


def test_vega_lite_gets_finished_values_only(trials):
    spec, _ = _time_series(trials)
    vl = to_vega_lite(spec)
    assert "transform" not in vl
    assert all("trial_ids" not in row for row in vl["data"]["values"])


def test_single_value_and_table(trials):
    single = single_value_spec(trials, AppliedFilters())
    assert single.data[0]["trial_count"] == len(trials)
    assert render(single, "png").startswith(b"\x89PNG")
    table, _ = table_spec(trials, AppliedFilters())
    assert table.metadata.total_rows == len(trials)
    with pytest.raises(NotRenderable):
        render(table, "png")


def test_filtered_answer_cites_and_checks_filter_fields(trials):
    phase3 = [t for t in trials if "PHASE3" in t.phases]
    filters = AppliedFilters(drugs=["pembrolizumab"], phases=[Phase.PHASE3])
    spec = single_value_spec(phase3, filters)
    by_id = {t.nct_id: t for t in phase3}  # the cohort, as the pipeline passes it
    evidence = build_evidence(spec, by_id, None, filters)
    entry = evidence[phase3[0].nct_id]
    assert "PHASE3" in entry.fields["protocolSection.designModule.phases"]
    assert "pembrolizumab" in entry.fields["derivedSection.interventionBrowseModule.meshes.term"]
    assert verify(spec, evidence, by_id, None, VisualizationType.SINGLE_VALUE, filters).passed

    # A trial that fails the phase filter must not be counted.
    other = next(t for t in trials if "PHASE3" not in t.phases)
    bad = single_value_spec([*phase3, other], filters)
    by_id = {t.nct_id: t for t in [*phase3, other]}
    result = verify(
        bad, build_evidence(bad, by_id, None, filters), by_id, None, VisualizationType.SINGLE_VALUE, filters
    )
    assert not next(c for c in result.checks if c.name == "cited_trials_meet_filters").passed


def test_network_datums_get_citations_and_checks(trials):
    """Nodes and edges are Datums: evidence and the verifier treat them like bars."""
    from clinical_trials_viz.models.spec import NetworkData
    from clinical_trials_viz.network import sponsor_drug_network
    from clinical_trials_viz.spec_builder import network_spec

    network = sponsor_drug_network(trials)
    spec = network_spec(network, AppliedFilters(), network.contributing_trials)
    assert isinstance(spec.data, NetworkData) and spec.data.edges
    assert len(spec.datums()) == len(spec.data.nodes) + len(spec.data.edges)
    by_id = {t.nct_id: t for t in trials}
    filters = AppliedFilters()
    evidence = build_evidence(spec, by_id, None, filters)
    assert {i for d in spec.datums() for i in d["trial_ids"]} == set(evidence)
    assert verify(spec, evidence, by_id, None, VisualizationType.NETWORK_GRAPH, filters).passed

    spec.data.edges[0]["trial_count"] += 1  # an edge weight that its citations do not support
    result = verify(spec, evidence, by_id, None, VisualizationType.NETWORK_GRAPH, filters)
    assert not next(c for c in result.checks if c.name == "counts_match_citations").passed
    with pytest.raises(TypeError):
        spec.rows()


# --- Structured fields against the question ---------------------------------------------------------


def test_the_gate_finds_a_structured_field_that_contradicts_the_question():
    plan = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(drugs=["nivolumab"]), group_by=Dimension.PHASE)
    result = check_plan(plan, QueryRequest(query="Nivolumab trials by phase", drug_name="Pembrolizumab"), set())
    assert [(c.request_field, c.question_value, c.field_value) for c in result.conflicts] == [
        ("drug_name", ["nivolumab"], ["Pembrolizumab"])
    ]
    same = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(countries=["USA"]), group_by=Dimension.PHASE)
    quiet = check_plan(same, QueryRequest(query="US trials", country="United States"), {"United States"})
    assert not quiet.conflicts  # an alias is not a conflict


def test_a_conflict_clarification_offers_both_values_as_valid_field_values():
    plan = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(phases=[Phase.PHASE3]))
    (conflict,) = check_plan(plan, QueryRequest(query="Phase 3 trials", trial_phase="PHASE2"), set()).conflicts
    clarification = clarify.conflict_question(conflict)
    assert clarification.field == "trial_phase" and clarification.reason == "conflict"
    assert [o.label for o in clarification.options] == ["Phase 3 (your question)", "Phase 2 (your filters)"]
    for option in clarification.options:
        QueryRequest(query="Phase 3 trials", trial_phase=option.value)  # type: ignore[arg-type]  # validates


def test_recruiting_by_country_says_which_status_is_meant():
    plan = AnswerPlan(
        operation=Operation.AGGREGATE,
        filters=Filters(conditions=["melanoma"], statuses=[OverallStatus.RECRUITING]),
        group_by=Dimension.COUNTRY,
    )
    result = check_plan(plan, QueryRequest(query="Which countries have the most recruiting melanoma trials?"), set())
    assert any("overall status" in a and "whatever their own status" in a for a in result.assumptions)
