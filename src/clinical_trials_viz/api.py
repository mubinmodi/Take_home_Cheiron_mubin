"""HTTP API (FastAPI)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from redis.asyncio import Redis

from clinical_trials_viz.access import MemoryUserLimiter, UserLimiter, identify
from clinical_trials_viz.breaker import CircuitBreaker
from clinical_trials_viz.config import Settings, get_settings
from clinical_trials_viz.ctgov.client import CtGovClient, MemoryPageCache, RateLimiter
from clinical_trials_viz.idempotency import MAX_KEY_LENGTH, FileIdempotencyStore, KeyInProgress, KeyReused, KeyStore
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.models.response import QueryResponse
from clinical_trials_viz.models.spec import VisualizationSpec
from clinical_trials_viz.pipeline import Pipeline, RunNotFound
from clinical_trials_viz.planner import Planner, build_planner
from clinical_trials_viz.render import NotRenderable, render, to_vega_lite
from clinical_trials_viz.runs import RunRecord, StoreUnavailable, run_store
from clinical_trials_viz.shared_state import RedisIdempotencyStore, RedisPageCache, RedisRateLimiter, RedisUserLimiter
from clinical_trials_viz.telemetry import configure_logging, setup_tracing

WEB_PAGE = Path(__file__).parent / "web" / "index.html"
log = logging.getLogger(__name__)


def http_error(
    status: int, code: str, message: str, retryable: bool = False, headers: dict[str, str] | None = None
) -> HTTPException:
    """HTTP errors share one JSON shape: {"detail": {"code", "message", "retryable"}}."""
    return HTTPException(status, {"code": code, "message": message, "retryable": retryable}, headers)


def create_app(
    settings: Settings | None = None,
    planner: Planner | None = None,
    http: httpx.AsyncClient | None = None,
    redis: Redis | None = None,
) -> FastAPI:
    """Build the app. Tests pass a fake planner, a mocked HTTP client and an in-memory Redis."""
    settings = settings or get_settings()
    users = settings.api_users()  # API key -> user name; empty: no key needed
    configure_logging(settings.log_level)
    setup_tracing(settings.otel_exporter)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client_http = http or httpx.AsyncClient(timeout=settings.ctgov_timeout_seconds)
        # Hosted: state every instance must share lives in Redis. Locally it stays in this process.
        shared = redis or (
            # Short timeouts: an unreachable Redis must fail fast so each part falls back to working locally.
            Redis.from_url(settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2)
            if settings.redis_url
            else None
        )
        per_minute = settings.ctgov_requests_per_minute
        client = CtGovClient(
            client_http,
            settings.ctgov_base_url,
            RedisRateLimiter(shared, per_minute) if shared else RateLimiter(per_minute),
            settings.max_pages,
            RedisPageCache(shared) if shared else MemoryPageCache(),
            CircuitBreaker("ClinicalTrials.gov", settings.breaker_failures, settings.breaker_cooldown_seconds),
        )
        database_url = settings.database_url.get_secret_value() if settings.database_url else None
        runs = run_store(database_url, settings.runs_dir)
        await runs.open()
        app.state.pipeline = Pipeline(
            client,
            planner
            or build_planner(
                settings.planner_primary,
                settings.planner_fallback,
                timeout=settings.planner_timeout_seconds,
                deadline=settings.planner_deadline_seconds,
                breaker_failures=settings.breaker_failures,
                breaker_cooldown=settings.breaker_cooldown_seconds,
            ),
            runs,
            settings.public_base_url,
            settings.run_deadline_seconds,
        )
        app.state.idempotency = (
            RedisIdempotencyStore(shared) if shared else FileIdempotencyStore(settings.runs_dir / "idempotency")
        )
        per_hour = settings.user_queries_per_hour
        app.state.user_limiter = (
            (RedisUserLimiter(shared, per_hour) if shared else MemoryUserLimiter(per_hour)) if per_hour else None
        )
        yield
        await runs.close()
        if shared is not None and redis is None:
            await shared.aclose()
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

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(request: Request, exc: StoreUnavailable) -> JSONResponse:
        log.error("run history unavailable on %s %s: %s", request.method, request.url.path, exc)
        detail = {"code": "store_unavailable", "message": "The run history is unavailable; try again shortly.",
                  "retryable": True}  # fmt: skip
        return JSONResponse(status_code=503, content={"detail": detail})

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Anything not handled above: log it and answer in the same JSON shape as every other error."""
        log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        detail = {"code": "internal", "message": "Unexpected error; see the server log.", "retryable": False}
        return JSONResponse(status_code=500, content={"detail": detail})

    @app.post("/v1/query", response_model=QueryResponse, response_model_exclude_none=True)
    async def query(
        request: QueryRequest,
        response: Response,
        http_request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                max_length=MAX_KEY_LENGTH,
                description="Optional. Retrying with the same key and request returns the original response "
                "without running again; the same key with a different request is rejected (422).",
            ),
        ] = None,
        api_key: Annotated[
            str | None,
            Header(alias="X-API-Key", description="Required when the service is configured with API keys (hosted)."),
        ] = None,
    ) -> QueryResponse:
        user = identify(api_key, users)
        if users and user is None:
            raise http_error(401, "unauthorized", "Send a valid API key in the X-API-Key header.")
        keys: KeyStore = app.state.idempotency
        if idempotency_key:
            try:
                replay = await keys.begin(idempotency_key, request)
            except KeyReused as exc:
                raise http_error(
                    422, "idempotency_key_reused", "Idempotency-Key was already used for a different request"
                ) from exc
            except KeyInProgress as exc:
                raise http_error(
                    409,
                    "request_in_progress",
                    "A request with this Idempotency-Key is still running; retry shortly",
                    True,
                ) from exc
            if replay is not None and (record := await pipeline().runs.load(replay)) is not None:
                response.headers["Idempotent-Replayed"] = "true"
                return record.response  # a replay is free: it does not count against the user's limit
        limiter: UserLimiter | None = app.state.user_limiter
        if limiter is not None and (wait := await limiter.spend(user or "anonymous")) is not None:
            if idempotency_key:
                await keys.abandon(idempotency_key)
            minutes = max(1, round(wait / 60))
            raise http_error(
                429,
                "rate_limited",
                f"You have asked {settings.user_queries_per_hour} questions this hour; try again in about "
                f"{minutes} minute{'s' if minutes != 1 else ''}.",
                True,
                {"Retry-After": str(int(wait) + 1)},
            )
        try:
            result = await pipeline().run(request, base_url=str(http_request.base_url), user=user)
        except RunNotFound as exc:
            if idempotency_key:
                await keys.abandon(idempotency_key)
            raise http_error(404, "run_not_found", f"previous_run_id {exc} not found") from exc
        except BaseException:
            if idempotency_key:
                await keys.abandon(idempotency_key)
            raise
        if idempotency_key:
            await keys.finish(idempotency_key, request, result.run_id)
        return result

    @app.get("/v1/runs/{run_id}", response_model=RunRecord, response_model_exclude_none=True)
    async def get_run(run_id: str) -> RunRecord:
        record = await pipeline().runs.load(run_id)
        if record is None:
            raise http_error(404, "run_not_found", "run not found")
        return record

    async def visualization(run_id: str, part: int) -> VisualizationSpec:
        """The chart of one part of a run: 0 is the top-level answer, 1+ the additional answers."""
        record = await pipeline().runs.load(run_id)
        answers = [record.response, *record.response.additional_answers] if record else []
        spec = answers[part].visualization if 0 <= part < len(answers) else None
        if spec is None:
            raise http_error(404, "no_visualization", "no visualization for this run and part")
        return spec

    @app.get("/v1/runs/{run_id}/chart.{fmt}")
    async def get_chart(run_id: str, fmt: Literal["png", "svg"], part: int = 0) -> Response:
        spec = await visualization(run_id, part)
        limit = settings.render_timeout_seconds
        try:
            # Drawing is CPU work: run it off the event loop, with a time limit.
            image = await asyncio.wait_for(run_in_threadpool(render, spec, fmt), limit)
        except NotRenderable as exc:
            raise http_error(404, "not_renderable", str(exc)) from exc
        except TimeoutError as exc:
            log.warning("run %s part %d: drawing the chart took longer than %g s", run_id, part, limit)
            raise http_error(504, "render_timeout", f"Drawing the chart took longer than {limit:g} s.", True) from exc
        except Exception as exc:
            log.error("run %s part %d: drawing the chart failed", run_id, part, exc_info=exc)
            raise http_error(
                500,
                "render_failed",
                f"The chart could not be drawn ({type(exc).__name__}); the visualization specification and "
                "citations in the run are unaffected.",
            ) from exc
        return Response(image, media_type="image/png" if fmt == "png" else "image/svg+xml")

    @app.get("/v1/runs/{run_id}/vega-lite.json")
    async def get_vega_lite(run_id: str, part: int = 0) -> dict[str, Any]:
        """The chart as a Vega-Lite spec (finished values only); each mark carries `_datum`, its index
        in the visualization's Datums, so a client can show that Datum's Citation on click."""
        spec = await visualization(run_id, part)
        try:
            return to_vega_lite(spec)
        except NotRenderable as exc:
            raise http_error(404, "not_renderable", str(exc)) from exc
        except Exception as exc:
            log.error("run %s part %d: Vega-Lite translation failed", run_id, part, exc_info=exc)
            raise http_error(500, "render_failed", f"The chart could not be prepared ({type(exc).__name__}).") from exc

    @app.get("/", include_in_schema=False)
    async def page() -> FileResponse:
        # Revalidate on every load (a 304 when unchanged): otherwise browsers keep showing the previous
        # page for a while after a release.
        return FileResponse(WEB_PAGE, media_type="text/html", headers={"Cache-Control": "no-cache"})

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
