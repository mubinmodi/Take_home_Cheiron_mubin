# Clinical Trials Question-to-Visualization

A service that answers natural-language questions about clinical trials with a cited visualization specification, using ClinicalTrials.gov as the only source of values.

## Language

### Asking

**Question**:
The natural-language text a user sends, plus any optional structured fields.
_Avoid_: Query (reserved for API requests), prompt

**Filter**:
One constraint that selects trials, such as drug, condition, phase, sponsor, country, status, start-year range or NCT ID.
_Avoid_: Parameter, criterion

**Query Plan**:
The model's typed interpretation of a Question: its Filters, its Operation and what to group by; or a request to clarify, or a refusal. The chart type follows from it.
_Avoid_: Intent, parse, query

**Operation**:
One of five ways of turning a Cohort into data: aggregate, bin, per-trial, relate, compare.
_Avoid_: Analysis type, query type

**Clarification**:
A question with choices taken from the data, returned instead of an answer when no sensible default exists; it may allow one choice or several.
_Avoid_: Follow-up question, prompt

**Conflict**:
A structured field that names a different value from the question for the same Filter ("nivolumab" in the question, `drug_name: pembrolizumab`). Code finds it and asks a Clarification offering both values; the answer is final for that question.

**Correction**:
A suggested change to a Question that found no trials: one Filter removed, with the trials that would then match counted live. Sent back as a Follow-up.
_Avoid_: Suggestion (the response field that carries Corrections, or related questions for an unanswerable Question)

**Assumption**:
A default the service applied without asking, reported back with the answer.
_Avoid_: Note, caveat

**Comparison Group**:
One side of a compare Operation, such as "trials involving pembrolizumab only". A comparison of A and B has three groups: A only, B only, and the Overlap Group.
_Avoid_: Arm (reserved for trial arms), series, cohort

**Overlap Group**:
The Trials that belong to more than one compared side, shown as their own group.
_Avoid_: Intersection, both

**Part**:
One separate request inside a Question that asks several things ("how many X, and which countries for Y?"). A model call (the split step) finds the Parts and rewrites each to stand alone. Each Part has its own Filters, Query Plan and answer.
_Avoid_: Sub-question, sub-query

**Series**:
A second Dimension split inside one chart, shown as colours ("phases per year": years on the axis, one series per phase).
_Avoid_: Breakdown (a breakdown is one Dimension), facet

**Follow-up**:
A Question that refines or corrects an earlier Run by referring to it.
_Avoid_: Conversation, session, chat turn

### Data

**Trial**:
Any study registered on ClinicalTrials.gov, whatever its study type (interventional, observational or expanded access).
_Avoid_: Study (use Trial throughout), record

**Cohort**:
The complete set of trials a Query Plan selects, after full retrieval and match checks.
_Avoid_: Result set, sample, search results

**Search Match**:
A trial the API's text search returned; it becomes part of the Cohort only if it passes the match check.
_Avoid_: Hit, result

**Drug**:
An intervention of type drug, biological or combination product, identified by its standard (MeSH) name when one exists.
_Avoid_: Treatment, medication, intervention (which is broader)

**Lead Sponsor**:
The single organization responsible for a trial; the default meaning of "sponsor".
_Avoid_: Sponsor (when the role matters), owner

**Collaborator**:
An organization supporting a trial that is not its Lead Sponsor.
_Avoid_: Partner, co-sponsor

**Same-Arm Combination**:
Two Drugs given to the same arm of a trial.
_Avoid_: Combination therapy (unqualified), co-occurrence

**Co-Listing**:
Two Drugs listed in the same trial, possibly in different arms; not evidence of combination.
_Avoid_: Combination

### Answering

**Visualization Specification**:
The service's own description of an answer: type, title, encoding, data and rendering metadata. It is the contract with any frontend.
_Avoid_: Vega-Lite spec, chart config

**Datum**:
One displayed unit of a Visualization Specification: a bar, bucket, point, row, node or edge.
_Avoid_: Data point (when the unit is a node or edge), record

**Citation**:
The trial IDs behind one Datum, each with the source field values that placed it there.
_Avoid_: Reference, source

**Evidence**:
The shared, de-duplicated set of cited trials in one answer.
_Avoid_: Bibliography, sources list

**Run**:
One end-to-end handling of one Question, ending in exactly one Outcome.
_Avoid_: Request, job, session

**Outcome**:
How a Run ended: success, no data, clarification required, unsupported, scope required, upstream error or internal error.
_Avoid_: Status, result

**Run Record**:
The saved record of a Run: its request, Query Plan and response. Follow-ups load the earlier plan from it, and chart images are drawn from it.
_Avoid_: Run Bundle (the fuller design below)

**Run Bundle**:
Designed, not built: a Run Record plus the raw source responses, so a Run could be replayed without the model or the API.
_Avoid_: Log, trace, snapshot

**Capability Catalog**:
The single versioned statement of supported Filters, Operations, counting rules and ambiguity defaults.
_Avoid_: Config, schema, prompt
