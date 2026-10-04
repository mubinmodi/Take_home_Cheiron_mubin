"""Run records: the Query Plan and response of each Run.

Enough for Follow-ups (`previous_run_id`) and `chart_url`. Locally each Run is one small JSON file;
the hosted version keeps run history in Postgres (`DATABASE_URL`), shared by every instance. Full
run bundles with raw API responses (for replay) are deferred.
"""

import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import JSON, Column, DateTime, Float, Integer, MetaData, String, Table, Text, insert, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from clinical_trials_viz.models.plan import AnswerPlan, ClarifyPlan, MultiAnswerPlan, UnsupportedPlan
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.models.response import QueryResponse

_RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")
log = logging.getLogger(__name__)


class RunRecord(BaseModel):
    run_id: str
    created_at: datetime
    request: QueryRequest
    plan: AnswerPlan | MultiAnswerPlan | ClarifyPlan | UnsupportedPlan | None
    response: QueryResponse


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def _record(request: QueryRequest, response: QueryResponse) -> RunRecord:
    return RunRecord(
        run_id=response.run_id, created_at=datetime.now(UTC), request=request, plan=response.plan, response=response
    )


class StoreUnavailable(Exception):
    """The run history cannot be read or written (full disk, database down)."""


class RunStore(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def save(self, request: QueryRequest, response: QueryResponse, user: str | None = None) -> None: ...

    async def load(self, run_id: str) -> RunRecord | None:
        """The saved Run, or None when it does not exist or cannot be parsed (treated as not found).
        Raises StoreUnavailable when the store itself cannot be reached."""
        ...


class FileRunStore:
    """One JSON file per Run (the local build)."""

    def __init__(self, directory: Path):
        self._dir = directory

    def _path(self, run_id: str) -> Path | None:
        return self._dir / f"{run_id}.json" if _RUN_ID.match(run_id) else None

    async def open(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        pass

    async def save(self, request: QueryRequest, response: QueryResponse, user: str | None = None) -> None:
        path = self._path(response.run_id)
        assert path is not None
        try:
            path.write_text(_record(request, response).model_dump_json(indent=2))
        except OSError as exc:
            raise StoreUnavailable(str(exc)) from exc

    async def load(self, run_id: str) -> RunRecord | None:
        path = self._path(run_id)
        if path is None or not path.exists():
            return None
        try:
            return RunRecord.model_validate_json(path.read_text())
        except (OSError, ValueError) as exc:  # unreadable or corrupted file (pydantic errors are ValueErrors)
            log.warning("run record %s could not be read: %s", run_id, exc)
            return None


_metadata = MetaData()
runs_table = Table(
    "runs",
    _metadata,
    Column("run_id", String(40), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("user_name", String(100), index=True),  # the API-key user (hosted); never returned by the API
    Column("outcome", String(40), nullable=False),
    Column("model_calls", Integer, nullable=False),
    Column("planner_model", String(100)),
    Column("latency_ms", Float, nullable=False),
    Column("question", Text, nullable=False),
    Column("record", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
)


def async_database_url(url: str) -> str:
    """A SQLAlchemy async URL from a connection string as providers print it: `postgres://` and
    `postgresql://` use asyncpg, which spells libpq's `sslmode` as `ssl` and has no `channel_binding`."""
    parsed = make_url(url)
    if parsed.drivername in ("postgres", "postgresql", "postgresql+asyncpg"):
        query = dict(parsed.query)
        if "sslmode" in query:
            query["ssl"] = query.pop("sslmode")
        query.pop("channel_binding", None)
        parsed = parsed.set(drivername="postgresql+asyncpg", query=query)
    return parsed.render_as_string(hide_password=False)


class SqlRunStore:
    """Run history in a SQL database: Postgres when hosted (any instance can load any Run), SQLite in tests."""

    def __init__(self, url: str):
        self._engine: AsyncEngine = create_async_engine(async_database_url(url), pool_pre_ping=True)
        self._ready = False

    async def open(self) -> None:
        """Create the table if needed. A database that is down at startup is retried on first use."""
        try:
            await self._create()
        except (SQLAlchemyError, OSError) as exc:
            log.error("run history database unavailable at startup: %s", type(exc).__name__)

    async def _create(self) -> None:
        if self._ready:
            return
        async with self._engine.begin() as conn:
            await conn.execute(CreateTable(runs_table, if_not_exists=True))
            for index in runs_table.indexes:
                await conn.execute(CreateIndex(index, if_not_exists=True))
        self._ready = True

    async def close(self) -> None:
        await self._engine.dispose()

    async def save(self, request: QueryRequest, response: QueryResponse, user: str | None = None) -> None:
        record = _record(request, response)
        row = {
            "run_id": record.run_id,
            "created_at": record.created_at,
            "user_name": user,
            "outcome": response.outcome.value,
            "model_calls": response.model_calls,
            "planner_model": response.planner_model,
            "latency_ms": round(sum(response.timings_ms.values()), 1),
            "question": request.query,
            "record": record.model_dump(mode="json"),
        }
        try:
            await self._create()
            async with self._engine.begin() as conn:
                await conn.execute(insert(runs_table).values(**row))
        except (SQLAlchemyError, OSError) as exc:
            raise StoreUnavailable(type(exc).__name__) from exc

    async def load(self, run_id: str) -> RunRecord | None:
        if not _RUN_ID.match(run_id):
            return None
        try:
            await self._create()
            async with self._engine.connect() as conn:
                stored = (await conn.execute(select(runs_table.c.record).where(runs_table.c.run_id == run_id))).scalar()
        except (SQLAlchemyError, OSError) as exc:
            raise StoreUnavailable(type(exc).__name__) from exc
        if stored is None:
            return None
        try:
            return RunRecord.model_validate(stored)
        except ValueError as exc:
            log.warning("run record %s could not be read: %s", run_id, exc)
            return None


def run_store(database_url: str | None, runs_dir: Path) -> RunStore:
    return SqlRunStore(database_url) if database_url else FileRunStore(runs_dir)
