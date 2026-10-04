"""Turn any failure during a Run into an Outcome and a structured error a client can act on.

Messages are written for people and never include provider response bodies; details go to the log
together with the run ID, which the message quotes for unexpected errors.
"""

import logging
from dataclasses import dataclass

from pydantic_ai import FallbackExceptionGroup, ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded

from clinical_trials_viz.ctgov.client import ScopeTooLarge, UpstreamError
from clinical_trials_viz.models.response import ErrorCode, ErrorInfo, Outcome
from clinical_trials_viz.planner import PlannerNotConfigured, PlannerTimeout, describe_model_error, is_outage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Failure:
    outcome: Outcome
    error: ErrorInfo

    @property
    def message(self) -> str:
        return self.error.message


def _failure(outcome: Outcome, code: ErrorCode, message: str, retryable: bool) -> Failure:
    return Failure(outcome, ErrorInfo(code=code, message=message, retryable=retryable))


def _model_errors(exc: BaseException) -> list[ModelAPIError]:
    if isinstance(exc, FallbackExceptionGroup):
        return [e for e in exc.exceptions if isinstance(e, ModelAPIError)]
    return [exc] if isinstance(exc, ModelAPIError) else []


def classify(exc: BaseException, run_id: str) -> Failure:
    """The Outcome and error for an exception raised while answering (all or part of) a Run."""
    if isinstance(exc, ScopeTooLarge):
        return _failure(
            Outcome.SCOPE_REQUIRED,
            ErrorCode.SCOPE_TOO_LARGE,
            f"{exc.total:,} trials match, more than the {exc.limit:,} this service retrieves per question. "
            "Narrow it, e.g. by phase, status, start years, country or sponsor.",
            retryable=False,
        )
    if isinstance(exc, UpstreamError):
        log.warning("run %s: ClinicalTrials.gov failed: %s", run_id, exc)
        return _failure(Outcome.UPSTREAM_ERROR, exc.code, str(exc), exc.retryable)
    if errors := _model_errors(exc):
        tried = "; ".join(describe_model_error(e) for e in errors)
        log.warning("run %s: every planning model failed: %s", run_id, tried)
        if any(is_outage(e) for e in errors):
            return _failure(
                Outcome.UPSTREAM_ERROR,
                ErrorCode.PLANNER_UNAVAILABLE,
                f"The planning model is unavailable ({tried}). Please try again shortly.",
                retryable=True,
            )
        return _failure(
            Outcome.UPSTREAM_ERROR,
            ErrorCode.PLANNER_REJECTED,
            f"The planning model rejected the request ({tried}). Check the API keys and model names.",
            retryable=False,
        )
    if isinstance(exc, PlannerTimeout):
        log.warning("run %s: %s", run_id, exc)
        return _failure(Outcome.UPSTREAM_ERROR, ErrorCode.PLANNER_TIMEOUT, f"{exc}. Please try again.", True)
    if isinstance(exc, PlannerNotConfigured):
        return _failure(Outcome.INTERNAL_ERROR, ErrorCode.PLANNER_NOT_CONFIGURED, str(exc), retryable=False)
    if isinstance(exc, UnexpectedModelBehavior | UsageLimitExceeded):
        log.warning("run %s: unusable plan: %s", run_id, exc)
        return _failure(
            Outcome.INTERNAL_ERROR,
            ErrorCode.PLANNER_INVALID_OUTPUT,
            "The planning model did not return a usable plan. Please try again or rephrase the question.",
            retryable=True,
        )
    log.error("run %s failed", run_id, exc_info=exc)
    return _failure(
        Outcome.INTERNAL_ERROR,
        ErrorCode.INTERNAL,
        f"Unexpected error (reference {run_id}); see the server log.",
        retryable=False,
    )
