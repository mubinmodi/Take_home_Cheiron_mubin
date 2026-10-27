"""Answering a Query Plan from its Cohort: count, build the Visualization Specification, choose the
Evidence each chart relies on, and verify. Everything an Operation needs is decided here, in one place.

The interface is `answer(plan, filters, cohort)`. The cohort is the retrieved trials, or for a comparison
one list per Comparison Group side, and is never empty (the pipeline handles an empty retrieval, which
needs the API to suggest corrections). The result is one of three: an `Answered` answer, `NothingToDraw`
(the trials were all checked but give the chart nothing to show; its reason becomes the no_data
message), or `Unverified` (the verifier found a problem; the answer must be withheld). In-process only:
no I/O, so it is tested with saved records."""

from dataclasses import dataclass, field

from clinical_trials_viz.analyze import breakdown, comparison_groups, cross_breakdown, enrollment_histogram
from clinical_trials_viz.catalog import DEFAULT_TOP_N, ENROLLMENT_FIELD, NOT_REPORTED, Dimension
from clinical_trials_viz.ctgov.trial import Trial
from clinical_trials_viz.models.plan import AnswerPlan, NetworkKind, Operation, PerTrialView
from clinical_trials_viz.models.response import AppliedFilters, EvidenceEntry, Verification, VerificationCheck
from clinical_trials_viz.models.spec import Encoding, NetworkData, RenderMetadata, VisualizationSpec, VisualizationType
from clinical_trials_viz.network import drug_drug_network, sponsor_drug_network
from clinical_trials_viz.spec_builder import (
    ARM_FIELD,
    COMPLETION_FIELD,
    COUNT,
    PRIMARY_COMPLETION_FIELD,
    START_FIELD,
    breakdown_spec,
    build_evidence,
    chart_type_for,
    comparison_spec,
    cross_spec,
    histogram_spec,
    network_spec,
    scatter_spec,
    single_value_spec,
    table_spec,
    timeline_spec,
)
from clinical_trials_viz.verify import verify


@dataclass
class Answered:
    spec: VisualizationSpec
    evidence: dict[str, EvidenceEntry]
    verification: Verification
    assumptions: list[str] = field(default_factory=list)


@dataclass
class NothingToDraw:
    reason: str  # the no_data message: how the question was read, and why there is nothing to show
    assumptions: list[str] = field(default_factory=list)


@dataclass
class Unverified:
    verification: Verification  # the failed checks; the answer is withheld
    assumptions: list[str] = field(default_factory=list)


AnswerResult = Answered | NothingToDraw | Unverified


def counts_without_records(plan: AnswerPlan, filters: AppliedFilters) -> bool:
    """A plain total whose filters ClinicalTrials.gov applies exactly as we would, so its own count is the
    answer: no grouping, no drug (the drug match check reads each record) and no sponsor (an ambiguous
    sponsor is asked about from the records)."""
    plain = plan.operation is Operation.AGGREGATE and plan.group_by is None and plan.series_by is None
    return plain and not filters.drugs and not filters.sponsor


def registry_total(filters: AppliedFilters, total: int, query: str) -> Answered:
    """A plain total above the retrieval cap: the registry's own count of `query`, which it cites instead
    of each trial (`counts_without_records` says when this is allowed)."""
    spec = VisualizationSpec(
        type=VisualizationType.SINGLE_VALUE,
        title=f"Number of trials: {filters.describe()}",
        encoding=Encoding(value=COUNT),
        data=[{"label": "Trials", "trial_count": total, "trial_ids": []}],
        metadata=RenderMetadata(cohort_size=total, counted_by="registry", source_query=query),
    )
    checks = [
        VerificationCheck(name="answers_plan", passed=True),
        VerificationCheck(name="count_is_registry_total", passed=True, detail=f"totalCount of {query}"),
    ]
    note = (
        f"{total:,} trials is ClinicalTrials.gov's own count for this search (the request is in "
        "metadata.source_query). Above the retrieval cap the trials are not fetched one by one, so this total "
        "cites the search rather than each trial."
    )
    return Answered(spec, {}, Verification(passed=True, checks=checks), [note])


def answer(plan: AnswerPlan, filters: AppliedFilters, cohort: list[Trial] | dict[str, list[Trial]]) -> AnswerResult:
    """Answer the plan from its (non-empty) cohort; for a comparison, `cohort` maps each side to its trials."""
    notes: list[str] = []
    if isinstance(cohort, dict):
        trials = {t.nct_id: t for side in cohort.values() for t in side}
        groups = comparison_groups(cohort)
        spec, compare_notes = comparison_spec(groups, plan.group_by, filters, list(cohort), plan.top_n or DEFAULT_TOP_N)
        notes += compare_notes
        dimension = plan.group_by
    else:
        trials = {t.nct_id: t for t in cohort}
        spec, dimension = _build(plan, filters, cohort, notes)
        if reason := _nothing_to_draw(spec, dimension, len(cohort), filters):
            return NothingToDraw(reason, notes)
    evidence = build_evidence(
        spec, trials, dimension, filters, tuple(_cited_dimensions(plan)), _record_fields(plan, spec)
    )
    verification = verify(spec, evidence, trials, dimension, chart_type_for(plan), filters, series=plan.series_by)
    if not verification.passed:
        return Unverified(verification, notes)
    return Answered(spec, evidence, verification, notes)


