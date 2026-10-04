# ClinicalTrials.gov API v2: data guide (API spike)

**Checked:** 2026-10-03 against the live API. `apiVersion` 2.0.5, `dataTimestamp` 2026-10-02T09:00:04, 605,599 studies.
**Saved responses:** [api-spike/](api-spike/) — `version.json`, `metadata.json` (full field tree), `enums.json` (41 enum types), `size.json`, one full study (`NCT02578680.json`, KEYNOTE-189), and every pembrolizumab trial with analysis fields (`pembrolizumab_cohort_2026-10-02.json.gz`, 2,968 studies).
**Feeds:** [harness-design.md](../harness-design.md) sections 3–4.

## 1. Endpoints we need

| Endpoint | Use | Notes |
|---|---|---|
| `GET /api/v2/studies` | Search and fetch trials | Main endpoint. Query, filter, `fields`, paging. |
| `GET /api/v2/studies/{nctId}` | One study, full record | For single-study questions and citation detail. |
| `GET /api/v2/version` | Data timestamp | Cache key and provenance in every response. Data refreshes daily. |
| `GET /api/v2/studies/metadata` | Field tree | Contract test: every field path we read must exist here. |
| `GET /api/v2/studies/enums` | Allowed values | Source for the catalog's phase, status, intervention type, sponsor class lists. |
| `GET /api/v2/stats/field/values` | Value counts | **Whole database only.** Rejects `query.*` parameters (`Invalid prefix in parameter name`). Not usable for filtered charts. |

## 2. What one study record contains

A study has four sections. Only `protocolSection` and `derivedSection` matter for charts.

| Module (path under `protocolSection`) | Fields we use | Shape |
|---|---|---|
| `identificationModule` | `nctId`, `briefTitle`, `acronym` | Text |
| `statusModule` | `overallStatus`; `startDateStruct`, `primaryCompletionDateStruct`, `completionDateStruct` (each `{date, type}`); `studyFirstPostDateStruct` | Dates are `YYYY-MM-DD` or `YYYY-MM`; `type` is `ACTUAL` or `ESTIMATED` |
| `sponsorCollaboratorsModule` | `leadSponsor {name, class}`, `collaborators[] {name, class}` | `class` ∈ NIH, FED, OTHER_GOV, INDIV, INDUSTRY, NETWORK, AMBIG, OTHER, UNKNOWN |
| `conditionsModule` | `conditions[]`, `keywords[]` | Free text, one study can list up to 101 |
| `designModule` | `studyType`, `phases[]`, `designInfo.allocation`, `designInfo.primaryPurpose`, `enrollmentInfo {count, type}` | `phases` is an **array** (e.g. `[PHASE1, PHASE2]`) |
| `armsInterventionsModule` | `interventions[] {type, name, otherNames[], armGroupLabels[]}`, `armGroups[] {label, type, interventionNames[]}` | Arm `interventionNames` are `"Type: Name"` strings |
| `contactsLocationsModule` | `locations[] {facility, status, city, state, country, geoPoint}`, `overallOfficials[]` | One row per **site** |

| Module (path under `derivedSection`) | Fields | Why it matters |
|---|---|---|
| `interventionBrowseModule.meshes[]` | `{id, term}` | Standard drug identity (e.g. `C582435 pembrolizumab`), independent of how the sponsor typed the name |
| `conditionBrowseModule.meshes[]`, `.ancestors[]` | `{id, term}` | Standard disease identity and its parent terms (e.g. NSCLC → Lung Neoplasms) |
| `miscInfoModule.removedCountries[]` | Country names | Countries whose sites were removed from the record (see 5.6) |

`resultsSection` (outcomes, adverse events) exists for studies with `hasResults: true`. It is out of scope for now.

## 3. How question parts map to API parameters (verified)

All counts below are for `query.intr=Pembrolizumab` unless shown otherwise.

| Question part | Parameter | Example | Count |
|---|---|---|---|
| Drug (search, synonym-expanded) | `query.intr` | `Pembrolizumab` / `Keytruda` / `MK-3475` | 2,968 each |
| Drug (exact standard identity) | `filter.advanced=AREA[InterventionMeshTerm]"pembrolizumab"` | — | 2,559 |
| Condition (search) | `query.cond` | `lung cancer` | 14,597 |
| Condition (standard identity) | `AREA[ConditionMeshTerm]"Lung Neoplasms"` | — | 6,133 |
| Sponsor (search, lead **or** collaborator) | `query.spons` | `Merck` | 5,227 |
| Lead sponsor only | `AREA[LeadSponsorName]Merck` | — | 2,750 |
| Collaborator only | `AREA[CollaboratorName]Merck` | — | 2,594 |
| Status | `filter.overallStatus` | `RECRUITING` | 712 |
| Phase | `aggFilters=phase:3` or `AREA[Phase]PHASE3` | — | 367 (both) |
| Start year | `AREA[StartDate]RANGE[2020-01-01,2020-12-31]` | — | 263 |
| Country | `AREA[LocationCountry]Germany` | — | 326 |
| Distance | `filter.geo=distance(52.52,13.40,50mi)` | Berlin | 165 |
| One study | `filter.ids=NCT02142738` | — | 1 |
| Intervention type | `AREA[InterventionType]DRUG` | — | 2,525 |

**Count check works.** Counting the downloaded cohort locally gives exactly the API's own totals: start year 2020 = 263, phase 3 = 367 (324 `PHASE3` + 43 `PHASE2/PHASE3`), Germany = 326, recruiting = 712. So the verifier can compare each bucket with a `countTotal` request for the same filter.

## 4. Paging and speed

