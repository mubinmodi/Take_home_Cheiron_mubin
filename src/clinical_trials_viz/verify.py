"""The verifier: a gate before every successful response. Failing checks block the answer."""

from clinical_trials_viz.catalog import NOT_REPORTED, OTHER_BUCKET, Dimension
from clinical_trials_viz.ctgov.trial import Trial, dimension_values
from clinical_trials_viz.models.response import EvidenceEntry, Verification, VerificationCheck
from clinical_trials_viz.models.spec import VisualizationSpec, VisualizationType


def verify(
    spec: VisualizationSpec,
    evidence: dict[str, EvidenceEntry],
    trials: dict[str, Trial],
    dimension: Dimension | None,
    expected_type: VisualizationType,
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

    # Readability: no silent gaps in a time series.
    if spec.type is VisualizationType.TIME_SERIES and dimension is Dimension.START_YEAR:
        years = [int(d[dimension.value]) for d in spec.data if d[dimension.value] != NOT_REPORTED]
        check("no_time_gaps", [] if years == list(range(min(years), max(years) + 1)) else ["missing years"])

    return Verification(passed=all(c.passed for c in checks), checks=checks)
