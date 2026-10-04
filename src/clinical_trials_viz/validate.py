"""Semantic gate: merge the plan with structured fields and check it against the catalog."""

import re
from dataclasses import dataclass, field

from clinical_trials_viz.catalog import COUNTRY_ALIASES, DIMENSIONS, MAX_COMPARE_SIDES, PINNED_FIELDS, Dimension
from clinical_trials_viz.models.plan import AnswerPlan, Operation
from clinical_trials_viz.models.request import NCT_ID_PATTERN, QueryRequest
from clinical_trials_viz.models.response import AppliedFilters

FieldValue = list[str] | int  # a structured field's value: names or enum values, or a year


@dataclass
class Conflict:
    """A structured field that contradicts the question, with both values as the field takes them."""

    request_field: str  # e.g. "trial_phase"
    question_value: FieldValue  # e.g. ["PHASE3"]
    field_value: FieldValue  # e.g. ["PHASE2"]


@dataclass
class GateResult:
    filters: AppliedFilters
    errors: list[str] = field(default_factory=list)  # fixable by the model (repair once)
    unsupported: str | None = None  # valid plan the service cannot run yet
    assumptions: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)  # the structured field is applied until answered


# Sponsor categories are values of the sponsor_class dimension, never sponsor names.
_SPONSOR_CATEGORIES = frozenset({
    "industry", "industrial", "pharma", "pharmaceutical", "pharmaceutical companies", "companies", "company",
    "academic", "academia", "university", "universities", "academic centers", "nih", "government", "federal",
    "other government", "network", "networks", "individual", "individuals", "other",
})  # fmt: skip
_CATEGORY_HINT = (
    "sponsor category, not a sponsor name: use group_by (or compare by) 'sponsor_class' instead, "
    "with the conditions or drugs as the compared sides"
)


def _is_sponsor_category(name: str) -> bool:
    words = name.strip().lower().removesuffix(" sponsors").removesuffix(" sponsored").removesuffix("-sponsored")
    return words in _SPONSOR_CATEGORIES


def normalize_country(name: str, known: set[str]) -> str | None:
    if name in known:
        return name
    alias = COUNTRY_ALIASES.get(name.strip().lower())
    if alias:
        return alias
    by_lower = {k.lower(): k for k in known}
    return by_lower.get(name.strip().lower())


def _confirms_question_sponsor(plan: AnswerPlan, request: QueryRequest) -> bool:
    """The structured sponsor repeats the question's own sponsor term (e.g. the "your question" answer
    to a conflict): search it as a lead sponsor name, so an ambiguous name is still asked about."""
    asked = (plan.filters.sponsor or "").strip().lower()
    return bool(asked) and [s.strip().lower() for s in request.sponsor or []] == [asked]


def merge_filters(plan: AnswerPlan, request: QueryRequest) -> AppliedFilters:
    """Structured request fields are authoritative; the plan fills everything else."""
    applied = AppliedFilters(**plan.filters.model_dump())
    for request_field, (filter_field, _) in PINNED_FIELDS.items():
        value = getattr(request, request_field)
        if value and request_field != "sponsor":
            setattr(applied, filter_field, value)
            applied.from_request.append(filter_field)
    if request.sponsor and _confirms_question_sponsor(plan, request):
        applied.from_request.append("sponsor")
    elif request.sponsor:  # structured sponsors are exact lead sponsor names (e.g. a Clarification answer)
        applied.exact_sponsors = list(request.sponsor)
        applied.sponsor, applied.sponsor_role, applied.sponsor_exact = " or ".join(request.sponsor), "lead", True
        applied.from_request.append("sponsor")
    return applied


def _as_set(value: object) -> set[str]:
    values = value if isinstance(value, list) else [value]
    words = (str(getattr(v, "value", v)).strip().lower() for v in values)
    return {COUNTRY_ALIASES.get(w, w).lower() for w in words}  # "USA" and "United States" agree


def _request_value(value: object) -> FieldValue:
    """A plan filter value in the form its structured request field takes (phases as 'PHASE3')."""
    if isinstance(value, int):
        return value
    values = value if isinstance(value, list) else [value]
    return [str(getattr(v, "value", v)) for v in values]


def _differs(request_field: str, asked: object, given: object) -> bool:
    if request_field == "sponsor":  # a term vs exact names: "Merck" agrees with "Merck Sharp & Dohme LLC"
        term = str(asked).strip().lower()  # every name must match it: ["Merck", "Pfizer"] would widen "Merck"
        return not all(term in name or name in term for name in _as_set(given))
    return _as_set(asked) != _as_set(given)


def find_conflicts(plan: AnswerPlan, request: QueryRequest) -> list[Conflict]:
    """Filters where the question names a different value from the structured field. Code asks which
    one is meant (catalog.PINNED_FIELDS); aliases such as "USA" and "United States" agree."""
    conflicts = []
    for request_field, (filter_field, _) in PINNED_FIELDS.items():
        asked, given = getattr(plan.filters, filter_field), getattr(request, request_field)
        if asked and given and _differs(request_field, asked, given):
            conflicts.append(Conflict(request_field, _request_value(asked), _request_value(given)))
    return conflicts


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
            if side.sponsor and _is_sponsor_category(side.sponsor):
                errors.append(f"compare_sides[{i}]: '{side.sponsor}' is a {_CATEGORY_HINT}")
    elif plan.compare_sides:
        errors.append("compare_sides is only allowed with operation 'compare'")

    if plan.view is not None and plan.operation is not Operation.PER_TRIAL:
        errors.append("view is only allowed with operation 'per_trial'")
    if plan.series_by is not None:
        if plan.operation is not Operation.AGGREGATE or plan.group_by is None:
            errors.append("series_by needs operation 'aggregate' with a group_by")
        elif plan.series_by == plan.group_by:
            errors.append("series_by must differ from group_by")
    if plan.filters.sponsor and _is_sponsor_category(plan.filters.sponsor):
        errors.append(f"sponsor '{plan.filters.sponsor}' is a {_CATEGORY_HINT}")
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

    if filters.keywords:
        result.assumptions.append(
            "Matched as free text anywhere in the trial record (not a specific field): "
            + ", ".join(f"'{k}'" for k in filters.keywords)
            + "."
        )
    if plan.group_by is Dimension.COUNTRY:
        result.assumptions.append("Countries count trials with a current site there; a trial counts once per country.")
        if filters.statuses:
            result.assumptions.append(
                "Status filters use the trial's overall status; its sites are counted whatever their own status."
            )
    result.conflicts = find_conflicts(plan, request)
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
