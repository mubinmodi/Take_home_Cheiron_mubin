"""The Filters of a Question: how they merge from the Query Plan and the structured fields, become API
parameters, are checked against a Trial, are cited and described, and can be dropped by a Correction.
Everything one Filter means lives here; callers ask `AppliedFilters` and never read its fields to decide.

Sponsor is the one Filter stored in several fields (a search term or exact lead sponsor names, and a
role). Only this module reads or writes them; the fields stay in the response for the API contract."""

from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, Field

from clinical_trials_viz.catalog import PHASE_LABELS, PINNED_FIELDS, Dimension, OverallStatus, Phase, StudyType
from clinical_trials_viz.ctgov.trial import Trial
from clinical_trials_viz.models.plan import AnswerPlan
from clinical_trials_viz.models.request import QueryRequest


@dataclass(frozen=True)
class FilterDef:
    """One Filter: the fields that hold it, the source field it is cited with, and whether (and in
    which order) a Correction may drop it."""

    name: str  # as the user reads it: "country", "start-year", ...
    fields: tuple[str, ...]
    cited: Dimension | None  # the Dimension whose source field shows a trial passes it
    correction_rank: int | None  # order in which Corrections are tried; None: never dropped


# In citation order (the order Evidence lists the fields).
FILTER_DEFS: tuple[FilterDef, ...] = (
    FilterDef("drug", ("drugs",), Dimension.DRUG, None),
    FilterDef("condition", ("conditions",), Dimension.CONDITION, None),
    FilterDef("keyword", ("keywords",), None, 6),
    FilterDef("phase", ("phases",), Dimension.PHASE, 2),
    FilterDef("status", ("statuses",), Dimension.STATUS, 3),
    FilterDef("study type", ("study_types",), Dimension.STUDY_TYPE, 5),
    FilterDef("sponsor", ("sponsor", "exact_sponsors", "sponsor_exact"), Dimension.LEAD_SPONSOR, 7),
    FilterDef("country", ("countries",), Dimension.COUNTRY, 1),
    FilterDef("start-year", ("start_year_from", "start_year_to"), Dimension.START_YEAR, 4),
    FilterDef("trial", ("nct_ids",), None, None),
)


@dataclass(frozen=True)
class Relaxation:
    """The Filters with one of them dropped, for a Correction."""

    name: str  # the dropped Filter
    without: "AppliedFilters"
    dropped: str  # the dropped Filter as the user reads it, e.g. "in Germany"
    pinned: bool  # set by a structured field, so a Follow-up cannot drop it


