"""The request contract: a Question plus optional structured Filters."""

import re
from datetime import date
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from clinical_trials_viz.catalog import OverallStatus, Phase

NCT_ID_PATTERN = re.compile(r"^NCT\d{8}$")

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


def _as_list(value: object) -> object:
    """Accept one value or a list for multi-value fields."""
    if value is None or isinstance(value, list):
        return value
    return [value]


def _as_upper_list(value: object) -> object:
    """Enum fields: accept one value or a list, in any letter case."""
    value = _as_list(value)
    if isinstance(value, list):
        return [v.upper() if isinstance(v, str) else v for v in value]
    return value


class QueryRequest(BaseModel):
    """`POST /v1/query` body. Only `query` is required; every other field pins a Filter."""

    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] = Field(
        description="Natural-language question about clinical trials."
    )
    drug_name: Annotated[Annotated[list[Name], Field(max_length=5)] | None, BeforeValidator(_as_list)] = Field(
        default=None, description="Drug name(s); brand and code names are accepted (e.g. Keytruda, MK-3475)."
    )
    condition: Annotated[Annotated[list[Name], Field(max_length=5)] | None, BeforeValidator(_as_list)] = Field(
        default=None, description="Condition or disease name(s)."
    )
    trial_phase: Annotated[list[Phase] | None, BeforeValidator(_as_upper_list)] = Field(
        default=None, description="Phase(s), e.g. PHASE3."
    )
    sponsor: Name | None = Field(
        default=None, description="Lead sponsor name, matched exactly (e.g. 'Merck Sharp & Dohme LLC')."
    )
    country: Annotated[Annotated[list[Name], Field(max_length=10)] | None, BeforeValidator(_as_list)] = Field(
        default=None, description="Country name(s) with a trial site."
    )
    status: Annotated[list[OverallStatus] | None, BeforeValidator(_as_upper_list)] = Field(
        default=None, description="Overall status(es), e.g. RECRUITING."
    )
    start_year: int | None = Field(default=None, ge=1990, description="Earliest trial start year.")
    end_year: int | None = Field(default=None, ge=1990, description="Latest trial start year.")
    nct_id: Annotated[Annotated[list[str], Field(max_length=20)] | None, BeforeValidator(_as_list)] = Field(
        default=None, description="Specific trial(s) by NCT ID, e.g. NCT02578680."
    )
    previous_run_id: str | None = Field(
        default=None, description="Run this request follows up on, refines or answers a Clarification for."
    )

    @model_validator(mode="after")
    def _check(self) -> "QueryRequest":
        latest = date.today().year + 5
        for name in ("start_year", "end_year"):
            year = getattr(self, name)
            if year is not None and year > latest:
                raise ValueError(f"{name} must be at most {latest}")
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must not be after end_year")
        for nct in self.nct_id or []:
            if not NCT_ID_PATTERN.match(nct):
                raise ValueError(f"invalid NCT ID {nct!r}; expected NCT followed by 8 digits")
        return self

    def structured_fields(self) -> dict[str, object]:
        """The structured fields the caller actually set (excluding the question and run link)."""
        return self.model_dump(exclude={"query", "previous_run_id"}, exclude_none=True, mode="json")
