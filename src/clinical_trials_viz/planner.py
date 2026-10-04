"""The planner: the service's only model step. Question in, typed Query Plan out.

The model sees the Question, the capability catalog and (for Follow-ups) the previous plan.
It never sees trial records, so trial text cannot inject instructions.
"""

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    ModelAPIError,
    ModelHTTPError,
    ModelSettings,
    ToolOutput,
    UsageLimitExceeded,
    UsageLimits,
    UserError,
)
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, infer_model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers import Provider, infer_provider
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from clinical_trials_viz.breaker import CircuitBreaker, CircuitOpen
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
  This is the normal case, even for long questions.
- `clarify_plan` only when no sensible default exists (see below).
- `unsupported_plan` when the registry cannot answer it (give up to 3 related questions it can answer
  in `suggestions`, e.g. "Which phases are Keytruda trials in?"): efficacy or safety conclusions,
  treatment advice, prices, or anything not about registered trials.

Operations (answer_plan.operation):
- `aggregate`: count trials. `group_by` null for a single total ("how many…"); otherwise the
  dimension to count by ("per year" -> start_year, "by phase" -> phase, "which countries" -> country).
- `compare`: the same count for 2-5 sides ("Drug A vs Drug B", "condition X vs Y"). Put each side
  in `compare_sides` (one of drug, condition, sponsor per side); shared filters go in `filters`.
- `per_trial`: individual trials. `view`: `table` for "list", "which trials", "show the studies";
  `timeline` for "timeline", "when did they run", "how long do they last", "Gantt";
  `scatter` for "enrollment against duration", "plot trial size vs length", "scatter".
- `relate`: a network of entities that share trials. Set `network`:
  `sponsor_drug` for "network of sponsors and drugs", "which companies develop which drugs";
  `drug_drug` for "which drugs are combined / co-occur / given together". group_by stays null.
