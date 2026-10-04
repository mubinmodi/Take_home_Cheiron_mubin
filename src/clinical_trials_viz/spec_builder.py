"""Build the Visualization Specification and its Evidence from counted results.

The chart type follows deterministically from the Query Plan; the model does not choose it.
"""

from typing import Any

from clinical_trials_viz.analyze import Breakdown, ComparisonGroups, breakdown
from clinical_trials_viz.catalog import DIMENSIONS, OTHER_BUCKET, PHASE_LABELS, TABLE_MAX_ROWS, Dimension, study_url
from clinical_trials_viz.ctgov.trial import Trial, dimension_evidence
from clinical_trials_viz.models.plan import AnswerPlan, Operation
from clinical_trials_viz.models.response import AppliedFilters, EvidenceEntry
from clinical_trials_viz.models.spec import (
    Channel,
    Encoding,
    FieldType,
    RenderMetadata,
    VisualizationSpec,
    VisualizationType,
)

COUNT = Channel(field="trial_count", type=FieldType.QUANTITATIVE, title="Trials")


def chart_type_for(plan: AnswerPlan) -> VisualizationType:
    """Which visualization answers a plan. Decided by code, so the same plan always gets the same chart."""
    if plan.operation is Operation.PER_TRIAL:
        return VisualizationType.TABLE
    if plan.operation is Operation.COMPARE:
        return VisualizationType.GROUPED_BAR_CHART if plan.group_by else VisualizationType.BAR_CHART
    if plan.group_by is None:
        return VisualizationType.SINGLE_VALUE
    if plan.group_by is Dimension.START_YEAR:
        return VisualizationType.TIME_SERIES
    return VisualizationType.BAR_CHART


def describe_filters(filters: AppliedFilters) -> str:
    parts: list[str] = []
    if filters.drugs:
        parts.append(" or ".join(filters.drugs))
    if filters.conditions:
        parts.append(" or ".join(filters.conditions))
    if filters.sponsor:
        parts.append(f"{'lead sponsor' if filters.sponsor_role == 'lead' else 'sponsor'} {filters.sponsor}")
    if filters.phases:
        parts.append(", ".join(PHASE_LABELS[p] for p in filters.phases))
    if filters.statuses:
        parts.append(", ".join(s.replace("_", " ").lower() for s in filters.statuses))
    if filters.study_types:
        parts.append(", ".join(s.replace("_", " ").lower() for s in filters.study_types))
    if filters.countries:
        parts.append("in " + ", ".join(filters.countries))
    if filters.start_year_from and filters.start_year_to:
        parts.append(f"started {filters.start_year_from}–{filters.start_year_to}")
    elif filters.start_year_from:
        parts.append(f"started {filters.start_year_from} or later")
    elif filters.start_year_to:
        parts.append(f"started {filters.start_year_to} or earlier")
    if filters.nct_ids:
        parts.append(", ".join(filters.nct_ids))
    return "; ".join(parts) if parts else "all trials"


def _dimension_channel(dimension: Dimension) -> Channel:
    kind = FieldType.ORDINAL if DIMENSIONS[dimension].ordered else FieldType.NOMINAL
    return Channel(field=dimension.value, type=kind, title=DIMENSIONS[dimension].label)


def single_value_spec(trials: list[Trial], filters: AppliedFilters) -> VisualizationSpec:
    ids = sorted(t.nct_id for t in trials)
    return VisualizationSpec(
        type=VisualizationType.SINGLE_VALUE,
        title=f"Number of trials: {describe_filters(filters)}",
        encoding=Encoding(value=COUNT),
        data=[{"label": "Trials", "trial_count": len(ids), "trial_ids": ids}],
        metadata=RenderMetadata(cohort_size=len(ids)),
    )


