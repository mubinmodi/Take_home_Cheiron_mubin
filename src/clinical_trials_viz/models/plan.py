"""The Query Plan: the model's typed interpretation of a Question.

The model fills exactly one of three shapes: an answer plan, a request to clarify,
or a refusal. It never writes numbers, NCT IDs or citations; code does.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from clinical_trials_viz.catalog import Dimension, OverallStatus, Phase, StudyType


class Operation(StrEnum):
    AGGREGATE = "aggregate"  # count trials, optionally grouped by one dimension
    COMPARE = "compare"  # aggregate across 2-5 comparison sides
    PER_TRIAL = "per_trial"  # one row per trial (list / table)
    BIN = "bin"  # histogram of a numeric field
    RELATE = "relate"  # network of related entities


class NetworkKind(StrEnum):
    SPONSOR_DRUG = "sponsor_drug"  # lead sponsors linked to the drugs their trials give
    DRUG_DRUG = "drug_drug"  # drugs given together in the same trial arm


class PerTrialView(StrEnum):
    TABLE = "table"  # "list the trials"
    TIMELINE = "timeline"  # "timeline", "when did they run", "durations"
    SCATTER = "scatter"  # "enrollment against duration", "plot size vs length"


class Filters(BaseModel):
    """Constraints that select the Cohort. Leave a field empty when the question does not mention it."""

    model_config = ConfigDict(extra="forbid")

    drugs: list[str] = Field(
        default_factory=list,
        description="Specific drug names as written by the user (brand or generic). Never a drug class.",
    )
    conditions: list[str] = Field(default_factory=list, description="Conditions or diseases, as written by the user.")
    phases: list[Phase] = Field(default_factory=list)
    statuses: list[OverallStatus] = Field(
        default_factory=list, description="e.g. RECRUITING for 'recruiting trials', COMPLETED for 'completed'."
    )
    study_types: list[StudyType] = Field(
        default_factory=list,
        description="Only when the user asks for it, e.g. 'observational studies'. Empty means all types.",
    )
    sponsor: str | None = Field(default=None, description="Sponsor organization as written by the user.")
    sponsor_role: Literal["lead", "any"] = Field(
        default="lead", description="'any' only when the user includes collaborators."
    )
    countries: list[str] = Field(default_factory=list, description="Country names in English.")
    start_year_from: int | None = Field(default=None, description="Earliest start year, e.g. 2020 for 'since 2020'.")
    start_year_to: int | None = Field(default=None, description="Latest start year.")
    nct_ids: list[str] = Field(default_factory=list, description="NCT IDs the user typed, e.g. NCT02578680.")


class ComparisonSide(BaseModel):
    """One side of a comparison. Set exactly one of drug, condition or sponsor."""

    model_config = ConfigDict(extra="forbid")

    drug: str | None = None
    condition: str | None = None
    sponsor: str | None = None


class AnswerPlan(BaseModel):
    """Use when the question can be answered with trial counts or trial lists."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["answer"] = "answer"
    relation: Literal["new", "refine"] = Field(
        default="new", description="'refine' only when a previous plan is given and the user adjusts it."
    )
    operation: Operation
    filters: Filters = Field(
        default_factory=Filters,
        description="Filters shared by the whole question (for compare: the parts common to all sides).",
    )
    group_by: Dimension | None = Field(
        default=None, description="aggregate/compare: what to count by. None for a single total ('how many…')."
    )
    compare_sides: list[ComparisonSide] = Field(
        default_factory=list, description="compare only: 2-5 sides, e.g. Drug A vs Drug B."
    )
    top_n: int | None = Field(
        default=None, ge=1, le=50, description="Only when the user asks for a number, e.g. 'top 5 countries'."
    )
    view: PerTrialView | None = Field(default=None, description="per_trial only: table (default), timeline or scatter.")
    network: NetworkKind | None = Field(default=None, description="relate only: which entities the network links.")


class ClarificationReason(StrEnum):
    MISSING_REFERENCE = "missing_reference"  # "this drug" with nothing to resolve it
    DRUG_CLASS = "drug_class"  # a class such as "PD-1 inhibitors" instead of named drugs
    CONFLICT = "conflict"  # a structured field and the question name different values
    MISSING_COMPARISON = "missing_comparison"  # "compare these" with sides not named


class ClarifyPlan(BaseModel):
    """Use only when no sensible default exists. Never for date meaning, sponsor role or brand names."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["clarify"] = "clarify"
    reason: ClarificationReason
    field: Literal["drug", "condition", "sponsor", "country", "compare_sides"]
    term: str | None = Field(default=None, description="The unclear term from the question, e.g. 'PD-1 inhibitors'.")
    mentioned_values: list[str] = Field(default_factory=list, description="Conflicting values the user wrote.")
    question: str = Field(description="Short question to show the user.")


class UnsupportedPlan(BaseModel):
    """Use when the question cannot be answered from ClinicalTrials.gov registry data (e.g. efficacy, medical advice)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["unsupported"] = "unsupported"
    reason: str = Field(description="One sentence explaining why, addressed to the user.")


QueryPlan = AnswerPlan | ClarifyPlan | UnsupportedPlan
