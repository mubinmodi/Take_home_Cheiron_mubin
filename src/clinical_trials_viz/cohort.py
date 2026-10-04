"""Turn applied Filters into API requests and a Cohort: complete retrieval plus match checks."""

from collections import Counter
from dataclasses import dataclass, field

from clinical_trials_viz.catalog import DRUG_IDENTITY_MIN_SHARE, TRIAL_FIELDS
from clinical_trials_viz.ctgov.client import CtGovClient
from clinical_trials_viz.ctgov.trial import Trial, clean_drug_name, parse_trial
from clinical_trials_viz.models.response import AppliedFilters


@dataclass
class Cohort:
    trials: list[Trial]
    search_matches: int
    assumptions: list[str] = field(default_factory=list)


def _quote(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


def build_params(filters: AppliedFilters, drug: str | None = None) -> dict[str, str]:
    """Compile Filters into ClinicalTrials.gov API v2 query parameters.

    Drugs are searched one at a time (see `fetch_cohort`), so only one drug is passed in.
    """
    params: dict[str, str] = {}
    advanced: list[str] = []
    if drug:
        params["query.intr"] = drug
    if filters.conditions:
        params["query.cond"] = " OR ".join(f"({c})" for c in filters.conditions)
    if filters.keywords:  # every keyword must appear somewhere in the trial record
        params["query.term"] = " AND ".join(f"({k})" for k in filters.keywords)
    if filters.sponsor:
        if filters.sponsor_role == "any":
            params["query.spons"] = filters.sponsor
        elif filters.sponsor_exact:
            names = filters.exact_sponsors or [filters.sponsor]
            advanced.append("AREA[LeadSponsorName](" + " OR ".join(_quote(n) for n in names) + ")")
        else:
            advanced.append(f"AREA[LeadSponsorName]({filters.sponsor})")
    if filters.phases:
        advanced.append("AREA[Phase](" + " OR ".join(filters.phases) + ")")
    if filters.study_types:
        advanced.append("AREA[StudyType](" + " OR ".join(filters.study_types) + ")")
    if filters.countries:
        advanced.append("AREA[LocationCountry](" + " OR ".join(_quote(c) for c in filters.countries) + ")")
    if filters.start_year_from or filters.start_year_to:
        start = f"{filters.start_year_from}-01-01" if filters.start_year_from else "MIN"
        end = f"{filters.start_year_to}-12-31" if filters.start_year_to else "MAX"
        advanced.append(f"AREA[StartDate]RANGE[{start},{end}]")
    if filters.statuses:
        params["filter.overallStatus"] = ",".join(filters.statuses)
    if filters.nct_ids:
        params["filter.ids"] = ",".join(filters.nct_ids)
    if advanced:
        params["filter.advanced"] = " AND ".join(advanced)
    return params


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
    """Match check: the drug is one of the trial's interventions, not just mentioned elsewhere."""
    if identity and identity in (t.lower() for t in trial.intervention_mesh_terms):
        return True
    wanted = {clean_drug_name(name)} | ({identity} if identity else set())
    for intervention in trial.interventions:
        names = [intervention.name, *intervention.other_names]
        if any(w in clean_drug_name(n) for n in names for w in wanted if w):
            return True
    return False


async def fetch_cohort(client: CtGovClient, filters: AppliedFilters, extra_fields: list[str] | None = None) -> Cohort:
    """Retrieve every matching trial; raises ScopeTooLarge instead of sampling."""
    fields = [*TRIAL_FIELDS, *(extra_fields or [])]
    if not filters.drugs:
        result = await client.search(build_params(filters), fields=fields)
        trials = [parse_trial(s) for s in result.studies]
        cohort = Cohort(trials, result.total)
    else:
        by_id: dict[str, Trial] = {}
        matches = 0
        assumptions: list[str] = []
        for drug in filters.drugs:
            result = await client.search(build_params(filters, drug), fields=fields)
            found = [parse_trial(s) for s in result.studies]
            matches += result.total
            identity = resolve_drug_identity(found)
            kept = [t for t in found if lists_drug(t, drug, identity)]
            if identity and identity != drug.lower():
                assumptions.append(f"'{drug}' was matched as the standard drug name '{identity}' (MeSH).")
            if dropped := len(found) - len(kept):
                assumptions.append(
                    f"{dropped} of {len(found)} search matches for '{drug}' were excluded because "
                    f"'{drug}' is not one of their interventions."
                )
            by_id.update((t.nct_id, t) for t in kept)
        cohort = Cohort(list(by_id.values()), matches, assumptions)

    if filters.sponsor and filters.sponsor_exact:
        wanted = {n.lower() for n in (filters.exact_sponsors or [filters.sponsor])}
        cohort.trials = [t for t in cohort.trials if (t.lead_sponsor or "").lower() in wanted]
    return cohort