def breakdown_spec(result: Breakdown, filters: AppliedFilters, cohort_size: int) -> VisualizationSpec:
    dim = result.dimension
    info = DIMENSIONS[dim]
    time_series = dim is Dimension.START_YEAR
    data: list[dict[str, Any]] = []
    for bucket in result.buckets:
        datum: dict[str, Any] = {dim.value: bucket.label, "trial_count": bucket.count, "trial_ids": bucket.trial_ids}
        if time_series:
            datum["estimated_count"] = bucket.estimated
        data.append(datum)
    tooltip = [_dimension_channel(dim), COUNT]
    if time_series:
        tooltip.append(Channel(field="estimated_count", type=FieldType.QUANTITATIVE, title="With estimated start date"))
    return VisualizationSpec(
        type=VisualizationType.TIME_SERIES if time_series else VisualizationType.BAR_CHART,
        title=f"Trials by {info.label.lower()}: {describe_filters(filters)}",
        subtitle=f"{cohort_size} trials",
        encoding=Encoding(x=_dimension_channel(dim), y=COUNT, tooltip=tooltip),
        data=data,
        metadata=RenderMetadata(
            time_granularity="year" if time_series else None,
            category_order=[b.label for b in result.buckets],
            top_n=result.top_n,
            other_bucket=result.folded > 0,
            multi_valued=info.multi_valued,
            cohort_size=cohort_size,
        ),
    )


def comparison_spec(
    groups: ComparisonGroups, dimension: Dimension | None, filters: AppliedFilters, sides: list[str], top_n: int | None
) -> tuple[VisualizationSpec, list[str]]:
    group_channel = Channel(field="group", type=FieldType.NOMINAL, title="Comparison group")
    cohort_size = sum(len(t) for t in groups.trials.values())
    scope = describe_filters(filters)
    title = " vs ".join(sides) + ("" if scope == "all trials" else f": {scope}")
    assumptions = [f"Trials that involve more than one compared side are shown as '{groups.overlap_label}'."]

    if dimension is None:
        data = [
            {
                "group": label,
                "trial_count": len(groups.trials[label]),
                "trial_ids": sorted(t.nct_id for t in groups.trials[label]),
            }
            for label in groups.labels
        ]
        spec = VisualizationSpec(
            type=VisualizationType.BAR_CHART,
            title=f"Trials compared: {title}",
            subtitle=f"{cohort_size} trials",
            encoding=Encoding(x=group_channel, y=COUNT, tooltip=[group_channel, COUNT]),
            data=data,
            metadata=RenderMetadata(category_order=groups.labels, cohort_size=cohort_size),
        )
        return spec, assumptions

    # Use one category order for every group so bars line up.
    everything = [t for label in groups.labels for t in groups.trials[label]]
    overall = breakdown(everything, dimension, top_n)
    assumptions.extend(overall.assumptions)
    kept = [b.label for b in overall.buckets]
    data = []
    for label in groups.labels:
        per_group = {b.label: b for b in breakdown(groups.trials[label], dimension, None).buckets}
        other_ids: set[str] = set()
        for category, bucket in per_group.items():
            if category not in kept:
                other_ids.update(bucket.trial_ids)
        for category in kept:
            ids = (
                sorted(other_ids)
                if category == OTHER_BUCKET and overall.folded
                else (per_group[category].trial_ids if category in per_group else [])
            )
            data.append({dimension.value: category, "group": label, "trial_count": len(ids), "trial_ids": ids})
    info = DIMENSIONS[dimension]
    spec = VisualizationSpec(
        type=VisualizationType.GROUPED_BAR_CHART,
        title=f"Trials by {info.label.lower()}: {title}",
        subtitle=f"{cohort_size} trials",
        encoding=Encoding(
            x=_dimension_channel(dimension),
            y=COUNT,
            color=group_channel,
            tooltip=[_dimension_channel(dimension), group_channel, COUNT],
        ),
        data=data,
        metadata=RenderMetadata(
            category_order=kept,
            series_order=groups.labels,
            top_n=overall.top_n,
            other_bucket=overall.folded > 0,
            multi_valued=info.multi_valued,
            cohort_size=cohort_size,
        ),
    )
    return spec, assumptions


