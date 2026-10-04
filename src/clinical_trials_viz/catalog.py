"""Capability catalog: the single versioned statement of what the service supports.

It feeds the planner prompt, the plan validator, the analysis code and the tests.
Rules here are decided in docs/harness-design.md sections 2-3.
"""

from dataclasses import dataclass
from enum import StrEnum

CATALOG_VERSION = "2026-10-03.1"


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


@dataclass(frozen=True)
class DimensionInfo:
    label: str
    description: str
    source_field: str  # API field path cited as evidence
    multi_valued: bool  # one Trial can fall into several buckets
    ordered: bool  # buckets have a natural order (years, phases); otherwise sorted by count
    top_n: bool  # long-tailed: keep the top N buckets and fold the rest into "Other"


DIMENSIONS: dict[Dimension, DimensionInfo] = {
    Dimension.START_YEAR: DimensionInfo(
        "Start year",
        "Year the trial started (actual or estimated)",
        "protocolSection.statusModule.startDateStruct",
        False,
        True,
        False,
    ),
    Dimension.PHASE: DimensionInfo(
        "Phase",
        "Trial phase; a multi-phase trial counts under each phase",
        "protocolSection.designModule.phases",
        True,
        True,
        False,
    ),
    Dimension.STATUS: DimensionInfo(
        "Overall status",
        "Recruitment status of the whole trial",
        "protocolSection.statusModule.overallStatus",
        False,
        False,
        False,
    ),
    Dimension.COUNTRY: DimensionInfo(
        "Country",
        "Countries with a current site; a trial counts once per country",
        "protocolSection.contactsLocationsModule.locations.country",
        True,
        False,
        True,
    ),
    Dimension.LEAD_SPONSOR: DimensionInfo(
        "Lead sponsor",
        "Organization responsible for the trial",
        "protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
        False,
        False,
        True,
    ),
    Dimension.SPONSOR_CLASS: DimensionInfo(
        "Sponsor category",
        "Lead sponsor type: industry, NIH, other government, other…",
        "protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
        False,
        False,
        False,
    ),
    Dimension.INTERVENTION_TYPE: DimensionInfo(
        "Intervention type",
        "Drug, biological, device, procedure…",
        "protocolSection.armsInterventionsModule.interventions.type",
        True,
        False,
        False,
    ),
    Dimension.STUDY_TYPE: DimensionInfo(
        "Study type",
        "Interventional, observational or expanded access",
        "protocolSection.designModule.studyType",
        False,
        False,
        False,
    ),
    Dimension.CONDITION: DimensionInfo(
        "Condition",
        "Standard (MeSH) condition terms, raw text when none",
        "derivedSection.conditionBrowseModule.meshes.term",
        True,
        False,
        True,
    ),
    Dimension.DRUG: DimensionInfo(
        "Drug",
        "Standard (MeSH) drug terms, cleaned raw names when none",
        "derivedSection.interventionBrowseModule.meshes.term",
        True,
        False,
        True,
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
MAX_TOP_N = 50
MAX_COMPARE_SIDES = 5
PAGE_SIZE = 1000
TABLE_MAX_ROWS = 100

# Drug identity: the MeSH term shared by at least this share of search matches.
DRUG_IDENTITY_MIN_SHARE = 0.5
# Sponsor ambiguity: a second distinct lead sponsor with at least this share triggers a Clarification.
SPONSOR_AMBIGUITY_MIN_SHARE = 0.1
DRUG_CLASS_OPTIONS = 8

# Fields requested from the API for every Trial (keeps pages small and fast).
TRIAL_FIELDS = [
    "NCTId",
    "BriefTitle",
    "OverallStatus",
    "StartDate",
    "StartDateType",
    "PrimaryCompletionDate",
    "CompletionDate",
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
]


def study_url(nct_id: str) -> str:
    return f"https://clinicaltrials.gov/study/{nct_id}"
