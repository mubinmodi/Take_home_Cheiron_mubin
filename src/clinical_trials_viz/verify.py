"""The verifier: a gate before every successful response. Failing checks block the answer."""

from clinical_trials_viz.catalog import NOT_REPORTED, OTHER_BUCKET, Dimension
from clinical_trials_viz.ctgov.trial import Trial, dimension_values
from clinical_trials_viz.models.response import AppliedFilters, EvidenceEntry, Verification, VerificationCheck
from clinical_trials_viz.models.spec import VisualizationSpec, VisualizationType


def verify(
    spec: VisualizationSpec,
    evidence: dict[str, EvidenceEntry],
    trials: dict[str, Trial],
    dimension: Dimension | None,
    expected_type: VisualizationType,
    filters: AppliedFilters,
) -> Verification:
    checks: list[VerificationCheck] = []

    def check(name: str, problems: list[str]) -> None:
        checks.append(
            VerificationCheck(name=name, passed=not problems, detail="; ".join(problems[:5]) if problems else None)
        )

    check("answers_plan", [] if spec.type is expected_type else [f"expected {expected_type}, built {spec.type}"])

    # Validity: every encoded field exists in every datum.
    enc = spec.encoding
    channels = [c for c in (enc.x, enc.y, enc.color, enc.value) if c] + (enc.columns or []) + enc.tooltip
    check(
        "encoded_fields_exist",
        [f"datum {i} lacks '{c.field}'" for i, d in enumerate(spec.data) for c in channels if c.field not in d],
    )

    # Citations add up: each count equals its distinct cited trials.
    check(
        "counts_match_citations",
        [
            f"datum {i}: count {d['trial_count']} but {len(set(d['trial_ids']))} cited trials"
            for i, d in enumerate(spec.data)
            if "trial_count" in d and d["trial_count"] != len(set(d["trial_ids"]))
        ],
    )

    # Every cited trial resolves in the evidence and in the retrieved cohort.
    cited = {i for d in spec.data for i in d["trial_ids"]}
    check("citations_resolve", [f"{i} missing" for i in sorted(cited) if i not in evidence or i not in trials])

    # Each cited trial really has the value of the bucket it is counted in.
    if dimension is not None and spec.type is not VisualizationType.TABLE:
        wrong = []
        for d in spec.data:
            label = d.get(dimension.value)
            if label in (OTHER_BUCKET, None):
                continue
            for nct_id in d["trial_ids"]:
                if nct_id in trials and label not in dimension_values(trials[nct_id], dimension):
                    wrong.append(f"{nct_id} counted under '{label}'")
        check("cited_values_match_source", wrong)

    # Every cited trial meets the filters, checked against its own source values (not the API's word).
    check("cited_trials_meet_filters", [
        f"{nct_id}: {problem}" for nct_id in sorted(cited) if nct_id in trials
        for problem in filter_violations(trials[nct_id], filters)
    ])  # fmt: skip

    # Readability: no silent gaps in a time series.
    if spec.type is VisualizationType.TIME_SERIES and dimension is Dimension.START_YEAR:
        years = [int(d[dimension.value]) for d in spec.data if d[dimension.value] != NOT_REPORTED]
        check("no_time_gaps", [] if years == list(range(min(years), max(years) + 1)) else ["missing years"])

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
    if filters.sponsor_exact and (trial.lead_sponsor or "").lower() != (filters.sponsor or "").lower():
        problems.append(f"lead sponsor {trial.lead_sponsor!r} is not {filters.sponsor!r}")
    if filters.start_year_from or filters.start_year_to:
        year = trial.start_year
        low, high = filters.start_year_from or 0, filters.start_year_to or 9999
        if year is None or not low <= year <= high:
            problems.append(f"start year {year} outside {low}-{high}")
    return problems
