"""Hosted mode (docs/hosted-deployment.md): settings, shared state, access control, circuit breakers
and the run deadline. The local mode must keep working with none of it configured."""

import asyncio
import time

import fakeredis
import httpx
import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from clinical_trials_viz import pipeline as pipeline_module
from clinical_trials_viz import runs as runs_module
from clinical_trials_viz.access import identify
from clinical_trials_viz.breaker import CircuitBreaker, CircuitOpen
from clinical_trials_viz.catalog import Dimension
from clinical_trials_viz.config import HOSTED_QUERIES_PER_HOUR, HOSTED_RUN_DEADLINE_SECONDS, Settings
from clinical_trials_viz.ctgov import client as ctgov_client
from clinical_trials_viz.idempotency import KeyInProgress, KeyReused
from clinical_trials_viz.models.plan import AnswerPlan, Filters, Operation
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.planner import LLMPlanner
from clinical_trials_viz.runs import SqlRunStore, async_database_url, runs_table
from clinical_trials_viz.shared_state import RedisIdempotencyStore, RedisRateLimiter, RedisUserLimiter
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


def test_open_access_runs_hosted_without_keys_or_an_hourly_limit():
    settings = hosted(redis_url="redis://cache", database_url="postgresql+asyncpg://db", open_access=True)
    assert settings.api_users() == {} and settings.user_queries_per_hour is None
    assert settings.run_deadline_seconds == HOSTED_RUN_DEADLINE_SECONDS


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


# --- API keys and the per-user limit ----------------------------------------------------------------

ALICE, BOB = "a" * 20, "b" * 20
QUESTION = {"query": "Keytruda trials per year"}


async def test_with_api_keys_a_question_needs_a_valid_key(make_client, settings, tmp_path, caplog):
    caplog.set_level("DEBUG")
    settings.api_keys = SecretStr(f"alice:{ALICE},bob:{BOB}")
    settings.database_url = SecretStr(f"sqlite+aiosqlite:///{tmp_path}/runs.db")
    async for client in make_client(ScriptedPlanner(TREND)):
        for headers in ({}, {"X-API-Key": "not-a-key"}):
            refused = await client.post("/v1/query", json=QUESTION, headers=headers)
            assert refused.status_code == 401
            assert refused.json()["detail"]["code"] == "unauthorized" and not refused.json()["detail"]["retryable"]
        answered = await client.post("/v1/query", json=QUESTION, headers={"X-API-Key": ALICE})
        assert answered.status_code == 200
        assert (await client.get(f"/v1/runs/{answered.json()['run_id']}")).status_code == 200  # reads stay open

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/runs.db")
    async with engine.connect() as conn:
        assert (await conn.execute(select(runs_table.c.user_name))).scalars().all() == ["alice"]
    await engine.dispose()
    assert ALICE not in caplog.text and "not-a-key" not in caplog.text


async def test_each_user_has_an_hourly_question_limit(make_client, settings):
    settings.api_keys = SecretStr(f"alice:{ALICE},bob:{BOB}")
    settings.user_queries_per_hour = 2
    async for client in make_client(ScriptedPlanner(TREND, TREND, TREND)):
        alice = {"X-API-Key": ALICE}
        assert [(await client.post("/v1/query", json=QUESTION, headers=alice)).status_code for _ in "12"] == [200, 200]
        over = await client.post("/v1/query", json=QUESTION, headers=alice)
        assert over.status_code == 429
        assert over.json()["detail"]["code"] == "rate_limited" and over.json()["detail"]["retryable"]
        assert 0 < int(over.headers["Retry-After"]) <= 3600
        assert (await client.post("/v1/query", json=QUESTION, headers={"X-API-Key": BOB})).status_code == 200


async def test_a_replay_does_not_count_against_the_limit(make_client, settings):
    settings.user_queries_per_hour = 1
    async for client in make_client(ScriptedPlanner(TREND)):
        once = {"Idempotency-Key": "once"}
        first = await client.post("/v1/query", json=QUESTION, headers=once)
        again = await client.post("/v1/query", json=QUESTION, headers=once)
        assert first.status_code == again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
        assert (await client.post("/v1/query", json=QUESTION)).status_code == 429  # a new question is over


