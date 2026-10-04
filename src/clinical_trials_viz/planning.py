"""The planning stage: the service's model steps, plus the code around them.

A split step (one model call) lists the separate questions a message asks, each rewritten to stand
alone; each is then planned on its own, exactly like a single question, so planning never has to
decide how to structure a multi-part answer. Every plan is gated, with one repair when the gate finds
errors. At most three planning calls, after the split call.
"""

from dataclasses import dataclass, field

from pydantic_ai import FallbackExceptionGroup, ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded

from clinical_trials_viz.models.plan import AnswerPlan, ClarifyPlan, MultiAnswerPlan, QueryPlan, UnsupportedPlan
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.planner import Planner, PlannerResult, PlannerTimeout, take_unreported_calls
from clinical_trials_viz.validate import GateResult, check_plan, separate_requests

MAX_MODEL_CALLS = 3  # planning calls per Run; the split step has its own (planner.SPLIT_MAX_CALLS)
MAX_PARTS = 3


@dataclass
class CallTally:
    """Model calls made while planning a Run. The caller holds it, so the count survives a failure."""

    calls: int = 0


@dataclass
class Planning:
    plan: QueryPlan
    request: QueryRequest  # the effective request: a refining Follow-up inherits earlier structured fields
    gates: list[GateResult | None] = field(default_factory=list)  # one per part; None when not an answer plan
    part_errors: list[BaseException | None] = field(default_factory=list)  # a part the model could not plan
    model_calls: int = 0
    model_name: str | None = None  # the model that produced the (last) plan
    notes: list[str] = field(default_factory=list)  # Assumptions about how the message was read
    warnings: list[str] = field(default_factory=list)  # e.g. the primary model failed and the fallback answered
    tally: CallTally = field(default_factory=CallTally)  # every model call of the Run, the split step's included

    def count(self, calls: int) -> None:
        self.model_calls += calls
        self.tally.calls += calls

    def record(self, result: PlannerResult) -> None:
        self.count(result.model_calls)
        self.model_name = result.model_name or self.model_name
        if result.model_failures:
            self.warnings.append(
                f"The primary model failed ({'; '.join(result.model_failures)}); "
                f"the fallback model ({result.model_name}) planned this question."
            )


# Planner failures that end one part of a multi-part message without stopping the others.
_PART_FAILURES = (ModelAPIError, FallbackExceptionGroup, PlannerTimeout, UnexpectedModelBehavior, UsageLimitExceeded)


async def plan_question(
    planner: Planner,
    request: QueryRequest,
    previous: QueryPlan | None,
    previous_request: QueryRequest | None,
    countries: set[str],
    tally: CallTally | None = None,
) -> Planning:
    """Plan a Question. Planner failures on a single question propagate (the caller classifies them);
    on a multi-part message they are kept per part in `part_errors`. `tally` counts every model call,
    including those of a step that then failed."""
    tally = tally if tally is not None else CallTally()
    if isinstance(previous, MultiAnswerPlan):
        # A Follow-up on a multi-part Run answers one of its parts (e.g. a Clarification for that part),
        # sent as that part's own request: plan it as a fresh question.
        previous = previous_request = None
    requests, warnings = await _split(planner, request.query, tally)
    if previous is None and request.previous_run_id is None:
        if len(requests) >= 2:
            planning = await _plan_parts(planner, request, requests, countries, tally)
        else:
            planning = await _plan_single(planner, request, previous, previous_request, countries, tally)
        planning.warnings[:0] = warnings
        return planning
    # A Follow-up refines one earlier answer, so it is never split: its first question is applied, and
    # any other question it asks is named in a note rather than dropped.
    notes = []
    if len(requests) >= 2:
        rest = "; ".join(f'"{r}"' for r in requests[1:])
        notes.append(
            f"This follow-up asks {len(requests)} things; only the first was applied to the earlier answer: "
            f'"{requests[0]}". Ask the rest as a new question: {rest}.'
        )
        request = request.model_copy(update={"query": requests[0]})
    planning = await _plan_single(planner, request, previous, previous_request, countries, tally)
    planning.warnings[:0] = warnings
    planning.notes[:0] = notes
    return planning


async def _split(planner: Planner, message: str, tally: CallTally) -> tuple[list[str], list[str]]:
    """The separate questions in a message, from the model's split step. If that step fails (an unusable
    reply, a provider error, a timeout), the code splitter is used instead and a warning says so; planning
    then reports a provider that is really down."""
    try:
        result = await planner.split(message)
    except _PART_FAILURES:
        tally.calls += take_unreported_calls()
        fallback = separate_requests(message)
        note = "The message could not be split by the model; it was split by simple rules instead"
        return fallback, [f"{note} ({len(fallback)} part(s))."]
    tally.calls += result.model_calls
    warnings = []
    if result.model_failures:
        warnings.append(
            f"The primary model failed ({'; '.join(result.model_failures)}); "
            f"the fallback model ({result.model_name}) split the message."
        )
    return result.requests, warnings


