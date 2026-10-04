"""Counting: turn a Cohort into buckets of trial IDs. All numbers come from here."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from clinical_trials_viz.catalog import DIMENSIONS, NOT_REPORTED, OTHER_BUCKET, PHASE_LABELS, Dimension
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
