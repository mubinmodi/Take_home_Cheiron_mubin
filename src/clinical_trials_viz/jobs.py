"""Background Runs (`POST /v1/runs`): which Runs are still being answered, or failed before they could be
saved. A finished Run is read from the run store like any other, so this only holds what is in between."""

import json
import logging
from contextlib import suppress
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

JOB_TTL_SECONDS = 3600  # a job an instance lost (e.g. it was replaced mid-run) stops showing as running


class JobStore(Protocol):
    async def start(self, run_id: str) -> None: ...

    async def fail(self, run_id: str, error: dict[str, Any]) -> None: ...

    async def finish(self, run_id: str) -> None: ...

    async def get(self, run_id: str) -> dict[str, Any] | None:
        """`{"status": "running"}` or `{"status": "failed", "error": {...}}`; None when unknown or finished."""
        ...


class MemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    async def start(self, run_id: str) -> None:
        self._jobs[run_id] = {"status": "running"}

    async def fail(self, run_id: str, error: dict[str, Any]) -> None:
        self._jobs[run_id] = {"status": "failed", "error": error}

    async def finish(self, run_id: str) -> None:
        self._jobs.pop(run_id, None)

    async def get(self, run_id: str) -> dict[str, Any] | None:
        return self._jobs.get(run_id)


class RedisJobStore:
    """Job states shared by every instance, so any instance can answer a status request."""

    def __init__(self, redis: Redis):
        self._redis = redis
        self._fallback = MemoryJobStore()

    @staticmethod
    def _key(run_id: str) -> str:
        return f"job:{run_id}"

    async def _set(self, run_id: str, state: dict[str, Any]) -> None:
        try:
            await self._redis.set(self._key(run_id), json.dumps(state), ex=JOB_TTL_SECONDS)
        except RedisError as exc:  # this instance still knows its own jobs
            log.warning("Redis unavailable for background runs (%s)", type(exc).__name__)
        self._fallback._jobs[run_id] = state

    async def start(self, run_id: str) -> None:
        await self._set(run_id, {"status": "running"})

    async def fail(self, run_id: str, error: dict[str, Any]) -> None:
        await self._set(run_id, {"status": "failed", "error": error})

    async def finish(self, run_id: str) -> None:
        await self._fallback.finish(run_id)
        with suppress(RedisError):  # the key expires on its own
            await self._redis.delete(self._key(run_id))

    async def get(self, run_id: str) -> dict[str, Any] | None:
        try:
            stored = await self._redis.get(self._key(run_id))
        except RedisError:
            return await self._fallback.get(run_id)
        return json.loads(stored) if stored else await self._fallback.get(run_id)