def _build(
    plan: AnswerPlan, filters: AppliedFilters, trials: list[Trial], notes: list[str]
) -> tuple[VisualizationSpec, Dimension | None]:
    """Count the cohort and build the chart the plan calls for; returns the spec and its grouping dimension."""
    if plan.operation is Operation.PER_TRIAL:
        build = {PerTrialView.TIMELINE: timeline_spec, PerTrialView.SCATTER: scatter_spec}.get(
            plan.view or PerTrialView.TABLE, table_spec
        )
        spec, view_notes = build(trials, filters)
        notes += view_notes
        return spec, None
    if plan.operation is Operation.BIN:
        histogram = enrollment_histogram(trials)
        notes += histogram.assumptions
        return histogram_spec(histogram, filters, len(trials)), None
    if plan.operation is Operation.RELATE:
        kind = plan.network or NetworkKind.SPONSOR_DRUG
        network = (sponsor_drug_network if kind is NetworkKind.SPONSOR_DRUG else drug_drug_network)(trials)
        notes += network.assumptions
        return network_spec(network, filters, network.contributing_trials, kind), None
    if plan.group_by is None:
        return single_value_spec(trials, filters), None
    if plan.series_by is not None:
        cross = cross_breakdown(trials, plan.group_by, plan.series_by, plan.top_n or DEFAULT_TOP_N)
        notes += cross.assumptions
        return cross_spec(cross, filters, len(trials)), plan.group_by
    result = breakdown(trials, plan.group_by, plan.top_n or DEFAULT_TOP_N)
    notes += result.assumptions
    return breakdown_spec(result, filters, len(trials)), plan.group_by


def _nothing_to_draw(
    spec: VisualizationSpec, dimension: Dimension | None, cohort_size: int, filters: AppliedFilters
) -> str | None:
    """Why a chart of a complete, non-empty cohort would show nothing, else None."""
    read = filters.describe()
    if isinstance(spec.data, NetworkData) and not spec.data.edges:
        return (
            f"The {cohort_size:,} trials matching {read} were all checked, but no two entities share enough of "
            "them to draw a link. Try a broader question (for example without a status or phase filter)."
        )
    missing = None
    match spec.type:
        case VisualizationType.TIMELINE if not spec.rows():
            missing = "both a start date and a completion date"
        case VisualizationType.SCATTER_PLOT if not spec.rows():
            missing = "a start date, a completion date and enrollment"
        case VisualizationType.TIME_SERIES if dimension is Dimension.START_YEAR and all(
            d[dimension.value] == NOT_REPORTED for d in spec.rows()
        ):
            missing = "a start date"
    if missing is None:
        return None
    return (
        f"None of the {cohort_size:,} trials matching {read} report {missing}, so there is nothing to plot. "
        "A table of these trials is still available (ask to list them)."
    )


def _cited_dimensions(plan: AnswerPlan) -> list[Dimension]:
    """Fields beyond the filters that place a trial in the answer: its comparison side, the series of
    a crossed chart, or the entities a network links. They are cited too."""
    if plan.series_by is not None:
        return [plan.series_by]
    if plan.operation is Operation.RELATE:
        if plan.network is NetworkKind.DRUG_DRUG:
            return [Dimension.DRUG]
        return [Dimension.LEAD_SPONSOR, Dimension.DRUG]
    found = []
    for side in plan.compare_sides:
        if side.drug:
            found.append(Dimension.DRUG)
        elif side.condition:
            found.append(Dimension.CONDITION)
        elif side.sponsor:
            found.append(Dimension.LEAD_SPONSOR)
    return list(dict.fromkeys(found))


def _record_fields(plan: AnswerPlan, spec: VisualizationSpec) -> tuple[str, ...]:
    """Record fields a chart plots directly, cited with each trial: dates, enrollment, combination arms."""
    fields: list[str] = []
    if spec.type in (VisualizationType.TIMELINE, VisualizationType.SCATTER_PLOT):
        fields += [START_FIELD, PRIMARY_COMPLETION_FIELD, COMPLETION_FIELD]
    if spec.type in (VisualizationType.HISTOGRAM, VisualizationType.SCATTER_PLOT):
        fields.append(ENROLLMENT_FIELD)
    if plan.network is NetworkKind.DRUG_DRUG:
        fields.append(ARM_FIELD)  # the arms that make each link a combination
    return tuple(fields)
