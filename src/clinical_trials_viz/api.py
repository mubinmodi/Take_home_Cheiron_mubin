"""HTTP API (FastAPI)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse

from clinical_trials_viz.config import Settings, get_settings
from clinical_trials_viz.ctgov.client import CtGovClient
from clinical_trials_viz.idempotency import MAX_KEY_LENGTH, IdempotencyStore, KeyInProgress, KeyReused
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.models.response import QueryResponse
from clinical_trials_viz.models.spec import VisualizationSpec
from clinical_trials_viz.pipeline import Pipeline, RunNotFound
from clinical_trials_viz.planner import Planner, build_planner
from clinical_trials_viz.render import NotRenderable, render, to_vega_lite
from clinical_trials_viz.runs import RunRecord, RunStore
from clinical_trials_viz.telemetry import setup_tracing

WEB_PAGE = Path(__file__).parent / "web" / "index.html"


def create_app(
    settings: Settings | None = None, planner: Planner | None = None, http: httpx.AsyncClient | None = None
) -> FastAPI:
    """Build the app. Tests pass a fake planner and a mocked HTTP client."""
    settings = settings or get_settings()
    setup_tracing(settings.otel_exporter)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client_http = http or httpx.AsyncClient(timeout=settings.ctgov_timeout_seconds)
        client = CtGovClient(
            client_http, settings.ctgov_base_url, settings.ctgov_requests_per_minute, settings.max_pages
        )
        app.state.pipeline = Pipeline(
            client,
            planner or build_planner(settings.planner_primary, settings.planner_fallback),
            RunStore(settings.runs_dir),
            settings.public_base_url,
        )
        app.state.idempotency = IdempotencyStore(settings.runs_dir / "idempotency")
        yield
        if http is None:
            await client_http.aclose()

    app = FastAPI(
        title="Clinical Trials Question-to-Visualization",
        version="0.1.0",
        description="Answers questions about clinical trials with a cited visualization specification, "
        "using ClinicalTrials.gov API v2 as the only source of values.",
        lifespan=lifespan,
    )
    if settings.otel_exporter != "none":
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)

    def pipeline() -> Pipeline:
        return app.state.pipeline

    @app.post("/v1/query", response_model=QueryResponse, response_model_exclude_none=True)
    async def query(
        request: QueryRequest,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                max_length=MAX_KEY_LENGTH,
                description="Optional. Retrying with the same key and request returns the original response "
                "without running again; the same key with a different request is rejected (422).",
            ),
        ] = None,
    ) -> QueryResponse:
        keys: IdempotencyStore = app.state.idempotency
        if idempotency_key:
            try:
                replay = await keys.begin(idempotency_key, request)
            except KeyReused as exc:
                raise HTTPException(422, "Idempotency-Key was already used for a different request") from exc
            except KeyInProgress as exc:
                raise HTTPException(409, "A request with this Idempotency-Key is still running; retry shortly") from exc
            if replay is not None and (record := pipeline().runs.load(replay)) is not None:
                response.headers["Idempotent-Replayed"] = "true"
                return record.response
        try:
            result = await pipeline().run(request)
        except RunNotFound as exc:
            if idempotency_key:
                keys.abandon(idempotency_key)
            raise HTTPException(404, f"previous_run_id {exc} not found") from exc
        except BaseException:
            if idempotency_key:
                keys.abandon(idempotency_key)
            raise
        if idempotency_key:
            keys.finish(idempotency_key, request, result.run_id)
        return result

    @app.get("/v1/runs/{run_id}", response_model=RunRecord, response_model_exclude_none=True)
    async def get_run(run_id: str) -> RunRecord:
        record = pipeline().runs.load(run_id)
        if record is None:
            raise HTTPException(404, "run not found")
        return record

    def visualization(run_id: str, part: int) -> VisualizationSpec:
        """The chart of one part of a run: 0 is the top-level answer, 1+ the additional answers."""
        record = pipeline().runs.load(run_id)
        answers = [record.response, *record.response.additional_answers] if record else []
        spec = answers[part].visualization if 0 <= part < len(answers) else None
        if spec is None:
            raise HTTPException(404, "no visualization for this run and part")
        return spec

    @app.get("/v1/runs/{run_id}/chart.{fmt}")
    async def get_chart(run_id: str, fmt: Literal["png", "svg"], part: int = 0) -> Response:
        spec = visualization(run_id, part)
        try:
            image = render(spec, fmt)
        except NotRenderable as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(image, media_type="image/png" if fmt == "png" else "image/svg+xml")

    @app.get("/v1/runs/{run_id}/vega-lite.json")
    async def get_vega_lite(run_id: str, part: int = 0) -> dict[str, Any]:
        """The chart as a Vega-Lite spec (finished values only); each mark carries `_datum`, its index
        in the visualization's Datums, so a client can show that Datum's Citation on click."""
        spec = visualization(run_id, part)
        try:
            return to_vega_lite(spec)
        except NotRenderable as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/", include_in_schema=False)
    async def page() -> FileResponse:
        return FileResponse(WEB_PAGE, media_type="text/html")

    @app.get("/v1/schema")
    async def schema() -> dict[str, Any]:
        return {
            "request": QueryRequest.model_json_schema(),
            "response": QueryResponse.model_json_schema(mode="serialization"),
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