def _quote(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


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

    # --- from the Query Plan and the structured fields -------------------------------------------

    @classmethod
    def merge(cls, plan: AnswerPlan, request: QueryRequest) -> Self:
        """Structured request fields are authoritative; the plan fills everything else."""
        applied = cls(**plan.filters.model_dump())
        for request_field, (filter_field, _) in PINNED_FIELDS.items():
            value = getattr(request, request_field)
            if value and request_field != "sponsor":
                setattr(applied, filter_field, value)
                applied.from_request.append(filter_field)
        if request.sponsor:
            asked = (plan.filters.sponsor or "").strip().lower()
            if asked and [s.strip().lower() for s in request.sponsor] == [asked]:
                # The field repeats the question's own sponsor term (e.g. the "your question" answer to a
                # Conflict): search it as a lead sponsor name, so an ambiguous name is still asked about.
                applied.from_request.append("sponsor")
            else:  # structured sponsors are exact lead sponsor names (e.g. a Clarification answer)
                applied.exact_sponsors = list(request.sponsor)
                applied.sponsor, applied.sponsor_role, applied.sponsor_exact = (
                    " or ".join(request.sponsor),
                    "lead",
                    True,
                )
                applied.from_request.append("sponsor")
        return applied

    def for_side(self, drug: str | None = None, condition: str | None = None, sponsor: str | None = None) -> Self:
        """These Filters for one side of a comparison (a drug, a condition or a lead sponsor search)."""
        side = self.model_copy(deep=True)
        if drug:
            side.drugs = [drug]
        elif condition:
            side.conditions = [condition]
        else:
            side.sponsor, side.sponsor_role, side.sponsor_exact = sponsor, "lead", False
        return side

    # --- sponsor -------------------------------------------------------------------------------------

    def sponsor_is_ambiguous_term(self) -> bool:
        """A lead sponsor search term (not exact names) that may match several organizations ("Merck")."""
        return bool(self.sponsor) and not self.sponsor_exact and self.sponsor_role == "lead"

    def exact_sponsor_names(self) -> set[str]:
        """The exact lead sponsor names to keep (lower case); empty unless matched exactly."""
        if not self.sponsor_exact:
            return set()
        return {n.lower() for n in (self.exact_sponsors or [self.sponsor or ""])}

    # --- retrieval and checking ----------------------------------------------------------------------

    def api_params(self, drug: str | None = None) -> dict[str, str]:
        """ClinicalTrials.gov API v2 query parameters. Drugs are searched one at a time, so only one is passed."""
        params: dict[str, str] = {}
        advanced: list[str] = []
        if drug:
            params["query.intr"] = drug
        if self.conditions:
            params["query.cond"] = " OR ".join(f"({c})" for c in self.conditions)
        if self.keywords:  # every keyword must appear somewhere in the trial record
            params["query.term"] = " AND ".join(f"({k})" for k in self.keywords)
        if self.sponsor:
            if self.sponsor_role == "any":
                params["query.spons"] = self.sponsor
            elif self.sponsor_exact:
                names = self.exact_sponsors or [self.sponsor]
                advanced.append("AREA[LeadSponsorName](" + " OR ".join(_quote(n) for n in names) + ")")
            else:
                advanced.append(f"AREA[LeadSponsorName]({self.sponsor})")
        if self.phases:
            advanced.append("AREA[Phase](" + " OR ".join(self.phases) + ")")
        if self.study_types:
            advanced.append("AREA[StudyType](" + " OR ".join(self.study_types) + ")")
        if self.countries:
            advanced.append("AREA[LocationCountry](" + " OR ".join(_quote(c) for c in self.countries) + ")")
        if self.start_year_from or self.start_year_to:
            start = f"{self.start_year_from}-01-01" if self.start_year_from else "MIN"
            end = f"{self.start_year_to}-12-31" if self.start_year_to else "MAX"
            advanced.append(f"AREA[StartDate]RANGE[{start},{end}]")
        if self.statuses:
            params["filter.overallStatus"] = ",".join(self.statuses)
        if self.nct_ids:
            params["filter.ids"] = ",".join(self.nct_ids)
        if advanced:
            params["filter.advanced"] = " AND ".join(advanced)
        return params

    def violations(self, trial: Trial) -> list[str]:
        """The Filters a trial fails, checked on its own record. Drug and condition matching is
        checked during retrieval instead."""
        problems = []
        if self.phases and not set(trial.phases) & set(self.phases):
            problems.append(f"phases {list(trial.phases)} not in {list(self.phases)}")
        if self.statuses and trial.overall_status not in self.statuses:
            problems.append(f"status {trial.overall_status} not in {list(self.statuses)}")
        if self.study_types and trial.study_type not in self.study_types:
            problems.append(f"study type {trial.study_type} not in {list(self.study_types)}")
        if self.countries and not set(trial.countries) & set(self.countries):
            problems.append(f"no site in {self.countries}")
        if self.nct_ids and trial.nct_id not in self.nct_ids:
            problems.append("not one of the requested NCT IDs")
        if (names := self.exact_sponsor_names()) and (trial.lead_sponsor or "").lower() not in names:
            problems.append(f"lead sponsor {trial.lead_sponsor!r} is not {self.sponsor!r}")
        if self.start_year_from or self.start_year_to:
            year = trial.start_year
            low, high = self.start_year_from or 0, self.start_year_to or 9999
            if year is None or not low <= year <= high:
                problems.append(f"start year {year} outside {low}-{high}")
        return problems

    # --- citing, describing, correcting --------------------------------------------------------------

    def _active(self, definition: FilterDef) -> bool:
        return any(getattr(self, f) for f in definition.fields)

    def cited_dimensions(self) -> list[Dimension]:
        """The source fields that decide whether a trial passes each applied Filter (cited as Evidence)."""
        return [d.cited for d in FILTER_DEFS if d.cited is not None and self._active(d)]

    def cites_collaborators(self) -> bool:
        """A sponsor of any role is matched through collaborators too, so they are cited."""
        return bool(self.sponsor) and self.sponsor_role == "any"

    def describe(self) -> str:
        """The Filters as the user reads them, e.g. "Keytruda; Phase 3; recruiting; in Germany"."""
        parts: list[str] = []
        if self.drugs:
            parts.append(" or ".join(self.drugs))
        if self.conditions:
            parts.append(" or ".join(self.conditions))
        if self.keywords:
            parts.append(" and ".join(f"'{k}'" for k in self.keywords))
        if self.sponsor:
            parts.append(f"{'lead sponsor' if self.sponsor_role == 'lead' else 'sponsor'} {self.sponsor}")
        if self.phases:
            parts.append(", ".join(PHASE_LABELS[p] for p in self.phases))
        if self.statuses:
            parts.append(", ".join(s.replace("_", " ").lower() for s in self.statuses))
        if self.study_types:
            parts.append(", ".join(s.replace("_", " ").lower() for s in self.study_types))
        if self.countries:
            parts.append("in " + ", ".join(self.countries))
        if self.start_year_from and self.start_year_to:
            parts.append(f"started {self.start_year_from}–{self.start_year_to}")
        elif self.start_year_from:
            parts.append(f"started {self.start_year_from} or later")
        elif self.start_year_to:
            parts.append(f"started {self.start_year_to} or earlier")
        if self.nct_ids:
            parts.append(", ".join(self.nct_ids))
        return "; ".join(parts) if parts else "all trials"

    def relaxations(self) -> list[Relaxation]:
        """These Filters with each droppable Filter removed in turn, in the order Corrections are tried."""
        droppable = sorted((d for d in FILTER_DEFS if d.correction_rank is not None and self._active(d)),
                           key=lambda d: d.correction_rank or 0)  # fmt: skip
        defaults = {f: info.get_default(call_default_factory=True) for f, info in type(self).model_fields.items()}
        return [
            Relaxation(
                name=d.name,
                without=self.model_copy(update={f: defaults[f] for f in d.fields}),
                dropped=type(self)(**{f: getattr(self, f) for f in d.fields if f != "sponsor_exact"}).describe(),
                pinned=any(f in self.from_request for f in d.fields),
            )
            for d in droppable
        ]
