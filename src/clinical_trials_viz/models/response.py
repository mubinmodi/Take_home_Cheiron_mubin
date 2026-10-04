"""The response contract for `POST /v1/query`."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from clinical_trials_viz.catalog import OverallStatus, Phase, StudyType
from clinical_trials_viz.models.plan import AnswerPlan, ClarifyPlan, UnsupportedPlan
from clinical_trials_viz.models.spec import VisualizationSpec


class Outcome(StrEnum):
    SUCCESS = "success"
    NO_DATA = "no_data"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED_QUERY = "unsupported_query"
    SCOPE_REQUIRED = "scope_required"
    UPSTREAM_ERROR = "upstream_error"
    INTERNAL_ERROR = "internal_error"


class AppliedFilters(BaseModel):
    """The Filters actually applied, after merging the plan with structured request fields."""

    drugs: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
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
    value: str | list[str] = Field(description="Send back as-is in the Clarification's field.")
    trial_count: int | None = None


class Clarification(BaseModel):
    """A question for the user. Send the chosen value(s) back in `field` with `previous_run_id`."""

    field: str = Field(description="Request field to fill with the answer, e.g. 'sponsor' or 'drug_name'.")
    question: str
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


class QueryResponse(BaseModel):
    run_id: str
    outcome: Outcome
    message: str | None = Field(default=None, description="Human-readable explanation for non-success outcomes.")
    relation: Literal["new", "refine"] | None = None
    plan: AnswerPlan | ClarifyPlan | UnsupportedPlan | None = None
    applied_filters: AppliedFilters | None = None
    visualization: VisualizationSpec | None = None
    chart_url: str | None = None
    evidence: dict[str, EvidenceEntry] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    clarification: Clarification | None = None
    source: SourceInfo | None = None
    verification: Verification | None = None
    model_calls: int = 0
    timings_ms: dict[str, float] = Field(default_factory=dict)