TABLE_COLUMNS = [
    Channel(field="nct_id", type=FieldType.NOMINAL, title="NCT ID"),
    Channel(field="title", type=FieldType.NOMINAL, title="Title"),
    Channel(field="status", type=FieldType.NOMINAL, title="Status"),
    Channel(field="phase", type=FieldType.NOMINAL, title="Phase"),
    Channel(field="start_date", type=FieldType.TEMPORAL, title="Start date"),
    Channel(field="lead_sponsor", type=FieldType.NOMINAL, title="Lead sponsor"),
    Channel(field="enrollment", type=FieldType.QUANTITATIVE, title="Enrollment"),
]


def table_spec(trials: list[Trial], filters: AppliedFilters) -> tuple[VisualizationSpec, list[str]]:
    ordered = sorted(trials, key=lambda t: t.start_date or "", reverse=True)
    rows = [
        {
            "nct_id": t.nct_id,
            "title": t.title,
            "status": t.overall_status,
            "phase": ", ".join(PHASE_LABELS.get(p, p) for p in t.phases) or None,
            "start_date": t.start_date,
            "lead_sponsor": t.lead_sponsor,
            "enrollment": t.enrollment,
            "trial_ids": [t.nct_id],
        }
        for t in ordered[:TABLE_MAX_ROWS]
    ]
    assumptions = []
    if len(ordered) > TABLE_MAX_ROWS:
        assumptions.append(f"Showing the {TABLE_MAX_ROWS} most recently started of {len(ordered)} trials.")
    spec = VisualizationSpec(
        type=VisualizationType.TABLE,
        title=f"Trials: {describe_filters(filters)}",
        subtitle=f"{len(ordered)} trials",
        encoding=Encoding(columns=TABLE_COLUMNS),
        data=rows,
        metadata=RenderMetadata(cohort_size=len(ordered), total_rows=len(ordered)),
    )
    return spec, assumptions


COLLABORATORS_FIELD = "protocolSection.sponsorCollaboratorsModule.collaborators.name"


def filter_dimensions(filters: AppliedFilters) -> list[Dimension]:
    """The source fields that decide whether a trial passes each applied filter."""
    used = {
        Dimension.DRUG: bool(filters.drugs),
        Dimension.CONDITION: bool(filters.conditions),
        Dimension.PHASE: bool(filters.phases),
        Dimension.STATUS: bool(filters.statuses),
        Dimension.STUDY_TYPE: bool(filters.study_types),
        Dimension.LEAD_SPONSOR: bool(filters.sponsor),
        Dimension.COUNTRY: bool(filters.countries),
        Dimension.START_YEAR: bool(filters.start_year_from or filters.start_year_to),
    }
    return [d for d, on in used.items() if on]


def build_evidence(
    spec: VisualizationSpec,
    trials: dict[str, Trial],
    dimension: Dimension | None,
    filters: AppliedFilters,
    extra: tuple[Dimension, ...] = (),
) -> dict[str, EvidenceEntry]:
    """One entry per cited trial, with the source values that placed it in the data: the field it
    is grouped by and every field an applied filter (or comparison side, via `extra`) relies on."""
    wanted: list[Dimension] = [*filter_dimensions(filters), *extra, *([dimension] if dimension else [])]
    cited = list(dict.fromkeys(wanted))
    evidence: dict[str, EvidenceEntry] = {}
    for datum in spec.datums():
        for nct_id in datum["trial_ids"]:
            if nct_id in evidence:
                continue
            trial = trials[nct_id]
            fields: dict[str, Any] = {"protocolSection.identificationModule.nctId": nct_id}
            for dim in cited:
                fields[DIMENSIONS[dim].source_field] = dimension_evidence(trial, dim)
            if filters.sponsor and filters.sponsor_role == "any":
                fields[COLLABORATORS_FIELD] = list(trial.collaborators)
            evidence[nct_id] = EvidenceEntry(nct_id=nct_id, title=trial.title, url=study_url(nct_id), fields=fields)
    return evidence
