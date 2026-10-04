"""The response contract for `POST /v1/query`."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from clinical_trials_viz.catalog import OverallStatus, Phase, StudyType
from clinical_trials_viz.models.plan import AnswerPlan, ClarifyPlan, MultiAnswerPlan, UnsupportedPlan
from clinical_trials_viz.models.spec import VisualizationSpec


class Outcome(StrEnum):
    SUCCESS = "success"
    NO_DATA = "no_data"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED_QUERY = "unsupported_query"
    SCOPE_REQUIRED = "scope_required"
    UPSTREAM_ERROR = "upstream_error"
    INTERNAL_ERROR = "internal_error"


class ErrorCode(StrEnum):
    """Why an answer failed. Stable strings a client can branch on; `message` is for people."""

    PLANNER_NOT_CONFIGURED = "planner_not_configured"  # no model has credentials
    PLANNER_UNAVAILABLE = "planner_unavailable"  # every model failed with a provider or network error
    PLANNER_REJECTED = "planner_rejected"  # providers refused the request (bad key, unknown model): fix configuration
    PLANNER_TIMEOUT = "planner_timeout"  # planning took longer than the time limit
    PLANNER_INVALID_OUTPUT = "planner_invalid_output"  # the model did not return a usable plan
    SOURCE_UNAVAILABLE = "source_unavailable"  # ClinicalTrials.gov unreachable or failing (after retries)
    SOURCE_RATE_LIMITED = "source_rate_limited"  # ClinicalTrials.gov rate limit, still hit after backing off
    SOURCE_REJECTED = "source_rejected"  # ClinicalTrials.gov refused the request (4xx)
    SOURCE_INVALID_RESPONSE = "source_invalid_response"  # ClinicalTrials.gov answered with something unreadable
    SCOPE_TOO_LARGE = "scope_too_large"  # more trials match than one question may retrieve
    VERIFICATION_FAILED = "verification_failed"  # the answer failed the verifier and was withheld
    RUN_TIMEOUT = "run_timeout"  # the whole Run took longer than its deadline (hosted: ~30 s)
    INTERNAL = "internal"  # a bug: see the server log for the run ID


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = Field(description="True when sending the same request again later may succeed.")


class AppliedFilters(BaseModel):
    """The Filters actually applied, after merging the plan with structured request fields."""

    drugs: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list, description="Free-text terms searched anywhere in the record.")
    phases: list[Phase] = Field(default_factory=list)
    statuses: list[OverallStatus] = Field(default_factory=list)
    study_types: list[StudyType] = Field(default_factory=list)
    sponsor: str | None = None
    sponsor_role: Literal["lead", "any"] = "lead"
    exact_sponsors: list[str] = Field(
        default_factory=list, description="Lead sponsor names matched exactly (any of them), from a structured field."
    )
    sponsor_exact: bool = Field(
        default=False, description="Sponsor came from a structured field and is matched exactly."
    )
    countries: list[str] = Field(default_factory=list)
    start_year_from: int | None = None
    start_year_to: int | None = None
    nct_ids: list[str] = Field(default_factory=list)
    from_request: list[str] = Field(default_factory=list, description="Filter names set by structured request fields.")


class ClarificationOption(BaseModel):
    label: str
    value: str | int | list[str] = Field(description="Send back as-is in the Clarification's field.")
    trial_count: int | None = None


class Clarification(BaseModel):
    """A question for the user. Send the chosen value(s) back in `field` with `previous_run_id`."""

    field: str = Field(description="Request field to fill with the answer, e.g. 'sponsor' or 'drug_name'.")
    question: str
    reason: str | None = Field(
        default=None, description="'conflict' when a structured field contradicts the question; the answer is final."
    )
    options: list[ClarificationOption] = Field(default_factory=list)
    multi_select: bool = False
    allow_free_text: bool = False


class EvidenceEntry(BaseModel):
    """One cited Trial, with the source field values that placed it in its data."""

    nct_id: str
    title: str
    url: str
    fields: dict[str, Any] = Field(description="API field path -> value as captured from ClinicalTrials.gov.")


class SourceInfo(BaseModel):
    api_version: str | None = None
    data_timestamp: str | None = None
    retrieved_at: datetime
    search_matches: int = Field(default=0, description="Trials returned by the API search(es).")
    cohort_size: int = Field(default=0, description="Trials kept after match checks.")
    api_requests: int = 0


class VerificationCheck(BaseModel):
    name: str
    passed: bool
    detail: str | None = None


class Verification(BaseModel):
    passed: bool
    checks: list[VerificationCheck]


class Answer(BaseModel):
    """The result for one part of a Question. A single Question has one part: the response itself."""

    outcome: Outcome
    message: str | None = Field(default=None, description="Human-readable explanation for non-success outcomes.")
    plan: AnswerPlan | MultiAnswerPlan | ClarifyPlan | UnsupportedPlan | None = None
    applied_filters: AppliedFilters | None = None
    visualization: VisualizationSpec | None = None
    chart_url: str | None = None
    evidence: dict[str, EvidenceEntry] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    clarification: Clarification | None = None
    source: SourceInfo | None = None
    verification: Verification | None = None
    error: ErrorInfo | None = Field(default=None, description="Set when the outcome is a failure.")
    warnings: list[str] = Field(
        default_factory=list,
        description="Problems that did not change the answer, e.g. the chart image could not be prepared "
        "(the visualization specification and citations are still valid).",
    )


class QueryResponse(Answer):
    """The first (or only) part's answer at the top level; further parts in `additional_answers`."""

    run_id: str
    relation: Literal["new", "refine"] | None = None
    additional_answers: list[Answer] = Field(
        default_factory=list,
        description="When the Question asks several separate things: the answers to the second and third part.",
    )
    model_calls: int = 0
    planner_model: str | None = Field(default=None, description="The model that produced the plan.")
    timings_ms: dict[str, float] = Field(default_factory=dict)
