# Take-Home Assignment: ClinicalTrials.gov Query-to-Visualization Agent (Backend)

**Role:** Primary source reference for the current project.  
**Captured:** 2026-10-03, by visually rereading all six user-provided images.  
**Companion documents:** [Review rubric](take-home-rubric.md) · [Exploration agenda](take-home-exploration.md).

The assignment text below is transcribed from the images. Formatting and wrapped lines are normalized; the requirements, examples, scoring, and qualifications are preserved. Editorial notes are explicitly labeled. Examples are illustrative assignment content, not measurements or outputs from our implementation.

## Source image index

1. `IMG_5198.png`: title, time/tools/goal, sections 1–2, start of section 3.1.
2. `IMG_5196.png`: remainder of section 3.1, section 3.2.
3. `IMG_5086.png`: example response, section 4, start of section 5.
4. `IMG_4447.png`: remainder of section 5, section 6.
5. `IMG_5978.png`: sections 7–8.
6. `IMG_5213.png`: appendix with all nine example questions.

Original attachments, in that order. **To do:** copy the six screenshots into `docs/assignment-images/` (macOS blocked automated access to the Messages attachments folder). The transcription below is complete without them.

- [`IMG_5198.png`](assignment-images/IMG_5198.png)
- [`IMG_5196.png`](assignment-images/IMG_5196.png)
- [`IMG_5086.png`](assignment-images/IMG_5086.png)
- [`IMG_4447.png`](assignment-images/IMG_4447.png)
- [`IMG_5978.png`](assignment-images/IMG_5978.png)
- [`IMG_5213.png`](assignment-images/IMG_5213.png)

## Assignment header — image 1

**Time expectation:** ~24 hours  
**Tools allowed:** Any programming language, libraries, AI tools, and internet access  
**Primary goal:** Build a backend service that converts clinical-trials questions into **structured visualization outputs backed by ClinicalTrials.gov API data**.

## 1) Problem Overview — image 1

You will build an AI-enabled backend that answers questions about clinical trials using the **ClinicalTrials.gov API**. The user will provide a natural-language query, along with optional structured fields (which you may define). Your system must:

