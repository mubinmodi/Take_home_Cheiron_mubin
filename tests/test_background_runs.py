"""Background Runs: POST /v1/runs returns at once; the answer is polled (ticket 13)."""

import asyncio

from clinical_trials_viz.catalog import Dimension
from clinical_trials_viz.models.plan import AnswerPlan, Filters, Operation
from tests.conftest import ScriptedPlanner

TREND = AnswerPlan(operation=Operation.AGGREGATE, filters=Filters(drugs=["Keytruda"]), group_by=Dimension.START_YEAR)


async def _poll(client, url: str) -> dict:
    for _ in range(200):
        status = (await client.get(url)).json()
        if status["status"] != "running":
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("the background run did not finish")


async def test_a_background_run_is_polled_until_done(make_client):
    async for client in make_client(ScriptedPlanner(TREND)):
        started = await client.post("/v1/runs", json={"query": "Keytruda trials per year"})
        assert started.status_code == 202
        body = started.json()
        assert body["status"] == "running" and body["status_url"] == f"/v1/runs/{body['run_id']}/status"
        done = await _poll(client, body["status_url"])
        assert done["status"] == "done"
        assert done["response"]["run_id"] == body["run_id"]
        assert done["response"]["visualization"]["type"] == "time_series"
        record = await client.get(f"/v1/runs/{body['run_id']}")
        assert record.status_code == 200


class SlowPlanner(ScriptedPlanner):
    async def plan(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        return await super().plan(*args, **kwargs)


async def test_a_background_run_is_not_held_to_the_request_deadline(make_client, settings):
    settings.run_deadline_seconds = 0.01  # shorter than the slow planner: POST /v1/query stops
    settings.background_run_deadline_seconds = None
    async for client in make_client(SlowPlanner(TREND, TREND)):
        quick = (await client.post("/v1/query", json={"query": "Keytruda trials per year"})).json()
        assert quick["outcome"] == "upstream_error" and quick["error"]["code"] == "run_timeout"
        started = (await client.post("/v1/runs", json={"query": "Keytruda trials per year"})).json()
        done = await _poll(client, started["status_url"])
        assert done["response"]["outcome"] == "success"


async def test_background_run_errors(make_client, monkeypatch):
    async for client in make_client(ScriptedPlanner(TREND)):
        missing = await client.post("/v1/runs", json={"query": "only phase 3", "previous_run_id": "run_" + "0" * 32})
        assert missing.status_code == 404
        assert (await client.get("/v1/runs/run_unknown/status")).status_code == 404

        pipeline = client._transport.app.state.pipeline  # type: ignore[attr-defined]

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(pipeline, "run", broken)
        started = (await client.post("/v1/runs", json={"query": "Keytruda trials per year"})).json()
        failed = await _poll(client, started["status_url"])
        assert failed["status"] == "failed" and failed["error"]["code"] == "internal"
        assert "boom" not in str(failed)  # the exception text stays in the server log


async def test_job_states_are_shared_through_redis():
    from fakeredis import FakeAsyncRedis

    from clinical_trials_viz.jobs import RedisJobStore

    redis = FakeAsyncRedis()
    one, other = RedisJobStore(redis), RedisJobStore(redis)  # two instances
    await one.start("run_a")
    assert await other.get("run_a") == {"status": "running"}
    await one.fail("run_a", {"code": "internal", "message": "x", "retryable": True})
    assert (await other.get("run_a"))["status"] == "failed"
    await one.finish("run_a")
    assert await other.get("run_a") is None
