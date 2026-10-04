"""The planner: the service's only model step. Question in, typed Query Plan out.

The model sees the Question, the capability catalog and (for Follow-ups) the previous plan.
It never sees trial records, so trial text cannot inject instructions.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic_ai import Agent, ModelAPIError, ToolOutput, UsageLimits, UserError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.fallback import FallbackModel

from clinical_trials_viz.catalog import CATALOG_VERSION, DIMENSIONS, PHASE_LABELS, OverallStatus
from clinical_trials_viz.models.plan import AnswerPlan, ClarifyPlan, QueryPlan, UnsupportedPlan


def _instructions() -> str:
    dimensions = "\n".join(f"- `{d.value}`: {info.description}" for d, info in DIMENSIONS.items())
    phases = ", ".join(f"{k} ({v})" for k, v in PHASE_LABELS.items())
    statuses = ", ".join(s.value for s in OverallStatus)
    return f"""\
You turn a question about clinical trials into a typed plan. Code then retrieves the trials from
ClinicalTrials.gov, counts them and draws the chart. You never produce numbers, trial IDs or
citations, and you never answer from your own knowledge. Catalog version {CATALOG_VERSION}.

Call exactly one output tool:
- `answer_plan` when the question can be answered with counts or lists of registered trials.
- `clarify_plan` only when no sensible default exists (see below).
- `unsupported_plan` when the registry cannot answer it: efficacy or safety conclusions,
  treatment advice, prices, or anything not about registered trials.

Operations (answer_plan.operation):
- `aggregate`: count trials. `group_by` null for a single total ("how many…"); otherwise the
  dimension to count by ("per year" -> start_year, "by phase" -> phase, "which countries" -> country).
- `compare`: the same count for 2-5 sides ("Drug A vs Drug B", "condition X vs Y"). Put each side
  in `compare_sides` (one of drug, condition, sponsor per side); shared filters go in `filters`.
- `per_trial`: individual trials. `view`: `table` for "list", "which trials", "show the studies";
  `timeline` for "timeline", "when did they run", "how long do they last", "Gantt".
- `relate`: a network of entities that share trials. Set `network`:
  `sponsor_drug` for "network of sponsors and drugs", "which companies develop which drugs";
  `drug_drug` for "which drugs are combined / co-occur / given together". group_by stays null.
- `bin`: an enrollment histogram ("distribution of trial sizes", "how many participants do trials
  enroll"). group_by stays null.

Dimensions for group_by:
{dimensions}

Filters: fill only what the question (or the structured fields) states.
- Phases: {phases}. "Phase 2/3" means both PHASE2 and PHASE3.
- Statuses: {statuses}. "recruiting" -> RECRUITING; "active" -> RECRUITING and ACTIVE_NOT_RECRUITING.
- Years: "since 2020" -> start_year_from 2020; "in 2021" -> from 2021 to 2021.
- Drugs: copy the user's drug names as written (brand, generic or code names all work).
  A drug class ("PD-1 inhibitors", "checkpoint inhibitors", "chemotherapy") is not a drug name.

Defaults: never ask about these; just use them:
- "year" means start year; "sponsor" means lead sponsor; brand names resolve automatically;
  "trials" includes all study types unless the user restricts them.

Clarify only for:
- `missing_reference`: "this drug" / "that condition" with no structured field to resolve it.
- `drug_class`: the user names a drug class instead of drugs. Set `term` to the class name.
- `conflict`: a structured field and the question name different values for the same filter.
  Put both values in `mentioned_values`.
- `missing_comparison`: "compare these" without naming the sides.

Structured fields, when present, are authoritative values for the matching filters and resolve
references like "this drug". Follow-ups: when a previous plan is shown, decide whether the new
message refines it (relation "refine": keep its filters and change only what the user asks) or
starts a new question (relation "new")."""


@dataclass
class PlannerResult:
    plan: QueryPlan
    model_calls: int
    model_name: str | None
    messages: list[ModelMessage] = field(default_factory=list)


class Planner(Protocol):
    async def plan(
        self,
        question: str,
        structured: dict[str, Any],
        previous_plan: QueryPlan | None,
        *,
        repair: tuple[list[ModelMessage], list[str]] | None = None,
        max_calls: int = 3,
    ) -> PlannerResult: ...


def build_prompt(question: str, structured: dict[str, Any], previous_plan: QueryPlan | None) -> str:
    parts = [f"Question: {question}"]
    if structured:
        parts.append("Structured fields: " + json.dumps(structured))
    if previous_plan is not None:
        parts.append("Previous plan: " + previous_plan.model_dump_json(exclude_defaults=True))
    return "\n".join(parts)


log = logging.getLogger(__name__)


class PlannerNotConfigured(Exception):
    """No planner model could be created (usually a missing API key)."""


class UnconfiguredPlanner:
    def __init__(self, reason: str):
        self.reason = reason

    async def plan(self, *args: Any, **kwargs: Any) -> PlannerResult:
        raise PlannerNotConfigured(self.reason)


def build_planner(primary: str, fallback: str | None) -> Planner:
    """Create the configured models that have credentials; the service still starts without any."""
    models: list[Model] = []
    problems: list[str] = []
    for name in (primary, fallback):
        if not name:
            continue
        try:
            models.append(infer_model(name))
        except UserError as exc:
            problems.append(f"{name}: {exc}")
            log.warning("planner model %s unavailable: %s", name, exc)
    if not models:
        return UnconfiguredPlanner("No planner model is configured. " + " ".join(problems))
    return LLMPlanner(*models)


class LLMPlanner:
    """pydantic-ai planner. Models come from configuration; fallback only on provider errors."""

    def __init__(self, primary: str | Model, fallback: str | Model | None = None):
        model: str | Model = FallbackModel(primary, fallback, fallback_on=(ModelAPIError,)) if fallback else primary
        self._agent = Agent(
            model,
            output_type=[
                ToolOutput(AnswerPlan, name="answer_plan"),
                ToolOutput(ClarifyPlan, name="clarify_plan"),
                ToolOutput(UnsupportedPlan, name="unsupported_plan"),
            ],
            instructions=_instructions(),
            retries={"output": 1},
            name="planner",
        )

    async def plan(
        self,
        question: str,
        structured: dict[str, Any],
        previous_plan: QueryPlan | None,
        *,
        repair: tuple[list[ModelMessage], list[str]] | None = None,
        max_calls: int = 3,
    ) -> PlannerResult:
        limits = UsageLimits(request_limit=max_calls)
        if repair:
            history, errors = repair
            prompt = "Your plan failed validation. Return a corrected plan.\nProblems:\n- " + "\n- ".join(errors)
            result = await self._agent.run(prompt, message_history=history, usage_limits=limits)
        else:
            result = await self._agent.run(build_prompt(question, structured, previous_plan), usage_limits=limits)
        response_model = result.response.model_name if result.response else None
        return PlannerResult(result.output, result.usage.requests, response_model, result.all_messages())