1. Interpret the user's question.
2. Retrieve relevant data from [ClinicalTrials.gov](https://clinicaltrials.gov/).
3. Identify if a visualization is needed and what type of visualization is suitable for the given question.
4. Produce a **visualization specification** that answers the question.

A frontend is **not required**, but your output must be clear and structured so that a frontend can render the visualization reliably.

## 2) Data Source — image 1

Use the **ClinicalTrials.gov Data API** as the authoritative data source.

- API documentation: <https://clinicaltrials.gov/data-api/api>
- You may use any endpoints/fields needed.

## 3) Functional Requirements — images 1–3

### 3.1 Inputs

Your service must accept a request containing:

**Required**

- `query` (string): a natural language question about clinical trials.

**Optional structured fields (candidate-defined)**

You may define an input schema that includes additional parameters. Examples (not required):

- `drug_name`
- `condition/disease`
- `trial_phase`
- `sponsor`
- `country/location`
- `start_year`, `end_year`
- any other fields you find useful

**You must document your request schema** (field names, types, optional/required, validation).

An example request may look like:

```json
{
  "query": "How has the number of trials for this drug changed over time?",
  "drug_name": "Pembrolizumab"
}
```

### 3.2 Outputs

Your service must return a structured response describing a visualization.

**Required output components**

1. **Visualization specification**
   - `type`: the visualization type (e.g., `bar_chart`, `time_series`, `network_graph`, etc.)
   - `title`: a human-readable title
   - `encoding`: a clear mapping from fields to visual channels (e.g., x-axis, y-axis, series, nodes/edges)
   - `data`: the data points required to render the visualization
2. **Response metadata**
   - any additional fields needed for the frontend to render appropriately (units, sorting, time granularity, grouping choices, etc.)
   - optional notes about assumptions, filters applied, or query interpretation

**You must document your response schema** so that a frontend engineer can implement a renderer without guessing. If you build a simple frontend demo, that's a **bonus**, but **backend + structured outputs** are the focus.

Example response (illustrative only):

```json
{
  "visualization": {
    "type": "bar_chart",
    "title": "Trials by Phase for Pembrolizumab",
    "encoding": {
      "x": {"field": "phase"},
      "y": {"field": "trial_count"}
    },
    "data": [
      {"phase": "Phase 1", "trial_count": 32},
      {"phase": "Phase 2", "trial_count": 78}
    ]
  },
  "meta": {
    "filters": {"drug_name": "Pembrolizumab"},
    "source": "clinicaltrials.gov"
  }
}
```

## 4) Visualization Requirements — image 3

- The answer to the query must be a **visualization** (via structured specification).
- You may support any visualization types, but your system should aim to support **multiple types**, such as:
  - bar chart / grouped bar chart
  - timeline/time series
  - scatter plot
  - histogram
  - network graph (entities like drugs, sponsors, conditions, investigators, sites)

**Design goal:** Cover as many query types as possible with a **single coherent approach** and support **multiple visualization types**. Submissions that implement richer visualizations (e.g., meaningful network graphs) and broader query coverage will be scored higher than those that only support one chart type.

## 5) Bonus: Deep Citations (Source Traceability) — images 3–4

As a bonus, include **deep citations** from ClinicalTrials.gov to support the values shown in the visualization.

What “deep citations” mean here:

- Each visualized datum (e.g., a bar, time bucket, node/edge weight) includes references to the underlying trial records that contributed to it.
- Each reference includes:
  - `nct_id`
  - an **exact text excerpt** from the API response (or a specific field/value) that supports the datum

Example (illustrative only):

```json
{
  "phase": "Phase 3",
  "trial_count": 41,
  "citations": [
    {
      "nct_id": "NCT01234567",
      "excerpt": "Phase 3 randomized study evaluating pembrolizumab..."
    }
  ]
}
```

This is intentionally challenging; implement as much as is reasonable in the time box.

## 6) Submission Requirements — image 4

Submit a **zip file** containing:

1. **Code:** All source code required to run the service.
2. **README:** must include
   - how to run (install, configure, start)
   - request/response schema documentation (inputs/outputs)
   - key design decisions and tradeoffs
   - limitations and what you would improve with more time
3. **Example Runs:** Provide **3–5 example queries with the actual JSON outputs produced by your system**.
4. **(Optional) Demo**
   - a small UI
   - a deployed endpoint
   - a short demo video

## 7) Evaluation Criteria — image 5

We will evaluate submissions on:

1. **System Design (35%)**
   - clear, rational design decisions
   - maintainable structure and extensibility
   - sensible handling of real-world API data
2. **AI / Agent Design (20%)**
   - avoid hallucination-prone steps
   - include validation or constraints
   - sensible planning and reasoning steps, along with appropriate tools
3. **Code Quality (20%)**
   - readability, organization, documentation
   - correctness and robustness
4. **Query and Visualization Coverage (15%)**
   - breadth of supported query types
   - ability to handle multiple question classes without one-off hacks
   - richer visualizations (e.g., meaningful network graphs) score higher than simple single-chart systems
5. **Input/Output Design (10%)**
   - well-structured, unambiguous schemas
   - frontend-friendly visualization specification

**Bonus consideration:** Deep citations/traceability to source records.

## 8) Integrity Note (Use AI Tools Freely) — image 5

You may use AI tools and online resources. We care about your **engineering judgment and design reasoning**.

In your README, briefly describe:

- which tools you used (if any)
- how you validated correctness
- what parts you designed/implemented deliberately vs generated and adapted

We reward submissions that show evidence of thoughtful construction, testing, and iteration.

## Appendix: Example Query Types (Non-Exhaustive) — image 6

You do not need to support all of these, but they illustrate the breadth we care about:

**Time trends**

- “How has the number of trials for [drug] changed per year since 2015?”
- “How many trials started each year for [condition]?”

**Distributions**

- “How are [condition] trials distributed across phases?”
- “What are the most common intervention types for [drug/condition] trials?”

**Comparisons**

- “Compare phases for trials involving Drug A vs Drug B.”
- “Compare sponsor categories across two conditions.”

**Geographic patterns**

- “Which countries have the most recruiting trials for [condition]?”

**Relationships/networks**

- “Show a network of sponsors ↔ drugs for [condition] trials.”
- “Which drugs frequently co-occur in combination studies (drug ↔ drug network)?”

To repeat, these are just some example queries and visualization possible. You are not restricted to these in your assignment.

## Editorial notes: what the source leaves open

- Section 1 asks the system to identify whether a visualization is needed; section 4 says the answer must be a visualization. The rubric treats supported analytical successes as visual answers and records clarification, unsupported, and no-data behavior as a design decision.
- The example request asks for a trend; the example response shows a phase distribution. They are independent illustrative shapes, not a correct end-to-end question/answer pair.
- The example citation shows one reference beside a count of 41. It illustrates citation shape; it does not establish that one record substantiates a 41-record count.
- No numerical bonus allocation, subcriterion weights, pass mark, deadline, hosting requirement, LLM provider, agent framework, chart library, or database is specified.
- The ~24-hour expectation is preserved. The user has not supplied a submission deadline or a separate available-hours budget.
- The user prioritizes this assignment and wants to explore ways to exceed it. That expands the design discussion; it does not make every possible extension a committed deliverable.
