"""Semantic gate: merge the plan with structured fields and check it against the catalog."""

import re
from dataclasses import dataclass, field

from clinical_trials_viz.catalog import COUNTRY_ALIASES, DIMENSIONS, MAX_COMPARE_SIDES, Dimension
from clinical_trials_viz.models.plan import AnswerPlan, Operation
from clinical_trials_viz.models.request import NCT_ID_PATTERN, QueryRequest
from clinical_trials_viz.models.response import AppliedFilters


@dataclass
class GateResult:
    filters: AppliedFilters
    errors: list[str] = field(default_factory=list)  # fixable by the model (repair once)
    unsupported: str | None = None  # valid plan the service cannot run yet
    assumptions: list[str] = field(default_factory=list)


def normalize_country(name: str, known: set[str]) -> str | None:
    if name in known:
        return name
    alias = COUNTRY_ALIASES.get(name.strip().lower())
    if alias:
        return alias
    by_lower = {k.lower(): k for k in known}
    return by_lower.get(name.strip().lower())


# Structured request field -> applied filter it pins.
_PINNED_FIELDS = {
    "drug_name": "drugs",
    "condition": "conditions",
    "trial_phase": "phases",
    "country": "countries",
    "status": "statuses",
    "start_year": "start_year_from",
    "end_year": "start_year_to",
    "nct_id": "nct_ids",
}


def merge_filters(plan: AnswerPlan, request: QueryRequest) -> AppliedFilters:
    """Structured request fields are authoritative; the plan fills everything else."""
    applied = AppliedFilters(**plan.filters.model_dump())
    for request_field, filter_field in _PINNED_FIELDS.items():
        value = getattr(request, request_field)
        if value:
            setattr(applied, filter_field, value)
            applied.from_request.append(filter_field)
    if request.sponsor:  # structured sponsors are exact lead sponsor names (e.g. a Clarification answer)
        applied.exact_sponsors = list(request.sponsor)
        applied.sponsor, applied.sponsor_role, applied.sponsor_exact = " or ".join(request.sponsor), "lead", True
        applied.from_request.append("sponsor")
    return applied


def check_plan(plan: AnswerPlan, request: QueryRequest, known_countries: set[str]) -> GateResult:
    filters = merge_filters(plan, request)
    result = GateResult(filters)
    errors = result.errors

    if plan.operation is Operation.BIN and plan.group_by is not None:
        errors.append("bin makes an enrollment histogram; set group_by to null")
    if plan.operation is Operation.RELATE:
        if plan.network is None:
            errors.append("relate needs network: sponsor_drug or drug_drug")
        if plan.group_by is not None:
            errors.append("relate does not use group_by; set it to null")
    elif plan.network is not None:
        errors.append("network is only allowed with operation 'relate'")

    if plan.operation is Operation.COMPARE:
        sides = plan.compare_sides
        if not 2 <= len(sides) <= MAX_COMPARE_SIDES:
            errors.append(f"compare needs 2 to {MAX_COMPARE_SIDES} compare_sides, got {len(sides)}")
        for i, side in enumerate(sides):
            if sum(v is not None for v in (side.drug, side.condition, side.sponsor)) != 1:
                errors.append(f"compare_sides[{i}] must set exactly one of drug, condition, sponsor")
    elif plan.compare_sides:
        errors.append("compare_sides is only allowed with operation 'compare'")

    if plan.view is not None and plan.operation is not Operation.PER_TRIAL:
        errors.append("view is only allowed with operation 'per_trial'")
    if plan.series_by is not None:
        if plan.operation is not Operation.AGGREGATE or plan.group_by is None:
            errors.append("series_by needs operation 'aggregate' with a group_by")
        elif plan.series_by == plan.group_by:
            errors.append("series_by must differ from group_by")
    if plan.operation is Operation.PER_TRIAL and plan.group_by is not None:
        errors.append("per_trial lists trials; set group_by to null")
    if plan.group_by is not None and plan.group_by not in DIMENSIONS:
        errors.append(f"unknown group_by {plan.group_by}")

    for nct in filters.nct_ids:
        if not NCT_ID_PATTERN.match(nct):
            errors.append(f"nct_ids: {nct!r} is not an NCT ID")
    y0, y1 = filters.start_year_from, filters.start_year_to
    if y0 and y1 and y0 > y1:
        errors.append("start_year_from is after start_year_to")

    countries = []
    for name in filters.countries:
        match = normalize_country(name, known_countries) if known_countries else name
        if match is None:
            errors.append(f"unknown country {name!r}; use the English country name")
        else:
            countries.append(match)
            if match != name:
                result.assumptions.append(f"Country '{name}' was read as '{match}'.")
    filters.countries = countries

    if plan.group_by is Dimension.COUNTRY:
        result.assumptions.append("Countries count trials with a current site there; a trial counts once per country.")
    if filters.sponsor and not filters.sponsor_exact and filters.sponsor_role == "lead":
        result.assumptions.append(f"'{filters.sponsor}' is matched as the lead sponsor (collaborators not included).")
    return result


# A second request inside one message: a joiner ("and", "also", ";", "?") followed closely by a request word.
_SECOND_REQUEST = re.compile(
    r"(?:\?|;|,?\s+and\s+(?:also\s+|then\s+)?|,?\s+as well as\s+|\balso\s+)"
    r"(?=(?:please\s+)?(?:show|list|plot|draw|map|chart|display|give|compare|what|which|how many|how|who|where)\b)"
    r"|,?\s+and\s+also\s+(?=by\b)|\s+and\s+(?=by\b)",  # a second breakdown: "by phase and (also) by year"
    re.IGNORECASE,
)


def separate_requests(question: str) -> list[str]:
    """Split a message where it appears to start a new request. A hint for the planner's repair turn,
    never a decision: "how many X, and which countries?" splits here but is one question."""
    parts = [p.strip(" ,.?;") for p in _SECOND_REQUEST.split(question)]
    return [p for p in parts if len(p.split()) >= 2]
