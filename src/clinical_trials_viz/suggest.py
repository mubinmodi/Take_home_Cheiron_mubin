"""Ways forward when a question has no answer, built by code: corrections counted live, never guessed."""

from clinical_trials_viz.cohort import build_params
from clinical_trials_viz.ctgov.client import CtGovClient
from clinical_trials_viz.models.response import AppliedFilters, Suggestion
from clinical_trials_viz.spec_builder import describe_filters

MAX_CORRECTIONS = 4

# Filters a correction may drop, one at a time: the name the user sees, and the fields it clears.
_DROPPABLE: list[tuple[str, tuple[str, ...]]] = [
    ("country", ("countries",)),
    ("phase", ("phases",)),
    ("status", ("statuses",)),
    ("start-year", ("start_year_from", "start_year_to")),
    ("study type", ("study_types",)),
    ("keyword", ("keywords",)),
    ("sponsor", ("sponsor", "exact_sponsors", "sponsor_exact")),
]


async def count(filters: AppliedFilters, client: CtGovClient) -> int:
    """Search matches for these filters (drugs are searched one at a time, as in retrieval)."""
    if filters.drugs:
        return sum([await client.count(build_params(filters, drug)) for drug in filters.drugs])
    return await client.count(build_params(filters))


async def corrections(filters: AppliedFilters, client: CtGovClient) -> list[Suggestion]:
    """For each filter, how many trials match without it. Only corrections that find trials are offered.
    A filter pinned by a structured field is reported but cannot be dropped by a Follow-up."""
    suggestions = []
    for name, fields in _DROPPABLE:
        if not any(getattr(filters, f) for f in fields):
            continue
        only = AppliedFilters(**{f: getattr(filters, f) for f in fields if f != "sponsor_exact"})
        relaxed = filters.model_copy(update={f: AppliedFilters.model_fields[f].get_default(call_default_factory=True)
                                              for f in fields})  # fmt: skip
        found = await count(relaxed, client)
        if not found:
            continue
        dropped = describe_filters(only)
        pinned = any(f in filters.from_request for f in fields)
        suggestions.append(
            Suggestion(
                label=f"Without the {name} filter ({dropped}): {'about ' if filters.drugs else ''}{found:,} "
                + ("trial" if found == 1 else "trials")
                + (" (remove it from your filters)" if pinned else ""),
                follow_up=None if pinned else f"Remove the {name} filter ({dropped})",
                trial_count=found,
            )
        )
        if len(suggestions) == MAX_CORRECTIONS:
            break
    return suggestions


async def unknown_drugs(filters: AppliedFilters, client: CtGovClient) -> list[str]:
    """Drug names that no trial in the registry lists as an intervention at all (likely misspelled)."""
    return [drug for drug in filters.drugs if not await client.count({"query.intr": drug})]