async def test_with_open_access_anyone_can_ask_even_with_a_stale_key(make_client, settings):
    settings.api_keys = SecretStr(f"alice:{ALICE}")
    settings.open_access = True
    async for client in make_client(ScriptedPlanner(TREND, TREND)):
        assert (await client.post("/v1/query", json=QUESTION)).status_code == 200
        stale = await client.post("/v1/query", json=QUESTION, headers={"X-API-Key": "an-old-key-from-last-week"})
        assert stale.status_code == 200


async def test_idempotency_keys_are_scoped_per_user(make_client, settings):
    settings.api_keys = SecretStr(f"alice:{ALICE},bob:{BOB}")
    settings.user_queries_per_hour = 1
    async for client in make_client(ScriptedPlanner(TREND, TREND)):
        same_key = {"Idempotency-Key": "shared-7"}
        alice = await client.post("/v1/query", json=QUESTION, headers={**same_key, "X-API-Key": ALICE})
        bob = await client.post("/v1/query", json=QUESTION, headers={**same_key, "X-API-Key": BOB})
        assert alice.status_code == bob.status_code == 200
        assert "Idempotent-Replayed" not in bob.headers  # Bob's request runs, and counts against Bob
        assert alice.json()["run_id"] != bob.json()["run_id"]
        again = await client.post("/v1/query", json=QUESTION, headers={"X-API-Key": BOB})
        assert again.status_code == 429


async def test_user_limits_hold_across_instances():
    redis = fakeredis.FakeAsyncRedis()
    one, other = RedisUserLimiter(redis, per_hour=2), RedisUserLimiter(redis, per_hour=2)
    assert [await one.spend("alice"), await other.spend("alice")] == [None, None]
    wait = await one.spend("alice")  # the third question this hour, whichever instance takes it
    assert wait is not None and 0 < wait <= 3600
    assert await other.spend("bob") is None


# --- Circuit breakers -------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_breaker_opens_after_repeated_failures_and_lets_one_trial_through():
    clock = Clock()
    breaker = CircuitBreaker("source", failures=2, cooldown=30, clock=clock)
    breaker.failure()
    breaker.check()  # one failure: still closed
    breaker.failure()
    with pytest.raises(CircuitOpen) as info:
        breaker.check()
    assert info.value.retry_in == 30
    clock.now = 30
    breaker.check()  # the cool-down is over: one trial call
    with pytest.raises(CircuitOpen):
        breaker.check()  # the others wait for it
    breaker.failure()  # the trial failed: another full cool-down
    clock.now = 45
    with pytest.raises(CircuitOpen) as info:
        breaker.check()
    assert info.value.retry_in == 15
    clock.now = 60
    breaker.check()
    breaker.success()  # the trial succeeded: closed
    breaker.check()
    breaker.check()


def test_a_trial_that_ends_without_an_answer_frees_its_slot():
    clock = Clock()
    breaker = CircuitBreaker("source", failures=1, cooldown=10, clock=clock)
    breaker.failure()
    clock.now = 10
    breaker.check()
    breaker.release()  # e.g. the run was cancelled mid-call
    breaker.check()  # the next call may try instead


