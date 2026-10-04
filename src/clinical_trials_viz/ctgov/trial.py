"""A typed view of one ClinicalTrials.gov study record, and the values each Dimension reads from it."""

import re
from dataclasses import dataclass, field
from typing import Any

from clinical_trials_viz.catalog import (
    DRUG_INTERVENTION_TYPES,
    NON_DRUG_TERMS,
    NOT_REPORTED,
    PHASE_LABELS,
    SPONSOR_CLASS_LABELS,
    Dimension,
)


@dataclass(frozen=True)
class Intervention:
    type: str
    name: str
    other_names: tuple[str, ...]


@dataclass(frozen=True)
class Trial:
    nct_id: str
    title: str
    overall_status: str | None
    start_date: str | None  # "YYYY-MM-DD" or "YYYY-MM"
    start_date_type: str | None  # "ACTUAL" or "ESTIMATED"
    primary_completion_date: str | None
    completion_date: str | None
    phases: tuple[str, ...]
    study_type: str | None
    lead_sponsor: str | None
    lead_sponsor_class: str | None
    collaborators: tuple[str, ...]
    interventions: tuple[Intervention, ...]
    intervention_mesh_terms: tuple[str, ...]
    conditions: tuple[str, ...]
    condition_mesh_terms: tuple[str, ...]
    countries: tuple[str, ...]  # distinct, current site locations
    removed_countries: tuple[str, ...]
    enrollment: int | None
    enrollment_type: str | None
    raw: dict[str, Any] = field(repr=False, compare=False, hash=False)

    @property
    def start_year(self) -> int | None:
        return int(self.start_date[:4]) if self.start_date else None


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in values if v))


def parse_trial(study: dict[str, Any]) -> Trial:
    p = study.get("protocolSection", {})
    d = study.get("derivedSection", {})
    status = p.get("statusModule", {})
    sponsors = p.get("sponsorCollaboratorsModule", {})
    design = p.get("designModule", {})
    arms = p.get("armsInterventionsModule", {})
    return Trial(
        nct_id=_get(p, "identificationModule", "nctId"),
        title=_get(p, "identificationModule", "briefTitle") or "",
        overall_status=status.get("overallStatus"),
        start_date=_get(status, "startDateStruct", "date"),
        start_date_type=_get(status, "startDateStruct", "type"),
        primary_completion_date=_get(status, "primaryCompletionDateStruct", "date"),
        completion_date=_get(status, "completionDateStruct", "date"),
        phases=tuple(design.get("phases") or ()),
        study_type=design.get("studyType"),
        lead_sponsor=_get(sponsors, "leadSponsor", "name"),
        lead_sponsor_class=_get(sponsors, "leadSponsor", "class"),
        collaborators=_unique([c.get("name") for c in sponsors.get("collaborators") or []]),
        interventions=tuple(
            Intervention(i.get("type") or "OTHER", i.get("name") or "", tuple(i.get("otherNames") or ()))
            for i in arms.get("interventions") or []
        ),
        intervention_mesh_terms=_unique([m.get("term") for m in _get(d, "interventionBrowseModule", "meshes") or []]),
        conditions=tuple(_get(p, "conditionsModule", "conditions") or ()),
        condition_mesh_terms=_unique([m.get("term") for m in _get(d, "conditionBrowseModule", "meshes") or []]),
        countries=_unique([loc.get("country") for loc in _get(p, "contactsLocationsModule", "locations") or []]),
        removed_countries=tuple(_get(d, "miscInfoModule", "removedCountries") or ()),
        enrollment=_get(design, "enrollmentInfo", "count"),
        enrollment_type=_get(design, "enrollmentInfo", "type"),
        raw=study,
    )


_DOSE = re.compile(r"\b\d+(\.\d+)?\s*(mg|mcg|μg|g|ml|mg/kg|mg/m2|iu|units?)\b.*$", re.IGNORECASE)
_MARKS = re.compile(r"[®™]")


def clean_drug_name(name: str) -> str:
    """Normalize a raw intervention name: lower case, no dose, no trademark marks."""
    name = _MARKS.sub("", name)
    name = _DOSE.sub("", name)
    return " ".join(name.lower().split()).strip(" -,;:")