- `pageSize` maximum is 1,000 (a larger value is accepted and capped).
- `nextPageToken` continues the page; `totalCount` only on the first page and only with `countTotal=true`.
- With a trimmed `fields` list, 2,968 studies came back in 3 pages in **3.4 seconds** (~1.1 s per page).
- A full record averages 17 KB (median 10 KB, 99th percentile 150 KB). Always send `fields`.
- **No rate-limit headers** were returned. The reported ~50 requests/minute limit is still unconfirmed; do not test it by flooding the API. Keep the shared token bucket.

## 5. Data quality findings (pembrolizumab cohort, 2,968 studies)

### 5.1 Search match is not the same as "a trial of this drug"
`query.intr=Pembrolizumab` returns 409 studies (14%) with no pembrolizumab MeSH term. In 328 of them (11%), pembrolizumab is not an intervention at all; it appears in other text (e.g. `NCT05553782`, an implantable microdevice study). 77 name it as an intervention but have no MeSH code.
**Implication:** retrieve with `query.intr` (synonym-expanded, broad), then keep a trial only if the drug is one of its interventions: by MeSH term, or by name/other-name match when MeSH is missing. Report the excluded count in `assumptions[]`.

### 5.2 Drug names are messy; MeSH fixes most of it
503 different raw spellings of pembrolizumab: `Pembrolizumab`, `pembrolizumab`, `MK-3475`, `Keytruda`, `Pembrolizumab 200 mg`, `Pembrolizumab (KEYTRUDA®)`, `Pembrolizumab/Quavonlimab`, … 93% of studies have intervention MeSH terms.
**Implication:** network nodes and drug grouping use the MeSH term as identity; fall back to a cleaned raw name (lower case, no dose, no ®) and mark it as unresolved.

### 5.3 "Drug" must include Biological
Pembrolizumab is recorded as `DRUG` 1,902 times and as `BIOLOGICAL` 793 times (plus a few odd types). Filtering on `DRUG` alone loses about a third of its mentions.
**Implication:** the default "drug" set is `DRUG` + `BIOLOGICAL` (and probably `COMBINATION_PRODUCT`).

### 5.4 Combination therapy can be shown from arm data
98% of studies link arms to interventions. Of 17,188 drug pairs listed in the same trial, 66% are given in the **same arm**; 34% are only co-listed (e.g. drug A in one arm, drug B in the comparator arm). KEYNOTE-189 shows both: pembrolizumab + pemetrexed share the experimental arm, while saline appears only in the control arm.
**Implication:** the drug ↔ drug network can claim "given together in the same arm" with evidence. This upgrades Q-09 from "co-listing only" to supportable. Noise such as saline, folic acid and vitamin B12 needs a filter (intervention type `DIETARY_SUPPLEMENT`, placebo/saline names).

### 5.5 Phases are lists
16% of studies have two phases (`PHASE1/PHASE2` 473, `PHASE2/PHASE3` 43); 6% have no phase (mostly observational). The API's own phase filter counts a `PHASE2/PHASE3` study under both phases.
**Implication:** a counting rule is needed: show combined phases as their own category (`Phase 1/2`), or count the study under each phase. The API count check matches the second option.

### 5.6 Countries: two traps
- Count **distinct trials**, not sites: the United States has 1,833 pembrolizumab trials but 37,029 site rows. One study has 1,660 sites.
- 7% of studies have no `locations`; some completed studies (e.g. KEYNOTE-189) had all sites moved to `removedCountries`. Germany: 326 trials by current locations, 370 including removed countries. The API's `LocationCountry` filter matches the first number.
**Implication:** default to current locations (matches the API), and state in `assumptions[]` how many studies had sites removed.

### 5.7 Dates
Start dates: 92% full date, 8% month only (`YYYY-MM`), 5 missing. 10% are `ESTIMATED` (future or not yet confirmed), and some start years are in the future (2026: 216, 2027: 7).
**Implication:** "year" = start date year works for 99.8%; label estimated dates and keep future years visible, not silently dropped.

### 5.8 Sponsors
Lead sponsor names are clean for big sponsors (`Merck Sharp & Dohme LLC`: 274 lead studies). 48% of studies have collaborators. `query.spons` searches lead **and** collaborators (Merck: 5,227 vs 2,750 lead-only). Sponsor class (`INDUSTRY`, `NIH`, `OTHER`, …) is present on almost every study and answers "sponsor categories".

### 5.9 Conditions
3,015 different raw condition strings in this one cohort, including `Non-small Cell Lung Cancer - Enrollment Completed` and stage variants. `query.cond=NSCLC` (8,778) and `query.cond=non-small cell lung cancer` (8,581) give different counts.
**Implication:** group by condition MeSH term, not raw text; search with `query.cond` and record the exact term used.

### 5.10 Enrollment
Present for all but 5 studies; 43% are `ESTIMATED` (planned, not actual). A histogram must label or separate estimated values.

## 6. What this settles and what is still open

Settled by evidence:
- Synonym search works: Keytruda / MK-3475 / pembrolizumab return the same trials.
- Local counting reproduces API totals, so the verifier's count check is feasible.
- Arm-level combinations are available for 98% of studies.
- A 3,000-trial question fetches in about 3–4 seconds.
- The stats endpoint cannot do filtered counts; code must count.

Needs a decision (counting rules, `harness-design.md` section 3):
1. Multi-phase studies: own category, or count under each phase?
2. Search matches that do not list the drug as an intervention: exclude (recommended) or keep?
3. Countries: current sites only (recommended, matches API) or include removed countries?
4. Which intervention types count as "drug": `DRUG` + `BIOLOGICAL` (+ `COMBINATION_PRODUCT`)?
5. Which supplements or placebo-like interventions to drop from drug networks?

Unverified: the rate limit.