- `bin`: an enrollment histogram ("distribution of trial sizes", "how many participants do trials
  enroll"). group_by stays null.

Dimensions for group_by (and series_by):
{dimensions}

One chart with two dimensions: set `series_by` only for "X per Y" or "X by Y and Z together"
("phases per year" -> group_by start_year, series_by phase; "status by country" -> group_by country,
series_by status). Leave series_by null in every other case.

If a message contains several separate requests, plan only the first one and never mix filters from
different requests. (Code splits multi-part messages before they reach you, so this is rare.)

Filters: fill only what the question (or the structured fields) states.
- Phases: {phases}. "Phase 2/3" means both PHASE2 and PHASE3.
- Statuses: {statuses}. "recruiting" -> RECRUITING; "active" -> RECRUITING and ACTIVE_NOT_RECRUITING.
- Years: "since 2020" -> start_year_from 2020; "in 2021" -> from 2021 to 2021.
- Drugs: copy the user's drug names as written (brand, generic or code names all work).
  A drug class ("PD-1 inhibitors", "checkpoint inhibitors", "chemotherapy") is not a drug name.
- Sponsors: "industry", "academic", "NIH", "government" are sponsor categories (the `sponsor_class`
  dimension), never sponsor names: "industry vs academic ... compare X and Y" -> compare the
  conditions X and Y with group_by sponsor_class.
- Keywords: topic words that are neither a drug nor a condition ("vaccine", "gene therapy", "CAR-T")
  go in `keywords`, never in conditions or drugs: "COVID-19 vaccine trials" -> condition COVID-19,
  keyword vaccine.

Defaults: never ask about these; just use them:
- "year" means start year; "sponsor" means lead sponsor; brand names resolve automatically;
  "trials" includes all study types unless the user restricts them.

Clarify only for:
- `missing_reference`: "this drug" / "that condition" with no structured field to resolve it.
- `drug_class`: the user names a drug class instead of drugs. Set `term` to the class name.
- `missing_comparison`: "compare these" without naming the sides.

Structured fields resolve references like "this drug". When the question itself names a value,
put the question's value in the filter even if a structured field differs; never ask about that,
code compares them. Follow-ups: when a previous plan is shown, decide whether the new
message refines it (relation "refine": keep its filters and change only what the user asks) or
starts a new question (relation "new")."""


@dataclass
class PlannerResult:
    plan: QueryPlan
    model_calls: int
    model_name: str | None  # the model that produced the plan (the fallback, if the primary failed)
    messages: list[ModelMessage] = field(default_factory=list)
    model_failures: list[str] = field(default_factory=list)  # e.g. ["gpt-5.4-mini: HTTP 503"], before a fallback


@dataclass
class SplitResult:
    requests: list[str]  # the separate questions in a message, each standalone (one item: a single question)
    model_calls: int
    model_name: str | None
    model_failures: list[str] = field(default_factory=list)


class MessageParts(BaseModel):
    """The separate questions a message asks."""

    requests: list[str] = Field(
        min_length=1,
        max_length=8,
        description="Each separate question, rewritten to stand alone with no 'their', 'them' or 'it'; one item if one.",
    )


SPLIT_INSTRUCTIONS = """You read a message sent to a clinical-trials analytics service and list the separate
questions it asks. You never answer them.
- One request is one answer or chart. "Phases of melanoma trials per year" and "trials by phase and
  status" are each ONE request.
- A comparison is always ONE request: never split its sides. "Compare phases for semaglutide vs
  tirzepatide trials", "A versus B", "compare A and B" and "how does A compare with B" stay whole.
- A message asks several things when it joins requests that each need their own answer: "and", "also",
  "plus", ";", a new sentence or a second "?", or numbering ("1) ... 2) ..."). "How many X, and which
  countries have the most?" is TWO requests. "What phases, what countries and what intervention types
  are lung cancer trials?" is THREE.
- Each item is read alone, without the others, so it must make sense on its own. Replace every word
  that points to another request ("their", "them", "those", "these", "it", "the same", "ones") with
  what it refers to: the drug, condition, status, phase, country and years.
  "List recruiting Keytruda trials in Germany and show their phases" becomes
  ["List recruiting Keytruda trials in Germany", "Show the phases of recruiting Keytruda trials in Germany"].
  "Which sponsors run the most Alzheimer's trials? Which countries host them?" becomes
  ["Which sponsors run the most Alzheimer's trials?", "Which countries host Alzheimer's trials?"].
- Otherwise keep the user's words. Never add a question the user did not ask; never drop one.
- A message that asks one thing: return it unchanged as the only item."""

SPLIT_MAX_CALLS = 2  # the split call and, if the primary model fails, the fallback's attempt


class Planner(Protocol):
    async def split(self, message: str) -> SplitResult: ...

    async def plan(
        self,
        question: str,
        structured: dict[str, Any],
        previous_plan: QueryPlan | None,
        *,
        repair: tuple[list[ModelMessage], list[str]] | None = None,
        max_calls: int = 3,
        context: str | None = None,
    ) -> PlannerResult: ...


def build_prompt(
    question: str, structured: dict[str, Any], previous_plan: QueryPlan | None, context: str | None = None
) -> str:
    parts = [f"Question: {question}"]
    if context:
        parts.append(
            "This question is one part of a longer message. Plan only this part; use the full message only to "
            f"resolve references (e.g. 'these trials'). Full message: {context}"
        )
    if structured:
        parts.append("Structured fields: " + json.dumps(structured))
    if previous_plan is not None:
        parts.append("Previous plan: " + previous_plan.model_dump_json(exclude_defaults=True))
    return "\n".join(parts)


log = logging.getLogger(__name__)

SDK_MAX_RETRIES = 1  # one quick transport retry per model; the fallback model is the real second chance


class PlannerNotConfigured(Exception):
    """No planner model could be created (usually a missing API key)."""


class PlannerTimeout(Exception):
    """Planning took longer than the deadline: the providers are slow or hanging."""


class UnconfiguredPlanner:
    def __init__(self, reason: str):
        self.reason = reason

    async def split(self, message: str) -> SplitResult:
        raise PlannerNotConfigured(self.reason)

    async def plan(self, *args: Any, **kwargs: Any) -> PlannerResult:
        raise PlannerNotConfigured(self.reason)


# Provider statuses that may pass with time; other 4xx (bad key, unknown model) need a configuration fix.
_OUTAGE_STATUS = frozenset({408, 409, 429})


def is_outage(exc: ModelAPIError) -> bool:
    """A provider failure that may pass (server error, rate limit, timeout, network), as opposed to a
    configuration error such as a bad key or an unknown model."""
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in _OUTAGE_STATUS or exc.status_code >= 500
    return True


def describe_model_error(exc: ModelAPIError) -> str:
    """A short, safe description of a provider failure: never the response body."""
    if isinstance(exc, ModelHTTPError):
        return f"{exc.model_name}: HTTP {exc.status_code}"
    return f"{exc.model_name}: {exc.message[:120]}"


# Provider failures seen during the current plan() call (each call runs in its own task context).
_model_failures: ContextVar[list[str] | None] = ContextVar("planner_model_failures", default=None)


class CallBudgetExhausted(UsageLimitExceeded):
    """A model attempt refused because this plan() call's budget is spent. Not a provider error, so
    the FallbackModel does not move on to the next model."""


@dataclass
class _CallBudget:
    """Provider attempts in one plan() call. pydantic-ai counts a request answered by the fallback
    once; this counts every attempt, the failed ones included (harness-design: every attempt counts)."""

    limit: int
    used: int = 0


_call_budget: ContextVar[_CallBudget | None] = ContextVar("planner_call_budget", default=None)


@dataclass
class _RunCalls:
    """Model calls across the plan() calls of one Run."""

    failed: dict[str, str] = field(default_factory=dict)  # model -> its outage; later calls in the Run skip it
    attempts: int = 0  # every provider attempt in the Run
    reported: int = 0  # attempts already counted in a returned PlannerResult


_run_calls: ContextVar[_RunCalls | None] = ContextVar("planner_run_calls", default=None)


@contextmanager
def run_scope() -> Iterator[None]:
    """Share provider failures and the call count between the plan() calls of one Run."""
    token = _run_calls.set(_RunCalls())
    try:
        yield
    finally:
        _run_calls.reset(token)


def take_unreported_calls() -> int:
    """Attempts made in this Run that no PlannerResult reported, because plan() raised. Each is counted once."""
    calls = _run_calls.get()
    if calls is None:
        return 0
    unreported, calls.reported = calls.attempts - calls.reported, calls.attempts
    return unreported


class _ReportingModel(WrapperModel):
    """One planner model behind its circuit breaker. Notes a provider failure (log and response
    warning), then re-raises it so the FallbackModel moves on to the next model; while the breaker is
    open the model is skipped at once instead of waiting for its timeout."""

    def __init__(self, wrapped: Model, breaker: CircuitBreaker):
        super().__init__(wrapped)
        self._breaker = breaker

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        if (budget := _call_budget.get()) is not None and budget.used >= budget.limit:
            raise CallBudgetExhausted(f"The {budget.limit} model call(s) allowed for this question are used up.")
        run_calls = _run_calls.get()
        if run_calls is not None and self.model_name in run_calls.failed:
            earlier = run_calls.failed[self.model_name]
            raise ModelAPIError(self.model_name, f"{earlier} earlier in this run; not called again")
        try:
            self._breaker.check()
        except CircuitOpen as exc:
            skipped = ModelAPIError(
                self.model_name, f"not called, paused for {exc.retry_in:.0f} s after repeated failures"
            )
            self._note(skipped)
            raise skipped from exc
        if budget is not None:
            budget.used += 1
        if run_calls is not None:
            run_calls.attempts += 1
        try:
            response = await super().request(messages, model_settings, model_request_parameters)
        except ModelAPIError as exc:
            if is_outage(exc):
                self._breaker.failure()
                if run_calls is not None:
                    run_calls.failed[self.model_name] = describe_model_error(exc).removeprefix(f"{self.model_name}: ")
            else:
                self._breaker.success()  # it answered; the configuration is at fault
            self._note(exc)
            raise
        except BaseException:
            self._breaker.release()
            raise
        self._breaker.success()
        return response

    @staticmethod
    def _note(exc: ModelAPIError) -> None:
        note = describe_model_error(exc)
        log.warning("planner model failed: %s", note)
        if (failures := _model_failures.get()) is not None:
            failures.append(note)


def _api_key(variable: str) -> str:
    if key := os.environ.get(variable):
        return key
    raise UserError(f"Set the `{variable}` environment variable to use this provider.")


def _provider(name: str, timeout: float) -> Provider[Any]:
    """Provider clients with a request timeout and one quick retry."""
    if name == "openai":
        client = AsyncOpenAI(api_key=_api_key("OPENAI_API_KEY"), timeout=timeout, max_retries=SDK_MAX_RETRIES)
        return OpenAIProvider(openai_client=client)
    if name == "anthropic":
        client = AsyncAnthropic(api_key=_api_key("ANTHROPIC_API_KEY"), timeout=timeout, max_retries=SDK_MAX_RETRIES)
        return AnthropicProvider(anthropic_client=client)
    return infer_provider(name)  # e.g. Gemini: its request timeout comes from the model settings


def build_planner(
    primary: str,
    fallback: str | None,
    *,
    timeout: float = 20.0,
    deadline: float | None = 60.0,
    breaker_failures: int = 5,
    breaker_cooldown: float = 30.0,
) -> Planner:
    """Create the configured models that have credentials; the service still starts without any."""
    models: list[Model] = []
    problems: list[str] = []
    for name in (primary, fallback):
        if not name:
            continue
        try:
            models.append(infer_model(name, provider_factory=lambda provider: _provider(provider, timeout)))
        except UserError as exc:
            problems.append(f"{name}: {exc}")
            log.warning("planner model %s unavailable: %s", name, exc)
    if not models:
        return UnconfiguredPlanner("No planner model is configured. " + " ".join(problems))
    return LLMPlanner(
        *models,
        timeout=timeout,
        deadline=deadline,
        breaker_failures=breaker_failures,
        breaker_cooldown=breaker_cooldown,
    )


class LLMPlanner:
    """pydantic-ai planner. Models come from configuration; fallback only on provider errors.

    `timeout` bounds each model request; `deadline` bounds the whole call, fallback included.
    """

    def __init__(
        self,
        primary: str | Model,
        fallback: str | Model | None = None,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        breaker_failures: int = 5,
        breaker_cooldown: float = 30.0,
    ):
        models = []
        for m in (primary, fallback):
            if m is not None:
                model = m if isinstance(m, Model) else infer_model(m)
                breaker = CircuitBreaker(f"planner model {model.model_name}", breaker_failures, breaker_cooldown)
                models.append(_ReportingModel(model, breaker))
        model: Model = FallbackModel(*models, fallback_on=(ModelAPIError,)) if len(models) > 1 else models[0]
        self._deadline = deadline
        self._agent = Agent(
            model,
            output_type=[
                ToolOutput(AnswerPlan, name="answer_plan"),
                ToolOutput(ClarifyPlan, name="clarify_plan"),
                ToolOutput(UnsupportedPlan, name="unsupported_plan"),
            ],
            instructions=_instructions(),
            model_settings=ModelSettings(timeout=timeout) if timeout else None,
            retries={"output": 1},
            name="planner",
        )
        self._splitter = Agent(
            model,
            output_type=ToolOutput(MessageParts, name="separate_requests"),
            instructions=SPLIT_INSTRUCTIONS,
            model_settings=ModelSettings(timeout=timeout) if timeout else None,
            name="splitter",
        )

    async def split(self, message: str) -> SplitResult:
        """The separate questions in a message, each rewritten to stand alone. One model call (two if
        the primary fails); its calls are counted like planning calls, outside planning's budget."""
        failures: list[str] = []
        budget = _CallBudget(SPLIT_MAX_CALLS)
        token, budget_token = _model_failures.set(failures), _call_budget.set(budget)
        try:
            async with asyncio.timeout(self._deadline):
                result = await self._splitter.run(
                    f"Message: {message}", usage_limits=UsageLimits(request_limit=SPLIT_MAX_CALLS)
                )
        except TimeoutError as exc:
            raise PlannerTimeout(f"Splitting the message took longer than {self._deadline:g} s") from exc
        finally:
            _model_failures.reset(token)
            _call_budget.reset(budget_token)
        if (run_calls := _run_calls.get()) is not None:
            run_calls.reported += budget.used
        requests = [r.strip() for r in result.output.requests if r.strip()] or [message]
        model_name = result.response.model_name if result.response else None
        return SplitResult(requests, budget.used, model_name, failures)

    async def plan(
        self,
        question: str,
        structured: dict[str, Any],
        previous_plan: QueryPlan | None,
        *,
        repair: tuple[list[ModelMessage], list[str]] | None = None,
        max_calls: int = 3,
        context: str | None = None,
    ) -> PlannerResult:
        limits = UsageLimits(request_limit=max_calls)
        if repair:
            history, errors = repair
            prompt = "Your plan failed validation. Return a corrected plan.\nProblems:\n- " + "\n- ".join(errors)
        else:
            history, prompt = None, build_prompt(question, structured, previous_plan, context)
        failures: list[str] = []
        budget = _CallBudget(max_calls)
        token, budget_token = _model_failures.set(failures), _call_budget.set(budget)
        try:
            async with asyncio.timeout(self._deadline):  # None: no deadline
                result = await self._agent.run(prompt, message_history=history, usage_limits=limits)
        except TimeoutError as exc:
            raise PlannerTimeout(f"Planning took longer than {self._deadline:g} s") from exc
        finally:
            _model_failures.reset(token)
            _call_budget.reset(budget_token)
        response_model = result.response.model_name if result.response else None
        if (run_calls := _run_calls.get()) is not None:
            run_calls.reported += budget.used
        return PlannerResult(result.output, budget.used, response_model, result.all_messages(), failures)
