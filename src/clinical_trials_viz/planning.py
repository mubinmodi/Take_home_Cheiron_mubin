"""The planning stage: the service's only model step, plus the code around it.

A message that asks several separate things is split by code and each request is planned on its own,
exactly like a single question; the model never has to decide how to structure a multi-part answer.
Every plan is then gated, with one repair when the gate finds errors. At most three model calls.
"""

from dataclasses import dataclass, field

from pydantic_ai import UnexpectedModelBehavior, UsageLimitExceeded

from clinical_trials_viz.models.plan import AnswerPlan, ClarifyPlan, MultiAnswerPlan, QueryPlan, UnsupportedPlan
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.planner import Planner, PlannerResult
from clinical_trials_viz.validate import GateResult, check_plan, separate_requests

MAX_MODEL_CALLS = 3
MAX_PARTS = 3


@dataclass
class Planning:
    plan: QueryPlan
    gates: list[GateResult | None]  # one per part (a single plan is one part); None for clarify/unsupported
    request: QueryRequest  # the effective request: a refining Follow-up inherits earlier structured fields
    model_calls: int = 0
    notes: list[str] = field(default_factory=list)  # Assumptions about how the message was read


async def plan_question(
    planner: Planner,
    request: QueryRequest,
    previous: QueryPlan | None,
    previous_request: QueryRequest | None,
    countries: set[str],
) -> Planning:
    if isinstance(previous, MultiAnswerPlan):
        # A Follow-up on a multi-part Run answers one of its parts (e.g. a Clarification for that part),
        # sent as that part's own request: plan it as a fresh question.
        previous = previous_request = None
    if previous is None and request.previous_run_id is None:  # Follow-ups are never split
        requests = separate_requests(request.query)
        if len(requests) >= 2:
            return await _plan_parts(planner, request, requests, countries)
    return await _plan_single(planner, request, previous, previous_request, countries)


async def _plan_single(
    planner: Planner,
    request: QueryRequest,
    previous: QueryPlan | None,
    previous_request: QueryRequest | None,
    countries: set[str],
) -> Planning:
    result = await planner.plan(request.query, request.structured_fields(), previous, max_calls=MAX_MODEL_CALLS)
    planning = Planning(result.plan, [], request, result.model_calls)
    plan = result.plan
    if isinstance(plan, AnswerPlan) and plan.relation == "refine" and previous_request is not None:
        asked = set(request.structured_fields())
        planning.request = request = _inherit_fields(request, previous_request)
        if kept := sorted(set(request.structured_fields()) - asked):
            planning.notes.append(
                "Kept from the earlier question: " + ", ".join(f"{k} = {_show(request, k)}" for k in kept) + "."
            )
    plan, gate, calls = await _gate_and_repair(planner, plan, request, countries, result, planning.model_calls)
    planning.plan, planning.gates, planning.model_calls = plan, [gate], planning.model_calls + calls
    return planning


async def _plan_parts(planner: Planner, request: QueryRequest, requests: list[str], countries: set[str]) -> Planning:
    notes = []
    if len(requests) > MAX_PARTS:
        notes.append(f"The message asks {len(requests)} things; only the first {MAX_PARTS} are answered.")
        requests = requests[:MAX_PARTS]
    structured = request.structured_fields()
    calls = 0
    parts: list[AnswerPlan | ClarifyPlan | UnsupportedPlan] = []
    gates: list[GateResult | None] = []
    for index, text in enumerate(requests):
        # Parts are planned one after another so they share the call budget: each later part keeps
        # at least one call, and an earlier part may use a spare call for the model's output retry.
        budget = MAX_MODEL_CALLS - calls - (len(requests) - index - 1)
        try:
            result = await planner.plan(text, structured, None, max_calls=budget, context=request.query)
        except (UsageLimitExceeded, UnexpectedModelBehavior):
            calls += budget
            parts.append(UnsupportedPlan(reason=f"This part could not be planned: {text}"))
            gates.append(None)
            continue
        calls += result.model_calls
        reserve = len(requests) - index - 1  # calls kept for the parts still to plan
        plan, gate, used = await _gate_and_repair(planner, result.plan, request, countries, result, calls, reserve)
        calls += used
        parts.append(plan)  # type: ignore[arg-type]  # parts are planned without a previous plan: never multi
        gates.append(gate)
    notes.insert(
        0,
        f"The question asks {len(requests)} separate things, each answered on its own with its own filters: "
        + "; ".join(f'({i}) "{text}"' for i, text in enumerate(requests, 1))
        + ".",
    )
    return Planning(MultiAnswerPlan(requests=requests, parts=parts), gates, request, calls, notes)


async def _gate_and_repair(
    planner: Planner,
    plan: QueryPlan,
    request: QueryRequest,
    countries: set[str],
    result: PlannerResult,
    calls_so_far: int,
    reserve: int = 0,
) -> tuple[QueryPlan, GateResult | None, int]:
    """Gate an answer plan; repair it once if calls remain. Returns the plan, its gate and the calls used."""
    if not isinstance(plan, AnswerPlan):
        return plan, None, 0
    gate = check_plan(plan, request, countries)
    used = 0
    if gate.errors and calls_so_far + reserve < MAX_MODEL_CALLS:
        result = await planner.plan(
            request.query,
            request.structured_fields(),
            None,
            repair=(result.messages, gate.errors),
            max_calls=MAX_MODEL_CALLS - calls_so_far - reserve,
        )
        used = result.model_calls
        plan = result.plan
        if not isinstance(plan, AnswerPlan):
            return plan, None, used
        gate = check_plan(plan, request, countries)
    if gate.errors:
        return (
            UnsupportedPlan(reason="The question could not be turned into a valid plan: " + "; ".join(gate.errors)),
            None,
            used,
        )
    return plan, gate, used


def _inherit_fields(request: QueryRequest, previous: QueryRequest) -> QueryRequest:
    """A refining Follow-up keeps the earlier request's structured fields (e.g. a Clarification answer)
    unless the new request sets them."""
    merged = {**previous.structured_fields(), **request.structured_fields()}
    return QueryRequest.model_validate({**merged, "query": request.query, "previous_run_id": request.previous_run_id})


def _show(request: QueryRequest, field_name: str) -> str:
    value = getattr(request, field_name)
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)
