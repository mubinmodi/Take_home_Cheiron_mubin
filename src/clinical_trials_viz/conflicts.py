"""Conflicts: a structured field that names a different value from the question for the same Filter.
The whole rule (harness-design: never pick one silently) lives here:

- detection: each pinned field compared with what the question said (aliases such as "USA" and
  "United States" agree; a sponsor term agrees with exact names that all contain it);
- a brand and its generic name are the same drug, not a Conflict (checked with the registry);
- an answer is final: a field the previous Run asked about settles it while the conversation
  continues that question (a Clarification answer resends it, a refinement continues it);
- otherwise one Clarification, with both values as valid options of the field.

The fields and their names come from the capability catalog (`catalog.PINNED_FIELDS`)."""

from dataclasses import dataclass, field

from clinical_trials_viz import clarify
from clinical_trials_viz.catalog import COUNTRY_ALIASES, PHASE_LABELS, PINNED_FIELDS
from clinical_trials_viz.ctgov.client import CtGovClient
from clinical_trials_viz.models.plan import AnswerPlan
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.models.response import Clarification, ClarificationOption

FieldValue = list[str] | int  # a structured field's value: names or enum values, or a year


@dataclass
class Conflict:
    """A structured field that contradicts the question, with both values as the field takes them."""

    request_field: str  # e.g. "trial_phase"
    question_value: FieldValue  # e.g. ["PHASE3"]
    field_value: FieldValue  # e.g. ["PHASE2"]


@dataclass
class PreviousRun:
    """What the Run a Follow-up continues tells the Conflict rule."""

    answered: dict[str, str | None] = field(default_factory=dict)  # request field -> its Clarification's reason
    queries: set[str] = field(default_factory=set)  # its question and the questions of its parts


@dataclass
class Resolution:
    """The Conflicts settled: Assumptions to report, or the one Clarification to ask."""

    assumptions: list[str] = field(default_factory=list)
    clarification: Clarification | None = None


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
    """Filters where the question names a different value from the structured field."""
    conflicts = []
    for request_field, (filter_field, _) in PINNED_FIELDS.items():
        asked, given = getattr(plan.filters, filter_field), getattr(request, request_field)
        if asked and given and _differs(request_field, asked, given):
            conflicts.append(Conflict(request_field, _request_value(asked), _request_value(given)))
    return conflicts


def show_value(value: FieldValue) -> str:
    """A request field value as the user reads it: 'Phase 3', 'Germany, France', '2020'."""
    values = value if isinstance(value, list) else [value]
    return ", ".join(PHASE_LABELS.get(str(v), str(v)) for v in values)


def conflict_question(conflict: Conflict) -> Clarification:
    """Ask which value is meant. Both options are valid values of the request field, built by code."""
    name, asked, given = (
        PINNED_FIELDS[conflict.request_field][1],
        show_value(conflict.question_value),
        show_value(conflict.field_value),
    )
    return Clarification(
        field=conflict.request_field,
        reason="conflict",
        question=f"Your question says {asked}, but your filters say {given}. Which {name} do you mean?",
        options=[
            ClarificationOption(label=f"{asked} (your question)", value=conflict.question_value),
            ClarificationOption(label=f"{given} (your filters)", value=conflict.field_value),
        ],
    )


async def resolve(
    conflicts: list[Conflict],
    plan: AnswerPlan,
    request: QueryRequest,
    previous: PreviousRun,
    client: CtGovClient,
) -> Resolution:
    """Settle the Conflicts of one part: the first one that still needs the user ends in a Clarification."""
    resolution = Resolution()
    continuing = plan.relation == "refine" or request.query in previous.queries
    for conflict in conflicts:
        asked, given = show_value(conflict.question_value), show_value(conflict.field_value)
        if continuing and conflict.request_field in previous.answered:  # e.g. which Merck, or which of two values
            if previous.answered[conflict.request_field] == "conflict":
                resolution.assumptions.append(f"Used {given}, as you chose; the question says {asked}.")
            continue
        names, others = conflict.question_value, conflict.field_value
        same_drug = (
            conflict.request_field == "drug_name"
            and isinstance(names, list)
            and isinstance(others, list)
            and await clarify.same_drug(names, others, client)
        )
        if same_drug:
            resolution.assumptions.append(
                f"'{asked}' in the question and '{given}' in your filters are the same drug ({same_drug})."
            )
            continue
        resolution.clarification = conflict_question(conflict)
        return resolution
    return resolution
