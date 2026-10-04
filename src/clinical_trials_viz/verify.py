"""The verifier: a gate before every successful response. Failing checks block the answer."""

from clinical_trials_viz.analyze import enrollment_bin, enrollment_type
from clinical_trials_viz.catalog import NOT_REPORTED, OTHER_BUCKET, Dimension, study_url
from clinical_trials_viz.ctgov.trial import Trial, dimension_values
from clinical_trials_viz.models.response import AppliedFilters, EvidenceEntry, Verification, VerificationCheck
from clinical_trials_viz.models.spec import NetworkData, VisualizationSpec, VisualizationType
from clinical_trials_viz.network import same_arm_pairs, trial_drugs
from clinical_trials_viz.spec_builder import cited_value, duration_months, iso_date, trial_end


def verify(
    spec: VisualizationSpec,
    evidence: dict[str, EvidenceEntry],
    trials: dict[str, Trial],
    dimension: Dimension | None,
    expected_type: VisualizationType,
    filters: AppliedFilters,
    series: Dimension | None = None,
) -> Verification:
    checks: list[VerificationCheck] = []

    def check(name: str, problems: list[str]) -> None:
        checks.append(
            VerificationCheck(name=name, passed=not problems, detail="; ".join(problems[:5]) if problems else None)
        )

    # The chart answers the plan: its type, and it is grouped by the plan's dimensions.
    wrong_plan = [] if spec.type is expected_type else [f"expected {expected_type}, built {spec.type}"]
    if spec.type not in (VisualizationType.TABLE, VisualizationType.NETWORK_GRAPH):
        enc = spec.encoding
        encoded = {c.field for c in (enc.x, enc.y, enc.color) if c}
        wrong_plan += [f"not grouped by {d.value}" for d in (dimension, series) if d and d.value not in encoded]
    check("answers_plan", wrong_plan)

    datums = spec.datums()

    # Validity: every encoded field exists in every row.
    if spec.type is not VisualizationType.NETWORK_GRAPH:
        enc = spec.encoding
        channels = [c for c in (enc.x, enc.y, enc.color, enc.value) if c] + (enc.columns or []) + enc.tooltip
        check(
            "encoded_fields_exist",
            [f"datum {i} lacks '{c.field}'" for i, d in enumerate(spec.rows()) for c in channels if c.field not in d],
        )

    # Citations add up: each count (bar height, node size, edge weight) equals its distinct cited trials.
    check(
        "counts_match_citations",
        [
            f"datum {i}: count {d['trial_count']} but {len(set(d['trial_ids']))} cited trials"
            for i, d in enumerate(datums)
            if "trial_count" in d and d["trial_count"] != len(set(d["trial_ids"]))
        ],
    )

    # Every cited trial resolves in the evidence and in the retrieved cohort.
    cited = {i for d in datums for i in d["trial_ids"]}
    check("citations_resolve", [f"{i} missing" for i in sorted(cited) if i not in evidence or i not in trials])

    # Each cited trial really has the value of the bucket it is counted in (both, for a crossed chart).
    if dimension is not None and spec.type not in (VisualizationType.TABLE, VisualizationType.NETWORK_GRAPH):
        wrong = []
        for dim in [dimension, *([series] if series else [])]:
            for d in spec.rows():
                label = d.get(dim.value)
                if label is None:
                    wrong.append(f"a row has no '{dim.value}' value")
                    continue
                if label == OTHER_BUCKET:
                    continue
                for nct_id in d["trial_ids"]:
                    if nct_id in trials and label not in dimension_values(trials[nct_id], dim):
                        wrong.append(f"{nct_id} counted under '{label}'")
        check("cited_values_match_source", wrong)

    if isinstance(spec.data, NetworkData):
        enc = spec.encoding
        node_fields = [c.field for c in (enc.label, enc.size, enc.color) if c]
        edge_fields = [c.field for c in (enc.source, enc.target, enc.weight) if c]
        check(
            "encoded_fields_exist",
            [f"node {n.get('id')} lacks '{f}'" for n in spec.data.nodes for f in node_fields if f not in n]
            + [f"edge {i} lacks '{f}'" for i, e in enumerate(spec.data.edges) for f in edge_fields if f not in e],
        )
        check("network_matches_source", network_problems(spec.data, trials))

    if spec.type is VisualizationType.HISTOGRAM:
        check(
            "cited_values_match_source",
            [
                f"{nct_id}: enrollment {trials[nct_id].enrollment} ({trials[nct_id].enrollment_type}) not in "
                f"'{d['enrollment_bin']}' / {d['enrollment_type']}"
                for d in spec.rows()
                for nct_id in d["trial_ids"]
                if nct_id in trials
                and (enrollment_bin(trials[nct_id].enrollment), enrollment_type(trials[nct_id]))
                != (d["enrollment_bin"], d["enrollment_type"])
            ],
        )

    if spec.type is VisualizationType.TIMELINE:
        wrong = []
        for d in spec.rows():
            trial = trials.get(d["nct_id"])
            if trial is None:
                continue
            end, _ = trial_end(trial)
            if (d["start"], d["end"]) != (iso_date(trial.start_date), iso_date(end)) or d["trial_ids"] != [
                trial.nct_id
            ]:
                wrong.append(f"{d['nct_id']}: {d['start']}–{d['end']} does not match its record")
        check("cited_values_match_source", wrong)

    if spec.type is VisualizationType.SCATTER_PLOT:
        check(
            "cited_values_match_source",
            [
                f"{d['nct_id']}: point does not match its record"
                for d in spec.rows()
                if (t := trials.get(d["nct_id"]))
                and (
                    (d["duration_months"], d["enrollment"]) != (duration_months(t), t.enrollment)
                    or d["trial_ids"] != [t.nct_id]
                )
            ],
        )

    # Every quoted citation value is what the trial record says, re-derived the way it was built.
    altered = []
    for nct_id, entry in evidence.items():
        trial = trials.get(nct_id)
        if trial is None:
            continue  # reported by citations_resolve
        if (entry.nct_id, entry.title, entry.url) != (nct_id, trial.title, study_url(nct_id)):
            altered.append(f"{nct_id}: identity, title or link differs from the record")
        for name, value in entry.fields.items():
            try:
                expected = cited_value(trial, name)
            except KeyError:
                altered.append(f"{nct_id}: '{name}' is not a field we cite")
                continue
            if value != expected:
                altered.append(f"{nct_id}: '{name}' differs from the record")
    check("evidence_matches_source", altered)

    # Every cited trial meets the filters, checked against its own source values (not the API's word).
    check("cited_trials_meet_filters", [
        f"{nct_id}: {problem}" for nct_id in sorted(cited) if nct_id in trials
        for problem in filter_violations(trials[nct_id], filters)
    ])  # fmt: skip

    # Readability: no silent gaps in a time series.
    if spec.type is VisualizationType.TIME_SERIES and dimension is Dimension.START_YEAR:
        years = sorted({int(d[dimension.value]) for d in spec.rows() if d[dimension.value] != NOT_REPORTED})
        check("no_time_gaps", [] if not years or years == list(range(years[0], years[-1] + 1)) else ["missing years"])

    return Verification(passed=all(c.passed for c in checks), checks=checks)