async def _plan_single(
    planner: Planner,
    request: QueryRequest,
    previous: QueryPlan | None,
    previous_request: QueryRequest | None,
    countries: set[str],
    tally: CallTally,
) -> Planning:
    result = await planner.plan(request.query, request.structured_fields(), previous, max_calls=MAX_MODEL_CALLS)
    planning = Planning(result.plan, request, tally=tally)
    planning.record(result)
    plan = result.plan
    if isinstance(plan, AnswerPlan) and plan.relation == "refine" and previous_request is not None:
        asked = set(request.structured_fields())
        planning.request = request = _inherit_fields(request, previous_request)
        if kept := sorted(set(request.structured_fields()) - asked):
            planning.notes.append(
                "Kept from the earlier question: " + ", ".join(f"{k} = {_show(request, k)}" for k in kept) + "."
            )
    plan, gate = await _gate_and_repair(planner, planning, plan, result, countries)
    planning.plan, planning.gates, planning.part_errors = plan, [gate], [None]
    return planning


async def _plan_parts(
    planner: Planner, request: QueryRequest, requests: list[str], countries: set[str], tally: CallTally
) -> Planning:
    notes = []
    if len(requests) > MAX_PARTS:
        notes.append(f"The message asks {len(requests)} things; only the first {MAX_PARTS} are answered.")
        requests = requests[:MAX_PARTS]
    structured = request.structured_fields()
    planning = Planning(UnsupportedPlan(reason="not planned yet"), request, notes=notes, tally=tally)
    parts: list[AnswerPlan | ClarifyPlan | UnsupportedPlan] = []
    for index, text in enumerate(requests):
        # Parts are planned one after another so they share the call budget: each later part keeps
        # at least one call, and an earlier part may use a spare call for the model's output retry.
        reserve = len(requests) - index - 1
        budget = MAX_MODEL_CALLS - planning.model_calls - reserve
        try:  # planning and repairing a part: a failure in either ends this part only
            result = await planner.plan(text, structured, None, max_calls=budget, context=request.query)
            planning.record(result)
            plan, gate = await _gate_and_repair(planner, planning, result.plan, result, countries, reserve)
        except _PART_FAILURES as exc:
            planning.count(take_unreported_calls())
            parts.append(UnsupportedPlan(reason=f"This part could not be planned: {text}"))
            planning.gates.append(None)
            planning.part_errors.append(exc)
            continue
        parts.append(plan)  # type: ignore[arg-type]  # parts are planned without a previous plan: never multi
        planning.gates.append(gate)
        planning.part_errors.append(None)
    planning.notes.insert(
        0,
        f"The question asks {len(requests)} separate things, each answered on its own with its own filters: "
        + "; ".join(f'({i}) "{text}"' for i, text in enumerate(requests, 1))
        + ".",
    )
    planning.plan = MultiAnswerPlan(requests=requests, parts=parts)
    return planning


async def _gate_and_repair(
    planner: Planner,
    planning: Planning,
    plan: QueryPlan,
    result: PlannerResult,
    countries: set[str],
    reserve: int = 0,
) -> tuple[QueryPlan, GateResult | None]:
    """Gate an answer plan and repair it once if calls remain (keeping `reserve` calls for later parts)."""
    if not isinstance(plan, AnswerPlan):
        return plan, None
    request = planning.request
    gate = check_plan(plan, request, countries)
    if gate.errors and planning.model_calls + reserve < MAX_MODEL_CALLS:
        result = await planner.plan(
            request.query,
            request.structured_fields(),
            None,
            repair=(result.messages, gate.errors),
            max_calls=MAX_MODEL_CALLS - planning.model_calls - reserve,
        )
        planning.record(result)
        plan = result.plan
        if not isinstance(plan, AnswerPlan):
            return plan, None
        gate = check_plan(plan, request, countries)
    if gate.errors:
        reason = "The question could not be turned into a valid plan: " + "; ".join(gate.errors)
        return UnsupportedPlan(reason=reason), None
    return plan, gate


def _inherit_fields(request: QueryRequest, previous: QueryRequest) -> QueryRequest:
    """A refining Follow-up keeps the earlier request's structured fields (e.g. a Clarification answer)
    unless the new request sets them."""
    merged = {**previous.structured_fields(), **request.structured_fields()}
    return QueryRequest.model_validate({**merged, "query": request.query, "previous_run_id": request.previous_run_id})


def _show(request: QueryRequest, field_name: str) -> str:
    value = getattr(request, field_name)
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)
