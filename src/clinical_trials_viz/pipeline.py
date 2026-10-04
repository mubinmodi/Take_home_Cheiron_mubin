"""The fixed workflow: plan -> gate -> retrieve -> count -> build -> verify -> respond.

Every Run ends in exactly one Outcome. The model is called at most three times.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from opentelemetry import trace
from pydantic_ai import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded

from clinical_trials_viz import clarify
from clinical_trials_viz.analyze import breakdown, comparison_groups, enrollment_histogram
from clinical_trials_viz.catalog import DEFAULT_TOP_N, ENROLLMENT_FIELD, Dimension
from clinical_trials_viz.cohort import Cohort, fetch_cohort
from clinical_trials_viz.ctgov.client import CtGovClient, ScopeTooLarge, UpstreamError
from clinical_trials_viz.ctgov.trial import Trial
from clinical_trials_viz.models.plan import (
    AnswerPlan,
    ClarifyPlan,
    NetworkKind,
    Operation,
    PerTrialView,
    QueryPlan,
    UnsupportedPlan,
)
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.models.response import AppliedFilters, Outcome, QueryResponse, SourceInfo
from clinical_trials_viz.models.spec import NetworkData, VisualizationSpec, VisualizationType
from clinical_trials_viz.network import drug_drug_network, same_arm_pairs, sponsor_drug_network
from clinical_trials_viz.planner import Planner, PlannerNotConfigured
from clinical_trials_viz.runs import RunStore, new_run_id
from clinical_trials_viz.spec_builder import (
    breakdown_spec,
    build_evidence,
    chart_type_for,
    comparison_spec,
    histogram_spec,
    network_spec,
    single_value_spec,
    table_spec,
    timeline_spec,
)
from clinical_trials_viz.validate import check_plan
from clinical_trials_viz.verify import verify

MAX_MODEL_CALLS = 3
START_FIELD = "protocolSection.statusModule.startDateStruct"
PRIMARY_COMPLETION_FIELD = "protocolSection.statusModule.primaryCompletionDateStruct"
COMPLETION_FIELD = "protocolSection.statusModule.completionDateStruct"
ARM_FIELD = "protocolSection.armsInterventionsModule.armGroups (drug pairs given in the same arm)"
log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class RunNotFound(Exception):
    pass


class _Run:
    """Mutable state of one Run."""

    def __init__(self, request: QueryRequest, api_requests_before: int):
        self.request = request
        self.response = QueryResponse(run_id=new_run_id(), outcome=Outcome.INTERNAL_ERROR)
        self.model_calls = 0
        self.api_requests_before = api_requests_before
        self.timings: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        with tracer.start_as_current_span(name):
            try:
                yield
            finally:
                self.timings[name] = round((time.perf_counter() - start) * 1000, 1)

    def finish(self, outcome: Outcome, message: str | None = None) -> QueryResponse:
        self.response.outcome = outcome
        self.response.message = message
        return self.response


class Pipeline:
    def __init__(self, client: CtGovClient, planner: Planner, runs: RunStore, public_base_url: str):
        self.client = client
        self.planner = planner
        self.runs = runs
        self.public_base_url = public_base_url.rstrip("/")
        self._countries: set[str] | None = None

    async def known_countries(self) -> set[str]:
        if self._countries is None:
            try:
                self._countries = set(await self.client.countries())
            except UpstreamError:
                return set()  # validate country names later instead of failing the Run
        return self._countries

    async def run(self, request: QueryRequest) -> QueryResponse:
        previous: QueryPlan | None = None
        if request.previous_run_id:
            record = self.runs.load(request.previous_run_id)
            if record is None:
                raise RunNotFound(request.previous_run_id)
            previous = record.plan

        run = _Run(request, self.client.requests_made)
        with tracer.start_as_current_span("run") as span:
            span.set_attribute("run.id", run.response.run_id)
            try:
                await self._execute(run, previous)
            except ScopeTooLarge as exc:
                run.finish(
                    Outcome.SCOPE_REQUIRED,
                    f"{exc.total:,} trials match, more than the {exc.limit:,} this service retrieves "
                    "per question. Narrow it, e.g. by phase, status, start years, country or sponsor.",
                )
            except UpstreamError as exc:
                run.finish(Outcome.UPSTREAM_ERROR, str(exc))
            except (ModelAPIError, UsageLimitExceeded) as exc:
                run.finish(Outcome.UPSTREAM_ERROR, f"The planning model failed: {exc}")
            except PlannerNotConfigured as exc:
                run.finish(Outcome.INTERNAL_ERROR, str(exc))
            except UnexpectedModelBehavior as exc:
                run.finish(Outcome.INTERNAL_ERROR, f"The planning model returned an unusable plan: {exc}")
            except Exception:
                log.exception("run %s failed", run.response.run_id)
                run.finish(Outcome.INTERNAL_ERROR, "Unexpected error; see server logs.")
            span.set_attribute("run.outcome", run.response.outcome.value)

        response = run.response
        response.model_calls = run.model_calls
        response.timings_ms = run.timings
        if response.source:
            response.source.api_requests = self.client.requests_made - run.api_requests_before
        self.runs.save(request, response)
        return response

    async def _plan(self, run: _Run, previous: QueryPlan | None) -> QueryPlan:
        """Plan, then gate; repair once with the gate's errors. Returns a gated plan."""
        request = run.request
        with run.stage("plan"):
            result = await self.planner.plan(
                request.query, request.structured_fields(), previous, max_calls=MAX_MODEL_CALLS
            )
        run.model_calls += result.model_calls
        plan = result.plan
        if not isinstance(plan, AnswerPlan):
            return plan

        gate = check_plan(plan, request, await self.known_countries())
        if gate.errors and run.model_calls < MAX_MODEL_CALLS:
            with run.stage("repair"):
                result = await self.planner.plan(
                    request.query,
                    request.structured_fields(),
                    previous,
                    repair=(result.messages, gate.errors),
                    max_calls=MAX_MODEL_CALLS - run.model_calls,
                )
            run.model_calls += result.model_calls
            plan = result.plan
            if not isinstance(plan, AnswerPlan):
                return plan
            gate = check_plan(plan, request, await self.known_countries())
        if gate.errors:
            return UnsupportedPlan(
                reason="The question could not be turned into a valid plan: " + "; ".join(gate.errors)
            )
        if gate.unsupported:
            return UnsupportedPlan(reason=gate.unsupported)
        run.response.applied_filters = gate.filters
        run.response.assumptions.extend(gate.assumptions)
        return plan

    async def _execute(self, run: _Run, previous: QueryPlan | None) -> None:
        plan = await self._plan(run, previous)
        response = run.response
        response.plan = plan

        if isinstance(plan, UnsupportedPlan):
            run.finish(Outcome.UNSUPPORTED_QUERY, plan.reason)
            return
        if isinstance(plan, ClarifyPlan):
            with run.stage("clarify"):
                response.clarification = await clarify.from_plan(plan, self.client, run.request.structured_fields())
            run.finish(Outcome.CLARIFICATION_REQUIRED, response.clarification.question)
            return

        response.relation = plan.relation
        filters = response.applied_filters
        assert filters is not None
        version = await self.client.version()
        response.source = SourceInfo(
            api_version=version.api_version, data_timestamp=version.data_timestamp, retrieved_at=datetime.now(UTC)
        )

        if plan.operation is Operation.COMPARE:
            await self._compare(run, plan, filters)
            return

        with run.stage("retrieve"):
            cohort = await fetch_cohort(self.client, filters)
        response.source.search_matches = cohort.search_matches
        response.source.cohort_size = len(cohort.trials)
        response.assumptions.extend(cohort.assumptions)

        if filters.sponsor and not filters.sponsor_exact and filters.sponsor_role == "lead":
            question = clarify.sponsor_ambiguity(cohort.trials, filters.sponsor)
            if question:
                response.clarification = question
                run.finish(Outcome.CLARIFICATION_REQUIRED, question.question)
                return
        if not cohort.trials:
            run.finish(Outcome.NO_DATA, "No trials match these filters (all matching pages were retrieved).")
            return

        with run.stage("analyze"):
            spec, dimension = self._build(run, plan, filters, cohort)
        if isinstance(spec.data, NetworkData) and not spec.data.edges:
            run.finish(Outcome.NO_DATA, "No entities share enough trials to draw a link (all pages retrieved).")
            return
        self._finish_success(run, plan, spec, {t.nct_id: t for t in cohort.trials}, dimension)

    def _build(
        self, run: _Run, plan: AnswerPlan, filters: AppliedFilters, cohort: Cohort
    ) -> tuple[VisualizationSpec, Dimension | None]:
        if plan.operation is Operation.PER_TRIAL:
            build = timeline_spec if plan.view is PerTrialView.TIMELINE else table_spec
            spec, notes = build(cohort.trials, filters)
            run.response.assumptions.extend(notes)
            return spec, None
        if plan.operation is Operation.BIN:
            histogram = enrollment_histogram(cohort.trials)
            run.response.assumptions.extend(histogram.assumptions)
            return histogram_spec(histogram, filters, len(cohort.trials)), None
        if plan.operation is Operation.RELATE:
            kind = plan.network or NetworkKind.SPONSOR_DRUG
            build = sponsor_drug_network if kind is NetworkKind.SPONSOR_DRUG else drug_drug_network
            network = build(cohort.trials)
            run.response.assumptions.extend(network.assumptions)
            return network_spec(network, filters, network.contributing_trials, kind), None
        if plan.group_by is None:
            return single_value_spec(cohort.trials, filters), None
        result = breakdown(cohort.trials, plan.group_by, plan.top_n or DEFAULT_TOP_N)
        run.response.assumptions.extend(result.assumptions)
        return breakdown_spec(result, filters, len(cohort.trials)), plan.group_by

    async def _compare(self, run: _Run, plan: AnswerPlan, base: AppliedFilters) -> None:
        response = run.response
        sides: dict[str, list[Trial]] = {}
        with run.stage("retrieve"):
            for side in plan.compare_sides:
                f = base.model_copy(deep=True)
                if side.drug:
                    f.drugs, label = [side.drug], side.drug
                elif side.condition:
                    f.conditions, label = [side.condition], side.condition
                else:
                    f.sponsor, f.sponsor_role, f.sponsor_exact, label = side.sponsor, "lead", False, side.sponsor
                cohort = await fetch_cohort(self.client, f)
                sides[label or "?"] = cohort.trials
                response.assumptions.extend(cohort.assumptions)
                assert response.source
                response.source.search_matches += cohort.search_matches
        trials = {t.nct_id: t for ts in sides.values() for t in ts}
        assert response.source
        response.source.cohort_size = len(trials)
        if not trials:
            run.finish(Outcome.NO_DATA, "No trials match any of the compared sides.")
            return
        with run.stage("analyze"):
            groups = comparison_groups(sides)
            spec, notes = comparison_spec(groups, plan.group_by, base, list(sides), plan.top_n or DEFAULT_TOP_N)
        response.assumptions.extend(notes)
        self._finish_success(run, plan, spec, trials, plan.group_by)

    def _finish_success(
        self,
        run: _Run,
        plan: AnswerPlan,
        spec: VisualizationSpec,
        trials: dict[str, Trial],
        dimension: Dimension | None,
    ) -> None:
        response = run.response
        with run.stage("verify"):
            filters = response.applied_filters or AppliedFilters()
            sides = tuple(_cited_dimensions(plan))
            evidence = build_evidence(spec, trials, dimension, filters, sides)
            if spec.type is VisualizationType.TIMELINE:
                for nct_id, entry in evidence.items():
                    trial = trials[nct_id]
                    entry.fields[START_FIELD] = {"date": trial.start_date, "type": trial.start_date_type}
                    entry.fields[PRIMARY_COMPLETION_FIELD] = {
                        "date": trial.primary_completion_date,
                        "type": trial.primary_completion_date_type,
                    }
                    entry.fields[COMPLETION_FIELD] = {"date": trial.completion_date, "type": trial.completion_date_type}
            if plan.operation is Operation.BIN:
                for nct_id, entry in evidence.items():
                    trial = trials[nct_id]
                    entry.fields[ENROLLMENT_FIELD] = {"count": trial.enrollment, "type": trial.enrollment_type}
            if plan.network is NetworkKind.DRUG_DRUG:
                for nct_id, entry in evidence.items():  # the arms that make each link a combination
                    entry.fields[ARM_FIELD] = {
                        f"{a} + {b}": arms for (a, b), arms in same_arm_pairs(trials[nct_id]).items()
                    }
            verification = verify(spec, evidence, trials, dimension, chart_type_for(plan), filters)
        response.verification = verification
        if not verification.passed:
            run.finish(Outcome.INTERNAL_ERROR, "The answer failed verification and was withheld.")
            return
        response.visualization = spec
        response.evidence = evidence
        if spec.type is not VisualizationType.TABLE:
            response.chart_url = f"{self.public_base_url}/v1/runs/{response.run_id}/chart.png"
        run.finish(Outcome.SUCCESS)


def _cited_dimensions(plan: AnswerPlan) -> list[Dimension]:
    """Fields beyond the filters that place a trial in the answer: its comparison side, or the
    entities a network links. They are cited too."""
    if plan.operation is Operation.RELATE:
        if plan.network is NetworkKind.DRUG_DRUG:
            return [Dimension.DRUG]
        return [Dimension.LEAD_SPONSOR, Dimension.DRUG]
    found = []
    for side in plan.compare_sides:
        if side.drug:
            found.append(Dimension.DRUG)
        elif side.condition:
            found.append(Dimension.CONDITION)
        elif side.sponsor:
            found.append(Dimension.LEAD_SPONSOR)
    return list(dict.fromkeys(found))
