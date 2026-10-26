"""Corrections for a question with no data: each filter dropped in turn and the rest counted live, by code.
(Questions suggested for an unanswerable question are written by the planner; see models.plan.UnsupportedPlan.)"""

from clinical_trials_viz.ctgov.client import CtGovClient
from clinical_trials_viz.models.response import AppliedFilters, Suggestion

MAX_CORRECTIONS = 4


async def count(filters: AppliedFilters, client: CtGovClient) -> int:
    """Search matches for these filters, each trial once (several drugs are searched together). Before the
    drug match check, so the label says "about"."""
    drugs = " OR ".join(f"({d})" for d in filters.drugs) or None
    return await client.count(filters.api_params(drugs))


async def corrections(filters: AppliedFilters, client: CtGovClient) -> list[Suggestion]:
    """For each filter, how many trials match without it. Only corrections that find trials are offered.
    A filter pinned by a structured field is reported but cannot be dropped by a Follow-up."""
    suggestions = []
    for relaxation in filters.relaxations():
        found = await count(relaxation.without, client)
        if not found:
            continue
        name, dropped, pinned = relaxation.name, relaxation.dropped, relaxation.pinned
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
