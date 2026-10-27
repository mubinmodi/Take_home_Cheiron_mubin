"""Capability catalog: the single versioned statement of what the service supports.

It feeds the planner prompt, the plan validator, the analysis code and the tests.
Rules here are decided in docs/harness-design.md sections 2-3.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clinical_trials_viz.ctgov.trial import Trial

# Bumped when the catalog changes: 2026-10-04.1 added the conflict policy (PINNED_FIELDS);
# 2026-10-26.1 marked the time axis (DimensionInfo.temporal); 2026-10-26.2 sponsor ambiguity at 5%;
# 2026-10-27.1 each Dimension carries its own readers (DimensionInfo.values, .evidence); 2026-10-27.2 age group;
# 2026-10-27.3 operations, views, networks and clarification reasons, with the prompt's guidance for each.
CATALOG_VERSION = "2026-10-27.3"


class Phase(StrEnum):
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    NA = "NA"


PHASE_LABELS: dict[str, str] = {
    "EARLY_PHASE1": "Early Phase 1",
    "PHASE1": "Phase 1",
    "PHASE2": "Phase 2",
    "PHASE3": "Phase 3",
    "PHASE4": "Phase 4",
    "NA": "Not applicable",
}


class OverallStatus(StrEnum):
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    COMPLETED = "COMPLETED"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"
    AVAILABLE = "AVAILABLE"
    NO_LONGER_AVAILABLE = "NO_LONGER_AVAILABLE"
    TEMPORARILY_NOT_AVAILABLE = "TEMPORARILY_NOT_AVAILABLE"
    APPROVED_FOR_MARKETING = "APPROVED_FOR_MARKETING"
    WITHHELD = "WITHHELD"
    UNKNOWN = "UNKNOWN"


class StudyType(StrEnum):
    INTERVENTIONAL = "INTERVENTIONAL"
    OBSERVATIONAL = "OBSERVATIONAL"
    EXPANDED_ACCESS = "EXPANDED_ACCESS"


class Operation(StrEnum):
    AGGREGATE = "aggregate"  # count trials, optionally grouped by one dimension
    COMPARE = "compare"  # aggregate across 2-5 comparison sides
    PER_TRIAL = "per_trial"  # one row per trial (list / table)
    BIN = "bin"  # histogram of a numeric field
    RELATE = "relate"  # network of related entities


class PerTrialView(StrEnum):
    TABLE = "table"  # "list the trials"
    TIMELINE = "timeline"  # "timeline", "when did they run", "durations"
    SCATTER = "scatter"  # "enrollment against duration", "plot size vs length"


class NetworkKind(StrEnum):
    SPONSOR_DRUG = "sponsor_drug"  # lead sponsors linked to the drugs their trials give
    DRUG_DRUG = "drug_drug"  # drugs given together in the same trial arm


class ClarificationReason(StrEnum):
    """What the model may ask about. Sponsor ambiguity and field conflicts are found by code (clarify.py, conflicts.py)."""

    MISSING_REFERENCE = "missing_reference"  # "this drug" with nothing to resolve it
    DRUG_CLASS = "drug_class"  # a class such as "PD-1 inhibitors" instead of named drugs
    MISSING_COMPARISON = "missing_comparison"  # "compare these" with sides not named


# When the planner should choose each operation, view, network and clarification. The planner prompt is
# generated from these, so a capability is described once.
VIEW_GUIDE: dict[PerTrialView, str] = {
    PerTrialView.TABLE: '"list", "which trials", "show the studies"',
    PerTrialView.TIMELINE: '"timeline", "when did they run", "how long do they last", "Gantt"',
    PerTrialView.SCATTER: '"enrollment against duration", "plot trial size vs length", "scatter"',
}

NETWORK_GUIDE: dict[NetworkKind, str] = {
    NetworkKind.SPONSOR_DRUG: '"network of sponsors and drugs", "which companies develop which drugs"',
    NetworkKind.DRUG_DRUG: '"which drugs are combined / co-occur / given together"',
}

OPERATION_GUIDE: dict[Operation, str] = {
    Operation.AGGREGATE: 'count trials. `group_by` null for a single total ("how many…"); otherwise the\n'
    '  dimension to count by ("per year" -> start_year, "by phase" -> phase, "which countries" -> country).',
    Operation.COMPARE: 'the same count for 2-5 sides ("Drug A vs Drug B", "condition X vs Y"). Put each side\n'
    "  in `compare_sides` (one of drug, condition, sponsor per side); shared filters go in `filters`.",
    Operation.PER_TRIAL: "individual trials. `view`: "
    + ";\n  ".join(f"`{view.value}` for {guide}" for view, guide in VIEW_GUIDE.items())
    + ".",
    Operation.RELATE: "a network of entities that share trials. Set `network`:\n  "
    + ";\n  ".join(f"`{kind.value}` for {guide}" for kind, guide in NETWORK_GUIDE.items())
    + ". group_by stays null.",
    Operation.BIN: 'an enrollment histogram ("distribution of trial sizes", "how many participants do trials\n'
    '  enroll"). group_by stays null.',
}

CLARIFICATION_GUIDE: dict[ClarificationReason, str] = {
    ClarificationReason.MISSING_REFERENCE: '"this drug" / "that condition" with no structured field to resolve it.',
    ClarificationReason.DRUG_CLASS: "the user names a drug class instead of drugs. Set `term` to the class name.",
    ClarificationReason.MISSING_COMPARISON: '"compare these" without naming the sides.',
}


class Dimension(StrEnum):
    """What a Cohort can be grouped by."""

    START_YEAR = "start_year"
    PHASE = "phase"
    STATUS = "status"
    COUNTRY = "country"
    LEAD_SPONSOR = "lead_sponsor"
    SPONSOR_CLASS = "sponsor_class"
    INTERVENTION_TYPE = "intervention_type"
    STUDY_TYPE = "study_type"
    CONDITION = "condition"
    DRUG = "drug"
    AGE_GROUP = "age_group"


@dataclass(frozen=True)
class DimensionInfo:
    label: str
    description: str
    source_field: str  # API field path cited as evidence
    multi_valued: bool  # one Trial can fall into several buckets
    ordered: bool  # buckets have a natural order (years, phases); otherwise sorted by count
    top_n: bool  # long-tailed: keep the top N buckets and fold the rest into "Other"
    values: "Callable[[Trial], tuple[str, ...]]"  # the bucket labels a Trial falls into (empty when not reported)
    evidence: "Callable[[Trial], Any]"  # the raw source value cited for those buckets
    temporal: bool = False  # a time axis: years in order, no silent gaps, drawn as a time series
    order: tuple[str, ...] = ()  # display order of the bucket labels when `ordered` and not temporal


DIMENSIONS: dict[Dimension, DimensionInfo] = {
    Dimension.START_YEAR: DimensionInfo(
        "Start year",
        "Year the trial started (actual or estimated)",
        "protocolSection.statusModule.startDateStruct",
        False,
        True,
        False,
        values=lambda t: (str(t.start_year),) if t.start_year else (),
        evidence=lambda t: {"date": t.start_date, "type": t.start_date_type},
        temporal=True,
    ),
    Dimension.PHASE: DimensionInfo(
        "Phase",
        "Trial phase; a multi-phase trial counts under each phase",
        "protocolSection.designModule.phases",
        True,
        True,
        False,
        order=tuple(PHASE_LABELS.values()),
        values=lambda t: tuple(PHASE_LABELS.get(p, p) for p in t.phases),
        evidence=lambda t: list(t.phases),
    ),
    Dimension.STATUS: DimensionInfo(
        "Overall status",
        "Recruitment status of the whole trial",
        "protocolSection.statusModule.overallStatus",
        False,
        False,
        False,
        values=lambda t: (t.overall_status,) if t.overall_status else (),
        evidence=lambda t: t.overall_status,
    ),
    Dimension.COUNTRY: DimensionInfo(
        "Country",
        "Countries with a current site; a trial counts once per country",
        "protocolSection.contactsLocationsModule.locations.country",
        True,
        False,
        True,
        values=lambda t: t.countries,
        evidence=lambda t: list(t.countries),
    ),
    Dimension.LEAD_SPONSOR: DimensionInfo(
        "Lead sponsor",
        "Organization responsible for the trial",
        "protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
        False,
        False,
        True,
        values=lambda t: (t.lead_sponsor,) if t.lead_sponsor else (),
        evidence=lambda t: t.lead_sponsor,
    ),
    Dimension.SPONSOR_CLASS: DimensionInfo(
        "Sponsor category",
        "Lead sponsor type: industry, NIH, other government, other…",
        "protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
        False,
        False,
        False,
        values=lambda t: (SPONSOR_CLASS_LABELS.get(c, c),) if (c := t.lead_sponsor_class) else (),
        evidence=lambda t: t.lead_sponsor_class,
    ),
    Dimension.INTERVENTION_TYPE: DimensionInfo(
        "Intervention type",
        "Drug, biological, device, procedure…",
        "protocolSection.armsInterventionsModule.interventions.type",
        True,
        False,
        False,
        values=lambda t: tuple(dict.fromkeys(i.type for i in t.interventions if i.type)),
        evidence=lambda t: sorted({i.type for i in t.interventions}),
    ),
    Dimension.STUDY_TYPE: DimensionInfo(
        "Study type",
        "Interventional, observational or expanded access",
        "protocolSection.designModule.studyType",
        False,
        False,
        False,
        values=lambda t: (t.study_type,) if t.study_type else (),
        evidence=lambda t: t.study_type,
    ),
    Dimension.CONDITION: DimensionInfo(
        "Condition",
        "Standard (MeSH) condition terms, raw text when none",
        "derivedSection.conditionBrowseModule.meshes.term",
        True,
        False,
        True,
        values=lambda t: t.condition_mesh_terms or t.conditions,
        evidence=lambda t: list(t.condition_mesh_terms or t.conditions),
    ),
    Dimension.DRUG: DimensionInfo(
        "Drug",
        "Standard (MeSH) drug terms, cleaned raw names when none",
        "derivedSection.interventionBrowseModule.meshes.term",
        True,
        False,
        True,
        values=lambda t: t.drugs,
        evidence=lambda t: list(t.intervention_mesh_terms) or [i.name for i in t.interventions],
    ),
    Dimension.AGE_GROUP: DimensionInfo(
        "Age group",
        "Age groups the trial enrolls (child, adult, older adult); a trial counts under each",
        "protocolSection.eligibilityModule.stdAges",
        True,
        True,
        False,
        values=lambda t: tuple(AGE_GROUP_LABELS.get(a, a) for a in t.std_ages),
        evidence=lambda t: list(t.std_ages),
        order=("Child (under 18)", "Adult (18–64)", "Older adult (65+)"),
    ),
}

# Labels for states that are not values, kept separate on purpose.
NOT_REPORTED = "Not reported"
OTHER_BUCKET = "Other"

DRUG_INTERVENTION_TYPES = frozenset({"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT"})

# Left out of drug groupings and networks: supportive or placebo-like interventions.
NON_DRUG_TERMS = frozenset(
    {
        "placebo",
        "placebos",
        "saline solution",
        "sodium chloride",
        "folic acid",
        "vitamin b 12",
        "vitamin b12",
        "water",
        "dexamethasone premedication",
        "standard of care",
    }
)

AGE_GROUP_LABELS: dict[str, str] = {
    "CHILD": "Child (under 18)",
    "ADULT": "Adult (18–64)",
    "OLDER_ADULT": "Older adult (65+)",
}

SPONSOR_CLASS_LABELS: dict[str, str] = {
    "INDUSTRY": "Industry",
    "NIH": "NIH",
    "FED": "U.S. federal (non-NIH)",
    "OTHER_GOV": "Other government",
    "NETWORK": "Network",
    "INDIV": "Individual",
    "OTHER": "Other (academic, hospital, non-profit)",
    "AMBIG": "Ambiguous",
    "UNKNOWN": "Unknown",
}

# Common ways people name countries that differ from the API's spelling.
COUNTRY_ALIASES: dict[str, str] = {
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "united states of america": "United States",
    "america": "United States",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "england": "United Kingdom",
    "korea": "South Korea",
    "republic of korea": "South Korea",
    "turkey": "Turkey (Türkiye)",
    "türkiye": "Turkey (Türkiye)",
    "czech republic": "Czechia",
    "russian federation": "Russia",
    "viet nam": "Vietnam",
    "uae": "United Arab Emirates",
    "holland": "Netherlands",
}

DEFAULT_TOP_N = 10

# Networks: keep the busiest nodes and only edges backed by several trials, so the graph stays readable.
NETWORK_TOP_SPONSORS = 15
NETWORK_TOP_DRUGS = 25
NETWORK_MIN_EDGE_TRIALS = 2
NETWORK_MAX_EDGES = 60

# Enrollment histogram bins (participants), chosen for a heavily skewed distribution.
# Each bin is (label, lowest, highest); highest None means no upper limit.
ENROLLMENT_BINS: list[tuple[str, int, int | None]] = [
    ("0", 0, 0),
    ("1–20", 1, 20),
    ("21–50", 21, 50),
    ("51–100", 51, 100),
    ("101–200", 101, 200),
    ("201–500", 201, 500),
    ("501–1,000", 501, 1000),
    ("1,001–5,000", 1001, 5000),
    ("Over 5,000", 5001, None),
]
ENROLLMENT_FIELD = "protocolSection.designModule.enrollmentInfo"

# Arm descriptions are long text, fetched only for drug-drug networks to tell alternatives
# ("cisplatin OR carboplatin") from combinations ("pembrolizumab PLUS pemetrexed").
ARM_DESCRIPTION_FIELDS = ["ArmGroupDescription"]
MAX_TOP_N = 50
MAX_COMPARE_SIDES = 5
PAGE_SIZE = 1000
TABLE_MAX_ROWS = 100
TIMELINE_MAX_ROWS = 50

# Structured request fields that pin a filter: request field -> (applied filter, what the user calls it).
# Ambiguity rule (harness-design: conflict): when the question names a different value for one of these,
# code asks which one is meant, offering both as valid values of the field; the answer is final.
# Sponsor is pinned as exact lead sponsor names (see filters.AppliedFilters.merge), with two exceptions:
# - a question's sponsor term ("Merck") agrees with field names that contain it ("Merck Sharp & Dohme
#   LLC"), so a sponsor conflict needs a field name that does not; every name must agree;
# - a field that repeats the question's own term is searched as a lead sponsor name, not matched exactly,
#   so choosing "your question" in a conflict can still lead to the "which Merck?" question.
# A field the previous run asked about is settled only while the conversation continues that question.
PINNED_FIELDS: dict[str, tuple[str, str]] = {
    "drug_name": ("drugs", "drug"),
    "condition": ("conditions", "condition"),
    "trial_phase": ("phases", "phase"),
    "country": ("countries", "country"),
    "status": ("statuses", "status"),
    "start_year": ("start_year_from", "start year"),
    "end_year": ("start_year_to", "end year"),
    "nct_id": ("nct_ids", "trial"),
    "sponsor": ("sponsor", "sponsor"),
}

# Drug identity: the MeSH term shared by at least this share of search matches.
DRUG_IDENTITY_MIN_SHARE = 0.5
# Sponsor ambiguity: a second distinct lead sponsor with at least this share triggers a Clarification.
SPONSOR_AMBIGUITY_MIN_SHARE = 0.05  # 2026-10-26: was 0.1, which sat on the edge for "Merck" (Merck KGaA ~10%)
DRUG_CLASS_OPTIONS = 8

# Fields requested from the API for every Trial (keeps pages small and fast).
TRIAL_FIELDS = [
    "NCTId",
    "BriefTitle",
    "OverallStatus",
    "StartDate",
    "StartDateType",
    "PrimaryCompletionDate",
    "PrimaryCompletionDateType",
    "CompletionDate",
    "CompletionDateType",
    "Phase",
    "StudyType",
    "LeadSponsorName",
    "LeadSponsorClass",
    "CollaboratorName",
    "InterventionType",
    "InterventionName",
    "InterventionOtherName",
    "InterventionArmGroupLabel",
    "ArmGroupLabel",
    "ArmGroupType",
    "ArmGroupInterventionName",
    "InterventionMeshTerm",
    "Condition",
    "ConditionMeshTerm",
    "LocationCountry",
    "RemovedCountry",
    "EnrollmentCount",
    "EnrollmentType",
    "StdAge",
]


def study_url(nct_id: str) -> str:
    return f"https://clinicaltrials.gov/study/{nct_id}"
