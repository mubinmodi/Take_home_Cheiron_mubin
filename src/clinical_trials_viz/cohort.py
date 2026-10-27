"""Retrieve a Cohort: every page the applied Filters select (filters.py builds the API parameters), plus
the drug match check."""

from collections import Counter
from dataclasses import dataclass, field

from clinical_trials_viz.catalog import (
    DRUG_IDENTITY_MIN_SHARE,
    DRUG_INTERVENTION_TYPES,
    KEYWORD_TEXT_FIELDS,
    TRIAL_FIELDS,
)
from clinical_trials_viz.ctgov.client import CtGovClient
from clinical_trials_viz.ctgov.trial import Intervention, Trial, clean_drug_name, parse_trial
from clinical_trials_viz.models.response import AppliedFilters


@dataclass
class Cohort:
    trials: list[Trial]
    search_matches: int
    assumptions: list[str] = field(default_factory=list)


def resolve_drug_identity(trials: list[Trial]) -> str | None:
    """The MeSH term shared by most search matches for a drug name, e.g. Keytruda -> pembrolizumab."""
    if not trials:
        return None
    counts = Counter(term.lower() for t in trials for term in t.intervention_mesh_terms)
    if not counts:
        return None
    term, n = counts.most_common(1)[0]
    return term if n / len(trials) >= DRUG_IDENTITY_MIN_SHARE else None


def lists_drug(trial: Trial, name: str, identity: str | None) -> bool:
    """Match check: the drug is one of the trial's drug-type interventions, not just mentioned elsewhere.

    A drug-type intervention that names the drug (name or other names) matches. MeSH terms are listed
    per trial, not per intervention, so a MeSH match counts only when no non-drug intervention is the
    one naming the drug (e.g. pembrolizumab recorded as a PROCEDURE) and the trial has a drug-type
    intervention for it to belong to (e.g. a DRUG listed only by its code, "MK-3475").
    """
    wanted = {w for w in (clean_drug_name(name), identity) if w}

    def names_it(intervention: Intervention) -> bool:
        names = [intervention.name, *intervention.other_names]
        return any(w in clean_drug_name(n) for n in names for w in wanted)

    drugs = [i for i in trial.interventions if i.type in DRUG_INTERVENTION_TYPES]
    if any(names_it(i) for i in drugs):
        return True
    others = [i for i in trial.interventions if i.type not in DRUG_INTERVENTION_TYPES]
    in_mesh = identity is not None and identity in (t.lower() for t in trial.intervention_mesh_terms)
    return bool(drugs) and in_mesh and not any(names_it(i) for i in others)


async def fetch_cohort(client: CtGovClient, filters: AppliedFilters, extra_fields: list[str] | None = None) -> Cohort:
    """Retrieve every matching trial; raises ScopeTooLarge instead of sampling."""
    fields = [*TRIAL_FIELDS, *(extra_fields or []), *(KEYWORD_TEXT_FIELDS if filters.keywords else [])]
    if not filters.drugs:
        result = await client.search(filters.api_params(), fields=fields)
        trials = [parse_trial(s) for s in result.studies]
        cohort = Cohort(trials, result.total)
    else:
        by_id: dict[str, Trial] = {}
        matches = 0
        assumptions: list[str] = []
        for drug in filters.drugs:
            result = await client.search(filters.api_params(drug), fields=fields)
            found = [parse_trial(s) for s in result.studies]
            matches += result.total
            identity = resolve_drug_identity(found)
            kept = [t for t in found if lists_drug(t, drug, identity)]
            if identity and identity != drug.lower():
                assumptions.append(f"'{drug}' was matched as the standard drug name '{identity}' (MeSH).")
            if dropped := len(found) - len(kept):
                assumptions.append(
                    f"{dropped} of {len(found)} search matches for '{drug}' were excluded because "
                    f"'{drug}' is not one of their drug interventions."
                )
            by_id.update((t.nct_id, t) for t in kept)
        cohort = Cohort(list(by_id.values()), matches, assumptions)

    if names := filters.exact_sponsor_names():
        cohort.trials = [t for t in cohort.trials if (t.lead_sponsor or "").lower() in names]
    return cohort
