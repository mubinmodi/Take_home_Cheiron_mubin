"""Counting: turn a Cohort into buckets of trial IDs. All numbers come from here."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from clinical_trials_viz.catalog import (
    DIMENSIONS,
    ENROLLMENT_BINS,
    NOT_REPORTED,
    OTHER_BUCKET,
    PHASE_LABELS,
    Dimension,
)
from clinical_trials_viz.ctgov.trial import Trial, dimension_values

_PHASE_ORDER = list(PHASE_LABELS.values())


@dataclass
class Bucket:
    label: str
    trial_ids: list[str]  # sorted, distinct
    estimated: int = 0  # trials whose bucket rests on an estimated date

    @property
    def count(self) -> int:
        return len(self.trial_ids)


@dataclass
class Breakdown:
    """A Cohort counted by one Dimension, in display order."""

    dimension: Dimension
    buckets: list[Bucket]
    top_n: int | None = None
    folded: int = 0  # categories folded into "Other"
    assumptions: list[str] = field(default_factory=list)


def _sort_key(dimension: Dimension, bucket: Bucket) -> tuple:
    not_reported = bucket.label in (NOT_REPORTED, OTHER_BUCKET)
    if dimension is Dimension.START_YEAR:
        return (not_reported, int(bucket.label) if not not_reported else 0)
    if dimension is Dimension.PHASE:
        order = _PHASE_ORDER.index(bucket.label) if bucket.label in _PHASE_ORDER else len(_PHASE_ORDER)
        return (not_reported, order)
    return (not_reported, -bucket.count, bucket.label)


def group(trials: list[Trial], dimension: Dimension) -> dict[str, Bucket]:
    ids: dict[str, set[str]] = defaultdict(set)
    estimated: dict[str, int] = defaultdict(int)
    for trial in trials:
        for label in dimension_values(trial, dimension):
            ids[label].add(trial.nct_id)
            if dimension is Dimension.START_YEAR and trial.start_date_type == "ESTIMATED":
                estimated[label] += 1
    return {label: Bucket(label, sorted(s), estimated[label]) for label, s in ids.items()}


def breakdown(trials: list[Trial], dimension: Dimension, top_n: int | None) -> Breakdown:
    info = DIMENSIONS[dimension]
    buckets = group(trials, dimension)
    result = Breakdown(dimension, [])

    if dimension is Dimension.START_YEAR:
        years = [int(label) for label in buckets if label != NOT_REPORTED]
        if years:  # no silent gaps: years with no trials appear as zero
            for year in range(min(years), max(years) + 1):
                buckets.setdefault(str(year), Bucket(str(year), []))
        if any(b.estimated for b in buckets.values()):
            result.assumptions.append(
                "Start year uses the trial start date; some dates are estimated (planned) rather than actual."
            )
        this_year = date.today().year
        if years and max(years) >= this_year:
            result.assumptions.append(
                f"Counts from {this_year} on are incomplete: they include only trials already registered, "
                "mostly with planned start dates."
            )
        if NOT_REPORTED in buckets:
            result.assumptions.append(
                f"{buckets[NOT_REPORTED].count} trials have no start date; they are listed as "
                f"'{NOT_REPORTED}' and not plotted on the time axis."
            )

    ordered = sorted(buckets.values(), key=lambda b: _sort_key(dimension, b))
    if info.top_n and top_n and len([b for b in ordered if b.label != NOT_REPORTED]) > top_n:
        keep = [b for b in ordered if b.label != NOT_REPORTED][:top_n]
        rest = [b for b in ordered if b not in keep]
        other_ids = sorted({i for b in rest for i in b.trial_ids})
        result.folded = len(rest)
        result.top_n = top_n
        ordered = [*keep, Bucket(OTHER_BUCKET, other_ids)]
        result.assumptions.append(
            f"Showing the top {top_n} {info.label.lower()} categories; {len(rest)} others are combined in '{OTHER_BUCKET}'."
        )
    result.buckets = ordered
    if info.multi_valued:
        result.assumptions.append(
            f"A trial can have several {info.label.lower()} values, so categories can add up to more than the total."
        )
    return result


@dataclass
class ComparisonGroups:
    """Trials split into one 'only' group per side plus one Overlap Group."""

    labels: list[str]  # display order, overlap last
    trials: dict[str, list[Trial]]
    overlap_label: str


def comparison_groups(sides: dict[str, list[Trial]]) -> ComparisonGroups:
    membership: dict[str, set[str]] = defaultdict(set)
    by_id: dict[str, Trial] = {}
    for label, trials in sides.items():
        for trial in trials:
            membership[trial.nct_id].add(label)
            by_id[trial.nct_id] = trial
    overlap_label = "Both" if len(sides) == 2 else "More than one"
    groups: dict[str, list[Trial]] = {f"{label} only": [] for label in sides}
    groups[overlap_label] = []
    for nct_id, labels in membership.items():
        key = f"{next(iter(labels))} only" if len(labels) == 1 else overlap_label
        groups[key].append(by_id[nct_id])
    return ComparisonGroups(list(groups), groups, overlap_label)


ENROLLMENT_TYPES = {"ACTUAL": "Actual", "ESTIMATED": "Estimated"}


def enrollment_bin(count: int | None) -> str:
    if count is None:
        return NOT_REPORTED
    for label, low, high in ENROLLMENT_BINS:
        if count >= low and (high is None or count <= high):
            return label
    return NOT_REPORTED  # negative counts do not occur in the registry


def enrollment_type(trial: Trial) -> str:
    return ENROLLMENT_TYPES.get(trial.enrollment_type or "", "Type not reported")


@dataclass
class Histogram:
    """Trials per enrollment bin, split by actual vs estimated enrollment, in bin order."""

    cells: list[tuple[str, str, list[str]]]  # (bin label, enrollment type, trial IDs)
    series: list[str]
    missing: int
    assumptions: list[str] = field(default_factory=list)


def enrollment_histogram(trials: list[Trial]) -> Histogram:
    ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for trial in trials:
        ids[(enrollment_bin(trial.enrollment), enrollment_type(trial))].add(trial.nct_id)
    labels = [label for label, _, _ in ENROLLMENT_BINS] + [NOT_REPORTED]
    present = {label for label, _ in ids}
    series = [t for t in [*ENROLLMENT_TYPES.values(), "Type not reported"] if any(k[1] == t for k in ids)]
    cells = [(label, t, sorted(ids.get((label, t), set()))) for label in labels if label in present or label != NOT_REPORTED
             for t in series]  # fmt: skip
    missing = sum(len(v) for (label, _), v in ids.items() if label == NOT_REPORTED)
    assumptions = [
        "Enrollment is the number of participants; 'Estimated' is the planned enrollment of trials that have not "
        "finished recruiting, 'Actual' is the final number.",
    ]
    if missing:
        assumptions.append(f"{missing} trials do not report enrollment; they are shown as '{NOT_REPORTED}'.")
    return Histogram(cells, series, missing, assumptions)


SERIES_TOP_N = 6  # colours stay distinguishable


def in_bucket(trial: Trial, dimension: Dimension, label: str, kept: set[str]) -> bool:
    """Whether a trial counts in a bucket; 'Other' holds trials with a value outside the kept buckets."""
    values = dimension_values(trial, dimension)
    if label == OTHER_BUCKET:
        return any(v not in kept for v in values)
    return label in values


@dataclass
class CrossBreakdown:
    """A Cohort counted by two Dimensions: the axis (`dimension`) and a coloured split (`series`)."""

    dimension: Dimension
    series: Dimension
    axis_labels: list[str]
    series_labels: list[str]
    cells: list[tuple[str, str, list[str]]]  # (axis label, series label, trial IDs), series-major
    axis_top_n: int | None
    assumptions: list[str] = field(default_factory=list)


def cross_breakdown(trials: list[Trial], dimension: Dimension, series: Dimension, top_n: int | None) -> CrossBreakdown:
    axis = breakdown(trials, dimension, top_n)
    split = breakdown(trials, series, SERIES_TOP_N)
    axis_labels = [b.label for b in axis.buckets]
    series_labels = [b.label for b in split.buckets]
    axis_kept = {label for label in axis_labels if label != OTHER_BUCKET}
    series_kept = {label for label in series_labels if label != OTHER_BUCKET}
    cells = []
    for s in series_labels:
        members = [t for t in trials if in_bucket(t, series, s, series_kept)]
        for x in axis_labels:
            ids = sorted(t.nct_id for t in members if in_bucket(t, dimension, x, axis_kept))
            cells.append((x, s, ids))
    notes = list(dict.fromkeys([*axis.assumptions, *split.assumptions]))
    return CrossBreakdown(dimension, series, axis_labels, series_labels, cells, axis.top_n, notes)
