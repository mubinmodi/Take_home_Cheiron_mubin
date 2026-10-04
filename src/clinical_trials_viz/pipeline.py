"""The fixed workflow: plan -> gate -> retrieve -> count -> build -> verify -> respond.

Every Run ends in exactly one Outcome. The model is called at most three times.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from opentelemetry import trace

from clinical_trials_viz import clarify
from clinical_trials_viz.analyze import breakdown, comparison_groups, cross_breakdown, enrollment_histogram
from clinical_trials_viz.catalog import ARM_DESCRIPTION_FIELDS, DEFAULT_TOP_N, ENROLLMENT_FIELD, Dimension
from clinical_trials_viz.cohort import Cohort, fetch_cohort
from clinical_trials_viz.ctgov.client import CtGovClient, UpstreamError
from clinical_trials_viz.ctgov.trial import Trial
from clinical_trials_viz.failures import Failure, classify
from clinical_trials_viz.models.plan import (
    AnswerPlan,
    ClarifyPlan,
    MultiAnswerPlan,
    NetworkKind,
    Operation,
    PerTrialView,
    QueryPlan,
    UnsupportedPlan,
)
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.models.response import (
    Answer,
    AppliedFilters,
    ErrorCode,
    ErrorInfo,
    Outcome,
    QueryResponse,
    SourceInfo,
)
from clinical_trials_viz.models.spec import NetworkData, VisualizationSpec, VisualizationType
from clinical_trials_viz.network import drug_drug_network, same_arm_pairs, sponsor_drug_network
from clinical_trials_viz.planner import Planner
from clinical_trials_viz.planning import plan_question
from clinical_trials_viz.render import chart_problem
from clinical_trials_viz.runs import RunStore, new_run_id
from clinical_trials_viz.spec_builder import (
    breakdown_spec,
    build_evidence,
    chart_type_for,
    comparison_spec,
    cross_spec,
    histogram_spec,
    network_spec,
    scatter_spec,
    single_value_spec,
    table_spec,
    timeline_spec,
)
from clinical_trials_viz.validate import GateResult
from clinical_trials_viz.verify import verify

START_FIELD = "protocolSection.statusModule.startDateStruct"
PRIMARY_COMPLETION_FIELD = "protocolSection.statusModule.primaryCompletionDateStruct"
COMPLETION_FIELD = "protocolSection.statusModule.completionDateStruct"
ARM_FIELD = "protocolSection.armsInterventionsModule.armGroups (drug pairs given in the same arm)"
log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


DEFAULT_BASE_URL = "http://localhost:8000"  # chart links when neither the settings nor the caller give one


class RunNotFound(Exception):
    pass


class _Run:
    """Mutable state of one Run."""

    def __init__(
        self, request: QueryRequest, api_requests_before: int, previous_request: QueryRequest | None, base_url: str
    ):
        self.request = request
        self.base_url = base_url  # of chart_url links
        self.previous_request = previous_request  # for Follow-ups: carried over when the plan refines
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


def _finish(target: Answer, outcome: Outcome, message: str | None = None, error: ErrorInfo | None = None) -> None:
    target.outcome = outcome
    target.message = message
    target.error = error


def _fail(target: Answer, failure: Failure) -> None:
    _finish(target, failure.outcome, failure.message, failure.error)


class Pipeline:
    def __init__(self, client: CtGovClient, planner: Planner, runs: RunStore, public_base_url: str | None = None):
        self.client = client
        self.planner = planner
        self.runs = runs
        self.public_base_url = public_base_url
        self._countries: set[str] | None = None

    async def known_countries(self) -> set[str]:
        if self._countries is None:
            try:
                self._countries = set(await self.client.countries())
            except UpstreamError:
                return set()  # validate country names later instead of failing the Run
        return self._countries

    async def run(self, request: QueryRequest, *, base_url: str | None = None) -> QueryResponse:
        """Answer one request. `base_url` is the address it arrived on, for chart links when the
        settings give no public address."""
        previous: QueryPlan | None = None
        previous_request: QueryRequest | None = None
        if request.previous_run_id:
            record = self.runs.load(request.previous_run_id)
            if record is None:
                raise RunNotFound(request.previous_run_id)
            previous, previous_request = record.plan, record.request

        links = (self.public_base_url or base_url or DEFAULT_BASE_URL).rstrip("/")
        run = _Run(request, self.client.requests_made, previous_request, links)
        with tracer.start_as_current_span("run") as span:
            span.set_attribute("run.id", run.response.run_id)
            try:
                await self._execute(run, previous)
            except Exception as exc:  # planner failures, source failures and bugs alike end in one Outcome
                _fail(run.response, classify(exc, run.response.run_id))
            span.set_attribute("run.outcome", run.response.outcome.value)

        response = run.response
        response.model_calls = run.model_calls
        response.timings_ms = run.timings
        api_requests = self.client.requests_made - run.api_requests_before
        for answer in (response, *response.additional_answers):
            if answer.source:
                answer.source.api_requests = api_requests  # shared by all parts of the Run
        try:
            self.runs.save(run.request, response)  # the effective request, so later Follow-ups inherit it too
        except OSError as exc:  # e.g. a full disk: still answer, but nothing can be fetched by run ID later
            log.error("run %s could not be saved: %s", response.run_id, exc)
            response.warnings.append(
                "This answer could not be saved, so its chart image and follow-ups are unavailable."
            )
            for answer in (response, *response.additional_answers):
                answer.chart_url = None
        return response

    async def _execute(self, run: _Run, previous: QueryPlan | None) -> None:
        with run.stage("plan"):
            planning = await plan_question(
                self.planner, run.request, previous, run.previous_request, await self.known_countries()
            )
        run.model_calls += planning.model_calls
        run.request = planning.request
        response = run.response
        response.plan = plan = planning.plan
        response.planner_model = planning.model_name
        response.assumptions.extend(planning.notes)
        response.warnings.extend(planning.warnings)
        if isinstance(plan, AnswerPlan | MultiAnswerPlan):
            response.relation = plan.relation

        parts = plan.parts if isinstance(plan, MultiAnswerPlan) else [plan]
        for index, (part, gate, error) in enumerate(zip(parts, planning.gates, planning.part_errors, strict=True)):
            target = response if index == 0 else Answer(outcome=Outcome.INTERNAL_ERROR, plan=part)
            if index:
                response.additional_answers.append(target)
            if error is not None:  # the model could not plan this part; the other parts still run
                _fail(target, classify(error, response.run_id))
                continue
            if isinstance(part, UnsupportedPlan):
                _finish(target, Outcome.UNSUPPORTED_QUERY, part.reason)
                continue
            if isinstance(part, ClarifyPlan):
                with run.stage("clarify"):
                    target.clarification = await clarify.from_plan(part, self.client, run.request.structured_fields())
                _finish(target, Outcome.CLARIFICATION_REQUIRED, target.clarification.question)
                continue
            assert isinstance(part, AnswerPlan) and gate is not None
            try:
                await self._answer(run, target, part, gate, index)
            except Exception as exc:  # one part failing (source error, too broad, a bug) leaves the others intact
                _fail(target, classify(exc, response.run_id))

    async def _answer(self, run: _Run, target: Answer, plan: AnswerPlan, gate: GateResult, index: int) -> None:
        """Answer one part of the Question into `target`."""
        if gate.unsupported:
            _finish(target, Outcome.UNSUPPORTED_QUERY, gate.unsupported)
            return
        target.applied_filters = filters = gate.filters
        target.assumptions.extend(gate.assumptions)
        version = await self.client.version()
        target.source = SourceInfo(
            api_version=version.api_version, data_timestamp=version.data_timestamp, retrieved_at=datetime.now(UTC)
        )

        if plan.operation is Operation.COMPARE:
            await self._compare(run, target, plan, filters, index)
            return

        with run.stage("retrieve"):
            extra = ARM_DESCRIPTION_FIELDS if plan.network is NetworkKind.DRUG_DRUG else None
            cohort = await fetch_cohort(self.client, filters, extra)
        target.source.search_matches = cohort.search_matches
        target.source.cohort_size = len(cohort.trials)
        target.assumptions.extend(cohort.assumptions)

        if filters.sponsor and not filters.sponsor_exact and filters.sponsor_role == "lead":
            question = clarify.sponsor_ambiguity(cohort.trials, filters.sponsor)
            if question:
                target.clarification = question
                _finish(target, Outcome.CLARIFICATION_REQUIRED, question.question)
                return
        if not cohort.trials:
            _finish(target, Outcome.NO_DATA, "No trials match these filters (all matching pages were retrieved).")
            return

        with run.stage("analyze"):
            spec, dimension = self._build(target, plan, filters, cohort)
        if isinstance(spec.data, NetworkData) and not spec.data.edges:
            _finish(target, Outcome.NO_DATA, "No entities share enough trials to draw a link (all pages retrieved).")
            return
        self._finish_success(run, target, plan, spec, {t.nct_id: t for t in cohort.trials}, dimension, index)

    def _build(
        self, target: Answer, plan: AnswerPlan, filters: AppliedFilters, cohort: Cohort
    ) -> tuple[VisualizationSpec, Dimension | None]:
        if plan.operation is Operation.PER_TRIAL:
            build = {PerTrialView.TIMELINE: timeline_spec, PerTrialView.SCATTER: scatter_spec}.get(
                plan.view or PerTrialView.TABLE, table_spec
            )
            spec, notes = build(cohort.trials, filters)
            target.assumptions.extend(notes)
            return spec, None
        if plan.operation is Operation.BIN:
            histogram = enrollment_histogram(cohort.trials)
            target.assumptions.extend(histogram.assumptions)
            return histogram_spec(histogram, filters, len(cohort.trials)), None
        if plan.operation is Operation.RELATE:
            kind = plan.network or NetworkKind.SPONSOR_DRUG
            build = sponsor_drug_network if kind is NetworkKind.SPONSOR_DRUG else drug_drug_network
            network = build(cohort.trials)
            target.assumptions.extend(network.assumptions)
            return network_spec(network, filters, network.contributing_trials, kind), None
        if plan.group_by is None:
            return single_value_spec(cohort.trials, filters), None
        if plan.series_by is not None:
            cross = cross_breakdown(cohort.trials, plan.group_by, plan.series_by, plan.top_n or DEFAULT_TOP_N)
            target.assumptions.extend(cross.assumptions)
            return cross_spec(cross, filters, len(cohort.trials)), plan.group_by
        result = breakdown(cohort.trials, plan.group_by, plan.top_n or DEFAULT_TOP_N)
        target.assumptions.extend(result.assumptions)
        return breakdown_spec(result, filters, len(cohort.trials)), plan.group_by

    async def _compare(self, run: _Run, target: Answer, plan: AnswerPlan, base: AppliedFilters, index: int) -> None:
        sides: dict[str, list[Trial]] = {}
        assert target.source
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
                target.assumptions.extend(cohort.assumptions)
                target.source.search_matches += cohort.search_matches
        trials = {t.nct_id: t for ts in sides.values() for t in ts}
        target.source.cohort_size = len(trials)
        if not trials:
            _finish(target, Outcome.NO_DATA, "No trials match any of the compared sides.")
            return
        with run.stage("analyze"):
            groups = comparison_groups(sides)
            spec, notes = comparison_spec(groups, plan.group_by, base, list(sides), plan.top_n or DEFAULT_TOP_N)
        target.assumptions.extend(notes)
        self._finish_success(run, target, plan, spec, trials, plan.group_by, index)

    def _finish_success(
        self,
        run: _Run,
        target: Answer,
        plan: AnswerPlan,
        spec: VisualizationSpec,
        trials: dict[str, Trial],
        dimension: Dimension | None,
        index: int,
    ) -> None:
        with run.stage("verify"):
            filters = target.applied_filters or AppliedFilters()
            extra = tuple(_cited_dimensions(plan))
            evidence = build_evidence(spec, trials, dimension, filters, extra)
            if spec.type in (VisualizationType.TIMELINE, VisualizationType.SCATTER_PLOT):
                for nct_id, entry in evidence.items():
                    trial = trials[nct_id]
                    entry.fields[START_FIELD] = {"date": trial.start_date, "type": trial.start_date_type}
                    entry.fields[PRIMARY_COMPLETION_FIELD] = {
                        "date": trial.primary_completion_date,
                        "type": trial.primary_completion_date_type,
                    }
                    entry.fields[COMPLETION_FIELD] = {"date": trial.completion_date, "type": trial.completion_date_type}
            if spec.type in (VisualizationType.HISTOGRAM, VisualizationType.SCATTER_PLOT):
                for nct_id, entry in evidence.items():
                    trial = trials[nct_id]
                    entry.fields[ENROLLMENT_FIELD] = {"count": trial.enrollment, "type": trial.enrollment_type}
            if plan.network is NetworkKind.DRUG_DRUG:
                for nct_id, entry in evidence.items():  # the arms that make each link a combination
                    entry.fields[ARM_FIELD] = {
                        f"{a} + {b}": arms for (a, b), arms in same_arm_pairs(trials[nct_id]).items()
                    }
            verification = verify(
                spec, evidence, trials, dimension, chart_type_for(plan), filters, series=plan.series_by
            )
        target.verification = verification
        if not verification.passed:
            failed = ", ".join(c.name for c in verification.checks if not c.passed)
            log.error("run %s: verification failed (%s)", run.response.run_id, failed)
            _finish(
                target,
                Outcome.INTERNAL_ERROR,
                "The answer failed verification and was withheld.",
                ErrorInfo(code=ErrorCode.VERIFICATION_FAILED, message=f"Failed checks: {failed}", retryable=False),
            )
            return
        target.visualization = spec
        target.evidence = evidence
        if spec.type is not VisualizationType.TABLE:
            # A chart that cannot be drawn does not change the answer: the specification and citations
            # stand; only the image link is withheld, with a warning.
            if problem := chart_problem(spec):
                target.warnings.append(
                    f"The chart image could not be prepared ({problem}); the visualization specification "
                    "and citations are unaffected."
                )
            else:
                part = f"?part={index}" if index else ""
                target.chart_url = f"{run.base_url}/v1/runs/{run.response.run_id}/chart.png{part}"
        _finish(target, Outcome.SUCCESS)


def _cited_dimensions(plan: AnswerPlan) -> list[Dimension]:
    """Fields beyond the filters that place a trial in the answer: its comparison side, the series of
    a crossed chart, or the entities a network links. They are cited too."""
    if plan.series_by is not None:
        return [plan.series_by]
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
