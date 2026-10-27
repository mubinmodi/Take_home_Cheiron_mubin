"""Build Clarifications. Options always come from the data or the user's own words, never the model."""

from collections import Counter

from clinical_trials_viz.catalog import DRUG_CLASS_OPTIONS, SPONSOR_AMBIGUITY_MIN_SHARE
from clinical_trials_viz.cohort import resolve_drug_identity
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
    return Clarification(field=field, question=plan.question, allow_free_text=True)


async def same_drug(names: list[str], others: list[str], client: CtGovClient) -> str | None:
    """The standard drug name when two sets of drug names are the same drug (e.g. Keytruda and
    pembrolizumab both resolve to the MeSH term 'pembrolizumab'); None when they differ or are unknown."""
    identities = []
    for group in (names, others):
        found = set()
        for name in group:
            result = await client.search({"query.intr": name}, max_pages=1, allow_partial=True)
            identity = resolve_drug_identity([parse_trial(s) for s in result.studies])
            if identity is None:
                return None
            found.add(identity)
        identities.append(found)
    return ", ".join(sorted(identities[0])) if identities[0] == identities[1] else None


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
    shown = major[:4]
    options = [ClarificationOption(label=f"{name} ({n} trials)", value=name, trial_count=n) for name, n in shown]
    combined = sum(n for _, n in shown)  # each trial has one lead sponsor, so counts add up
    options.append(ClarificationOption(label=f"All of these ({combined} trials)", value=[name for name, _ in shown],
                                       trial_count=combined))  # fmt: skip
    return Clarification(
        field="sponsor",
        question=f"'{term}' matches more than one lead sponsor. Which one do you mean?",
        options=options,
        multi_select=False,
    )


def sponsor_sides_ambiguity(questions: list[tuple[str, Clarification]]) -> Clarification:
    """One question for every comparison side whose sponsor term matches several lead sponsors ("Merck
    vs Pfizer"), so all the answers arrive together: each chosen name narrows the side it belongs to."""
    if len(questions) == 1:
        return questions[0][1]
    options = [o for _, q in questions for o in q.options if not isinstance(o.value, list)]  # no "All of these"
    terms = " and ".join(f"'{term}'" for term, _ in questions)
    return Clarification(
        field="sponsor",
        question=f"{terms} each match more than one lead sponsor. Choose the ones you mean (at least one for each).",
        options=options,
        multi_select=True,
    )