def _names(intervention: Intervention) -> list[str]:
    return [n for n in (clean_drug_name(x) for x in (intervention.name, *intervention.other_names)) if n]


def _mesh_for(intervention: Intervention, mesh_terms: list[str]) -> str | None:
    """The trial's MeSH term that names this intervention, if any."""
    names = _names(intervention)
    for term in mesh_terms:
        if any(term in name or name in term for name in names):
            return term
    return None


def drug_identities(trial: Trial) -> tuple[str, ...]:
    """The Drugs in a trial, one per drug-type intervention.

    MeSH terms are listed per trial and also cover procedures and diagnostics, so each term is
    matched to an intervention by name and kept only for drug-type interventions. A drug listed
    under another name (e.g. "MK-3475") is paired with the one unmatched MeSH term when exactly
    one of each remains; otherwise its cleaned raw name is used.
    """
    mesh = [t.lower() for t in trial.intervention_mesh_terms]
    drugs = [i for i in trial.interventions if i.type in DRUG_INTERVENTION_TYPES]
    matched = {id(i): _mesh_for(i, mesh) for i in trial.interventions}
    used = {m for m in matched.values() if m}
    unmatched_drugs = [i for i in drugs if matched[id(i)] is None]
    unmatched_mesh = [m for m in mesh if m not in used]
    if len(unmatched_drugs) == 1 and len(unmatched_mesh) == 1:
        matched[id(unmatched_drugs[0])] = unmatched_mesh[0]
    names = [matched[id(i)] or clean_drug_name(i.name) for i in drugs]
    return _unique([n for n in names if n and n not in NON_DRUG_TERMS])


def dimension_values(trial: Trial, dimension: Dimension) -> tuple[str, ...]:
    """Bucket labels a trial falls into. Never empty: missing data is its own state."""
    match dimension:
        case Dimension.START_YEAR:
            values: tuple[str, ...] = (str(trial.start_year),) if trial.start_year else ()
        case Dimension.PHASE:
            values = tuple(PHASE_LABELS.get(p, p) for p in trial.phases)
        case Dimension.STATUS:
            values = (trial.overall_status,) if trial.overall_status else ()
        case Dimension.COUNTRY:
            values = trial.countries
        case Dimension.LEAD_SPONSOR:
            values = (trial.lead_sponsor,) if trial.lead_sponsor else ()
        case Dimension.SPONSOR_CLASS:
            cls = trial.lead_sponsor_class
            values = (SPONSOR_CLASS_LABELS.get(cls, cls),) if cls else ()
        case Dimension.INTERVENTION_TYPE:
            values = _unique([i.type for i in trial.interventions])
        case Dimension.STUDY_TYPE:
            values = (trial.study_type,) if trial.study_type else ()
        case Dimension.CONDITION:
            values = trial.condition_mesh_terms or trial.conditions
        case Dimension.DRUG:
            values = drug_identities(trial)
    return values or (NOT_REPORTED,)


def dimension_evidence(trial: Trial, dimension: Dimension) -> Any:
    """The raw source value behind a trial's bucket, for the Citation."""
    match dimension:
        case Dimension.START_YEAR:
            return {"date": trial.start_date, "type": trial.start_date_type}
        case Dimension.PHASE:
            return list(trial.phases)
        case Dimension.STATUS:
            return trial.overall_status
        case Dimension.COUNTRY:
            return list(trial.countries)
        case Dimension.LEAD_SPONSOR:
            return trial.lead_sponsor
        case Dimension.SPONSOR_CLASS:
            return trial.lead_sponsor_class
        case Dimension.INTERVENTION_TYPE:
            return sorted({i.type for i in trial.interventions})
        case Dimension.STUDY_TYPE:
            return trial.study_type
        case Dimension.CONDITION:
            return list(trial.condition_mesh_terms or trial.conditions)
        case Dimension.DRUG:
            return list(trial.intervention_mesh_terms) or [i.name for i in trial.interventions]
