"""Hosted mode (docs/hosted-deployment.md): settings, shared state, access control, circuit breakers
and the run deadline. The local mode must keep working with none of it configured."""

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from clinical_trials_viz.catalog import Dimension
from clinical_trials_viz.config import HOSTED_QUERIES_PER_HOUR, HOSTED_RUN_DEADLINE_SECONDS, Settings
from clinical_trials_viz.models.plan import AnswerPlan, Filters, Operation
from clinical_trials_viz.runs import async_database_url, runs_table
from tests.conftest import ScriptedPlanner, ask

TREND = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(drugs=["Keytruda"]), group_by=Dimension.START_YEAR)
KEY = "k" * 24


def local(**values) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def hosted(**values) -> Settings:
    return local(deployment="hosted", **values)


# --- Settings ---------------------------------------------------------------------------------------


def test_the_local_mode_needs_no_infrastructure():
    settings = local()
    assert settings.deployment == "local"
    assert settings.redis_url is None and settings.database_url is None and settings.api_users() == {}
    assert settings.user_queries_per_hour is None and settings.run_deadline_seconds is None


def test_hosted_mode_refuses_to_start_and_names_every_missing_setting():
    with pytest.raises(ValidationError) as info:
        hosted()
    assert "DEPLOYMENT=hosted needs REDIS_URL, DATABASE_URL, API_KEYS" in str(info.value)


def test_hosted_mode_defaults_the_per_user_limit_and_the_deadline():
    settings = hosted(redis_url="redis://cache", database_url="postgresql+asyncpg://db", api_keys=f"alice:{KEY}")
    assert settings.user_queries_per_hour == HOSTED_QUERIES_PER_HOUR
    assert settings.run_deadline_seconds == HOSTED_RUN_DEADLINE_SECONDS
    assert settings.api_users() == {KEY: "alice"}


def test_a_malformed_api_key_entry_is_rejected_without_quoting_any_key():
    with pytest.raises(ValidationError) as info:
        local(api_keys=f"alice:{KEY}, bob:too-short")
    assert "API_KEYS entry 2 must be name:key" in str(info.value)
    assert "too-short" not in str(info.value) and KEY not in str(info.value)


def test_connection_strings_and_keys_stay_out_of_reprs():
    settings = local(redis_url="redis://:hunter2@cache", api_keys=f"alice:{KEY}")
    assert "hunter2" not in repr(settings) and KEY not in repr(settings)


async def test_chart_links_use_the_public_address_when_one_is_set(make_client, settings):
    settings.public_base_url = "https://trials.example.org"
    body = await ask(make_client, ScriptedPlanner(TREND), query="Keytruda trials per year")
    assert body["chart_url"] == f"https://trials.example.org/v1/runs/{body['run_id']}/chart.png"


# --- Run history in SQL (Postgres when hosted; SQLite here) -----------------------------------------


def test_provider_connection_strings_become_asyncpg_urls():
    url = async_database_url("postgres://user:pw@host.neon.tech/db?sslmode=require&channel_binding=require")
    assert url == "postgresql+asyncpg://user:pw@host.neon.tech/db?ssl=require"
    assert async_database_url("sqlite+aiosqlite:///runs.db") == "sqlite+aiosqlite:///runs.db"


async def test_follow_ups_and_charts_work_from_the_sql_run_history(make_client, settings, tmp_path):
    database = tmp_path / "runs.db"
    settings.database_url = SecretStr(f"sqlite+aiosqlite:///{database}")
    by_phase = TREND.model_copy(update={"group_by": Dimension.PHASE})
    async for client in make_client(ScriptedPlanner(TREND, by_phase)):
        first = (await client.post("/v1/query", json={"query": "Keytruda trials per year"})).json()
        follow_up = await client.post(
            "/v1/query", json={"query": "by phase instead", "previous_run_id": first["run_id"]}
        )
        assert follow_up.status_code == 200 and follow_up.json()["outcome"] == "success"
        assert (await client.get(f"/v1/runs/{first['run_id']}")).json()["plan"]["group_by"] == "start_year"
        assert (await client.get(first["chart_url"])).content.startswith(b"\x89PNG")

    assert not list(settings.runs_dir.glob("run_*.json"))  # nothing written to local files
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.connect() as conn:
        rows = (await conn.execute(select(runs_table).order_by(runs_table.c.created_at))).mappings().all()
    await engine.dispose()
    assert [r["question"] for r in rows] == ["Keytruda trials per year", "by phase instead"]
    assert all(r["outcome"] == "success" and r["model_calls"] == 1 and r["latency_ms"] > 0 for r in rows)


async def test_an_unreachable_run_history_still_answers_and_reports_503(make_client, settings, tmp_path):
    settings.database_url = SecretStr(f"sqlite+aiosqlite:///{tmp_path}/missing-directory/runs.db")
    async for client in make_client(ScriptedPlanner(TREND)):
        body = (await client.post("/v1/query", json={"query": "Keytruda trials per year"})).json()
        assert body["outcome"] == "success" and "chart_url" not in body
        assert any("could not be saved" in w for w in body["warnings"])
        response = await client.get(f"/v1/runs/{body['run_id']}")
        assert response.status_code == 503
        assert response.json()["detail"] == {
            "code": "store_unavailable",
            "message": "The run history is unavailable; try again shortly.",
            "retryable": True,
        }