def filter_violations(trial: Trial, filters: AppliedFilters) -> list[str]:
    """Filters a trial fails. Drug and condition matching is checked during retrieval instead."""
    problems = []
    if filters.phases and not set(trial.phases) & set(filters.phases):
        problems.append(f"phases {list(trial.phases)} not in {list(filters.phases)}")
    if filters.statuses and trial.overall_status not in filters.statuses:
        problems.append(f"status {trial.overall_status} not in {list(filters.statuses)}")
    if filters.study_types and trial.study_type not in filters.study_types:
        problems.append(f"study type {trial.study_type} not in {list(filters.study_types)}")
    if filters.countries and not set(trial.countries) & set(filters.countries):
        problems.append(f"no site in {filters.countries}")
    if filters.nct_ids and trial.nct_id not in filters.nct_ids:
        problems.append("not one of the requested NCT IDs")
    wanted = {n.lower() for n in (filters.exact_sponsors or [filters.sponsor or ""])}
    if filters.sponsor_exact and (trial.lead_sponsor or "").lower() not in wanted:
        problems.append(f"lead sponsor {trial.lead_sponsor!r} is not {filters.sponsor!r}")
    if filters.start_year_from or filters.start_year_to:
        year = trial.start_year
        low, high = filters.start_year_from or 0, filters.start_year_to or 9999
        if year is None or not low <= year <= high:
            problems.append(f"start year {year} outside {low}-{high}")
    return problems


def _node_values(trial: Trial, kind: str) -> tuple[str, ...]:
    if kind == "sponsor":
        return (trial.lead_sponsor,) if trial.lead_sponsor else ()
    if kind == "drug":
        return trial_drugs(trial)
    return ()


def network_problems(data: NetworkData, trials: dict[str, Trial]) -> list[str]:
    """Edges join existing nodes, and every cited trial really has each entity it is cited for."""
    nodes = {n["id"]: n for n in data.nodes}
    problems = []
    for node in data.nodes:
        for nct_id in node["trial_ids"]:
            if nct_id in trials and node.get("label") not in _node_values(trials[nct_id], node.get("kind", "")):
                problems.append(f"{nct_id} cited for {node['id']} but its record lacks it")
    for edge in data.edges:
        ends = [nodes.get(edge["source"]), nodes.get(edge["target"])]
        if None in ends:
            problems.append(f"edge {edge['source']} -> {edge['target']} points at a missing node")
            continue
        for nct_id in edge["trial_ids"]:
            trial = trials.get(nct_id)
            if trial and any(n.get("label") not in _node_values(trial, n.get("kind", "")) for n in ends if n):
                problems.append(f"{nct_id} cited for {edge['source']} -> {edge['target']} but lacks one end")
            elif trial and edge.get("kind") == "same_arm":
                pair = tuple(sorted(str(n.get("label")) for n in ends if n))
                if pair not in same_arm_pairs(trial):
                    problems.append(f"{nct_id} cited for {pair[0]} + {pair[1]} but no arm gives both")
    return problems