@pytest.fixture
def instant_retries(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(ctgov_client.asyncio, "sleep", instant)


async def test_clinicaltrials_failing_repeatedly_fails_fast(make_client, ctgov, settings, instant_retries):
    settings.breaker_failures = 2
    studies = ctgov.get("/studies").mock(return_value=httpx.Response(503, text="maintenance"))
    async for client in make_client(ScriptedPlanner(TREND, TREND, TREND)):
        for _ in range(2):
            body = (await client.post("/v1/query", json=QUESTION)).json()
            assert body["error"]["code"] == "source_unavailable" and "HTTP 503" in body["message"]
        calls = studies.call_count
        body = (await client.post("/v1/query", json=QUESTION)).json()
    assert studies.call_count == calls  # not called again: the breaker is open
    assert body["error"]["code"] == "source_unavailable" and body["error"]["retryable"]
    assert "paused for 30 s" in body["message"]


async def test_a_model_failing_repeatedly_is_skipped_until_its_cool_down_ends(make_client):
    calls: list[str] = []

    def outage(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append("primary")
        raise ModelHTTPError(503, "primary-model")

    def plan(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        args = {"operation": "aggregate", "filters": {"drugs": ["Keytruda"]}, "group_by": "start_year"}
        return ModelResponse(parts=[ToolCallPart("answer_plan", args)], model_name="fallback-model")

    planner = LLMPlanner(
        FunctionModel(outage, model_name="primary-model"),
        FunctionModel(plan, model_name="fallback-model"),
        breaker_failures=1,
        breaker_cooldown=60,
    )
    async for client in make_client(planner):
        first = (await client.post("/v1/query", json=QUESTION)).json()
        second = (await client.post("/v1/query", json=QUESTION)).json()
    assert first["outcome"] == second["outcome"] == "success"
    assert calls == ["primary"]  # the second question went straight to the fallback
    assert any("primary-model: not called, paused for 60 s" in w for w in second["warnings"])


async def test_a_rejected_key_never_opens_a_breaker(make_client):
    calls: list[str] = []

    def bad_key(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append("primary")
        raise ModelHTTPError(401, "primary-model")

    planner = LLMPlanner(FunctionModel(bad_key, model_name="primary-model"), breaker_failures=1)
    async for client in make_client(planner):
        for _ in range(2):
            body = (await client.post("/v1/query", json=QUESTION)).json()
            assert body["error"]["code"] == "planner_rejected"  # the real cause, every time
    assert calls == ["primary", "primary"]


# --- Run deadline -----------------------------------------------------------------------------------

HISTOGRAM = AnswerPlan(operation=Operation.BIN, filters=Filters(drugs=["pembrolizumab"]))
TWO_PARTS = "Show Keytruda trials per year, and show the enrollment distribution of pembrolizumab trials"


async def test_a_run_past_its_deadline_ends_as_run_timeout_and_is_recorded(make_client, settings):
    settings.run_deadline_seconds = 0.2

    async def hang(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    started = time.perf_counter()
    async for client in make_client(LLMPlanner(FunctionModel(hang, model_name="slow-model"))):
        body = (await client.post("/v1/query", json=QUESTION)).json()
        assert (await client.get(f"/v1/runs/{body['run_id']}")).status_code == 200
    assert time.perf_counter() - started < 2
    assert body["outcome"] == "upstream_error"
    assert body["error"]["code"] == "run_timeout" and body["error"]["retryable"]
    assert "longer than 0.2 s and was stopped during planning" in body["message"]


async def test_parts_answered_before_the_deadline_stand(make_client, settings, ctgov, page):
    settings.run_deadline_seconds = 0.5

    async def slow_for_pembrolizumab(request: httpx.Request) -> httpx.Response:
        if "pembrolizumab" in str(request.url).lower():
            await asyncio.sleep(5)
        return httpx.Response(200, json=page)

    ctgov.get("/studies").mock(side_effect=slow_for_pembrolizumab)
    body = await ask(make_client, ScriptedPlanner(TREND, HISTOGRAM), query=TWO_PARTS)
    assert body["outcome"] == "success" and body["visualization"]["type"] == "time_series"
    second = body["additional_answers"][0]
    assert second["outcome"] == "upstream_error" and second["error"]["code"] == "run_timeout"
    assert "stopped during retrieval from ClinicalTrials.gov" in second["message"]


async def test_loading_the_earlier_run_counts_toward_the_deadline(make_client, settings, monkeypatch):
    settings.run_deadline_seconds = 0.2
    async for client in make_client(ScriptedPlanner(TREND, TREND)):
        first = (await client.post("/v1/query", json=QUESTION)).json()
        store = client._transport.app.state.pipeline.runs  # type: ignore[attr-defined]

        async def slow_load(run_id, load=store.load):
            await asyncio.sleep(2)
            return await load(run_id)

        monkeypatch.setattr(store, "load", slow_load)
        started = time.perf_counter()
        body = (await client.post("/v1/query", json={**QUESTION, "previous_run_id": first["run_id"]})).json()
        assert time.perf_counter() - started < 1
    assert body["error"]["code"] == "run_timeout"
    assert "stopped during loading the earlier answer" in body["message"]


async def test_a_follow_up_while_the_store_is_down_is_503(make_client, monkeypatch):
    async for client in make_client(ScriptedPlanner(TREND)):
        first = (await client.post("/v1/query", json=QUESTION)).json()
        store = client._transport.app.state.pipeline.runs  # type: ignore[attr-defined]

        async def down(run_id):
            raise runs_module.StoreUnavailable("database down")

        monkeypatch.setattr(store, "load", down)
        response = await client.post("/v1/query", json={**QUESTION, "previous_run_id": first["run_id"]})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "store_unavailable"


async def test_a_slow_save_still_answers_with_a_warning(make_client, monkeypatch):
    monkeypatch.setattr(pipeline_module, "SAVE_TIMEOUT_SECONDS", 0.05, raising=False)
    async for client in make_client(ScriptedPlanner(TREND)):
        store = client._transport.app.state.pipeline.runs  # type: ignore[attr-defined]

        async def slow_save(*args):
            await asyncio.sleep(2)

        monkeypatch.setattr(store, "save", slow_save)
        started = time.perf_counter()
        body = (await client.post("/v1/query", json=QUESTION)).json()
        assert time.perf_counter() - started < 1
    assert body["outcome"] == "success"
    assert any("could not be saved" in w for w in body["warnings"])
    assert "chart_url" not in body


async def test_an_unreachable_database_does_not_hold_up_startup(tmp_path, monkeypatch):
    # On AWS the database's firewall was still closed: each task waited a minute to start, failed its
    # health checks and was replaced, so the first deployment never finished.
    store = SqlRunStore(f"sqlite+aiosqlite:///{tmp_path}/runs.db")

    async def unreachable():
        await asyncio.sleep(3600)

    monkeypatch.setattr(store, "_create", unreachable)
    async with asyncio.timeout(1):  # fails fast, rather than hanging, if startup waits for the database again
        await store.open()
    await store.close()  # cancels the background attempt


def test_postgres_connections_give_up_in_seconds(monkeypatch):
    seen = {}
    monkeypatch.setattr(runs_module, "create_async_engine", lambda url, **options: seen.update(url=url, **options))
    SqlRunStore("postgresql://user:pw@db.example/trials?sslmode=require")
    assert seen["url"].startswith("postgresql+asyncpg://") and seen["connect_args"]["timeout"] <= 10


def test_a_key_is_accepted_alone_or_as_stored_with_its_name():
    users = {KEY: "alice"}
    assert identify(KEY, users) == identify(f"alice:{KEY}", users) == identify(f" {KEY}\n", users) == "alice"
    assert identify(f"bob:{KEY}", users) is None  # the name must be the key's own
    assert identify("alice:not-the-key", users) is None and identify(None, users) is None


def test_a_key_pasted_through_a_notes_app_still_matches():
    hex_key = "0123456789abcdef0123456789abcdef01234567"
    users = {hex_key: "mubin"}
    for pasted in (
        f"Mubin:{hex_key}",  # first letter capitalized
        hex_key.upper(),
        f"\u201c{hex_key}\u201d",  # curly quotes
        f'"mubin:{hex_key}"',
        f"{hex_key}\u200b",  # a zero-width space
        f"\u00a0{hex_key} \r\n",
    ):
        assert identify(pasted, users) == "mubin", repr(pasted)
    assert identify(hex_key[:-1] + "8", users) is None  # one different character is still refused
