"""Build Clarifications. Options always come from the data or the user's own words, never the model."""

from collections import Counter

from clinical_trials_viz.catalog import DRUG_CLASS_OPTIONS, SPONSOR_AMBIGUITY_MIN_SHARE
from clinical_trials_viz.ctgov.client import CtGovClient
from clinical_trials_viz.ctgov.trial import Trial, drug_identities, parse_trial
from clinical_trials_viz.models.plan import ClarificationReason, ClarifyPlan
from clinical_trials_viz.models.response import Clarification, ClarificationOption

# Plan field -> request field the answer goes back in.
_REQUEST_FIELD = {
    "drug": "drug_name",
    "condition": "condition",
    "sponsor": "sponsor",
    "country": "country",
    "compare_sides": "query",
}


async def from_plan(plan: ClarifyPlan, client: CtGovClient, structured: dict[str, object]) -> Clarification:
    field = _REQUEST_FIELD[plan.field]
    if plan.reason is ClarificationReason.DRUG_CLASS and plan.term:
        return await drug_class_options(plan.term, client)
    if plan.reason is ClarificationReason.CONFLICT:
        values = list(dict.fromkeys([*plan.mentioned_values, *_as_strings(structured.get(field))]))
        return Clarification(
            field=field, question=plan.question, options=[ClarificationOption(label=v, value=v) for v in values]
        )
    return Clarification(field=field, question=plan.question, allow_free_text=True)


def _as_strings(value: object) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


async def drug_class_options(term: str, client: CtGovClient) -> Clarification:
    """Offer the drugs most often found in trials that mention the class name (not a verified class list)."""
    result = await client.search({"query.intr": term}, max_pages=1, allow_partial=True)
    trials = [parse_trial(s) for s in result.studies]
    counts = Counter(name for t in trials for name in drug_identities(t))
    options = [
        ClarificationOption(label=f"{name} ({n} trials)", value=name, trial_count=n)
        for name, n in counts.most_common(DRUG_CLASS_OPTIONS)
    ]
    return Clarification(
        field="drug_name",
        question=(
            f"'{term}' is a drug class, and ClinicalTrials.gov has no class list. These are the drugs "
            f"most often found in trials mentioning '{term}'. Which ones do you mean?"
        ),
        options=options,
        multi_select=True,
        allow_free_text=True,
    )


def sponsor_ambiguity(trials: list[Trial], term: str) -> Clarification | None:
    """Ask when a sponsor name matches several distinct lead sponsors (e.g. 'Merck')."""
    counts = Counter(t.lead_sponsor for t in trials if t.lead_sponsor)
    total = sum(counts.values())
    if total == 0:
        return None
    major = [(name, n) for name, n in counts.most_common() if n / total >= SPONSOR_AMBIGUITY_MIN_SHARE]
    if len(major) < 2:
        return None
    options = [ClarificationOption(label=f"{name} ({n} trials)", value=name, trial_count=n) for name, n in major[:4]]
    return Clarification(
        field="sponsor",
        question=f"'{term}' matches more than one lead sponsor. Which one do you mean?",
        options=options,
        multi_select=False,
    )
