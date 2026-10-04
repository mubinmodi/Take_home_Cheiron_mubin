"""Hosted mode (docs/hosted-deployment.md): settings, shared state, access control, circuit breakers
and the run deadline. The local mode must keep working with none of it configured."""

import asyncio

import fakeredis
import pytest
from pydantic import SecretStr, ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from clinical_trials_viz.catalog import Dimension
from clinical_trials_viz.config import HOSTED_QUERIES_PER_HOUR, HOSTED_RUN_DEADLINE_SECONDS, Settings
from clinical_trials_viz.idempotency import KeyInProgress, KeyReused
from clinical_trials_viz.models.plan import AnswerPlan, Filters, Operation
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.runs import async_database_url, runs_table
from clinical_trials_viz.shared_state import RedisIdempotencyStore, RedisRateLimiter
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


# --- Shared state in Redis --------------------------------------------------------------------------

NOW_MS = 1_759_500_000_000.0  # a real epoch in milliseconds, so the Lua script's number handling is exercised


async def test_two_instances_share_one_clinicaltrials_budget():
    redis = fakeredis.FakeAsyncRedis()
    first, second = RedisRateLimiter(redis, per_minute=6), RedisRateLimiter(redis, per_minute=6)
    waits = [await limiter.reserve(NOW_MS) for limiter in (first, second) * 3]
    assert waits == [0] * 6  # a burst of up to 6, whichever instance asks
    assert await first.reserve(NOW_MS) == pytest.approx(10_000)  # the 7th waits one interval (60 s / 6)
    assert await second.reserve(NOW_MS + 10_000) == 0


async def test_a_page_fetched_by_one_instance_serves_the_others(make_client, ctgov):
    redis = fakeredis.FakeAsyncRedis()
    pages_fetched = []
    for _ in range(2):  # two instances, one Redis
        async for client in make_client(ScriptedPlanner(TREND), redis=redis):
            body = (await client.post("/v1/query", json={"query": "Keytruda trials per year"})).json()
            assert body["outcome"] == "success"
        pages_fetched.append(sum(call.request.url.path.endswith("/studies") for call in ctgov.calls))
    assert pages_fetched[0] >= 1 and pages_fetched[1] == pages_fetched[0]  # the second instance fetched none


async def test_idempotency_keys_hold_across_instances():
    redis = fakeredis.FakeAsyncRedis()
    one, other = RedisIdempotencyStore(redis), RedisIdempotencyStore(redis)
    request = QueryRequest(query="Keytruda trials per year")
    assert await one.begin("key-1", request) is None  # first use: run it
    with pytest.raises(KeyInProgress):
        await other.begin("key-1", request)
    await one.finish("key-1", request, "run_" + "a" * 32)
    assert await other.begin("key-1", request) == "run_" + "a" * 32  # replayed on another instance
    with pytest.raises(KeyReused):
        await other.begin("key-1", QueryRequest(query="something else"))
    assert await one.begin("key-2", request) is None
    await one.abandon("key-2")  # failed before producing a run: the key is free again
    assert await other.begin("key-2", request) is None


async def test_a_replay_through_another_instance_returns_the_original_answer(make_client):
    redis = fakeredis.FakeAsyncRedis()
    body, headers = {"query": "Keytruda trials per year"}, {"Idempotency-Key": "retry-7"}
    answers = []
    for _ in range(2):
        async for client in make_client(ScriptedPlanner(TREND), redis=redis):
            answers.append(await client.post("/v1/query", json=body, headers=headers))
    assert answers[1].json() == answers[0].json() and answers[1].headers["Idempotent-Replayed"] == "true"


async def test_a_redis_outage_degrades_instead_of_failing_questions(make_client, caplog):
    unreachable = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    async for client in make_client(ScriptedPlanner(TREND, TREND), redis=unreachable):
        headers = {"Idempotency-Key": "during-outage"}
        first = await client.post("/v1/query", json={"query": "Keytruda trials per year"}, headers=headers)
        again = await client.post("/v1/query", json={"query": "Keytruda trials per year"}, headers=headers)
    assert first.json()["outcome"] == again.json()["outcome"] == "success"
    assert "Redis unavailable for the ClinicalTrials.gov rate limit" in caplog.text
    assert "Redis unavailable for Idempotency-Keys" in caplog.text


async def test_api_requests_are_counted_once_by_the_run_that_made_them(make_client, ctgov):
    plans = (TREND, TREND.model_copy(update={"filters": Filters(drugs=["pembrolizumab"])}))
    async for client in make_client(ScriptedPlanner(*plans)):
        replies = await asyncio.gather(*(client.post("/v1/query", json={"query": f"question {i}"}) for i in range(2)))
    counts = [reply.json()["source"]["api_requests"] for reply in replies]
    # A shared counter would credit each run with the other's requests too, so the sum would exceed the calls.
    assert all(counts) and sum(counts) == len(ctgov.calls)
