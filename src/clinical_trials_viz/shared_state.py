"""State shared by every instance of the hosted service, in Redis (docs/hosted-deployment.md):

- one ClinicalTrials.gov request budget, because every instance shares the registry's per-IP limit;
- the page cache, so a page one instance fetched serves the others;
- Idempotency-Keys, so a retry that reaches another instance still replays.

A Redis failure degrades instead of failing questions: the budget falls back to this instance's own
bucket, the cache misses, and idempotency is skipped. Each logs once when Redis fails and once when
it recovers.
"""

import asyncio
import json
import logging
import time
import zlib
from hashlib import sha256
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from clinical_trials_viz.ctgov.client import RateLimiter
from clinical_trials_viz.idempotency import KEY_TTL, KeyInProgress, KeyReused, fingerprint
from clinical_trials_viz.models.request import QueryRequest

log = logging.getLogger(__name__)


class _Health:
    """Logs when Redis starts failing for one purpose and when it recovers, not on every call."""

    def __init__(self, purpose: str):
        self._purpose = purpose
        self._failing = False

    def failed(self, exc: Exception) -> None:
        if not self._failing:
            log.warning("Redis unavailable for %s (%s); continuing without it", self._purpose, type(exc).__name__)
            self._failing = True

    def ok(self) -> None:
        if self._failing:
            log.warning("Redis available again for %s", self._purpose)
            self._failing = False


# Generic cell rate algorithm: the whole bucket is one timestamp, the theoretical arrival time (TAT) of
# the next request. A request fits when the TAT is at most `tolerance` ahead of now, and then moves it
# one interval on. Returns 0 when granted, else the milliseconds to wait. Atomic, so instances never
# overspend together.
_GCRA = """
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local tolerance = tonumber(ARGV[3])
local tat = tonumber(redis.call('GET', KEYS[1]) or '0')
if tat < now then tat = now end
if tat - now > tolerance then
  return math.ceil(tat - now - tolerance)
end
local next_tat = tat + interval
redis.call('SET', KEYS[1], tostring(next_tat), 'PX', math.ceil(next_tat - now + interval))
return 0
"""


class RedisRateLimiter:
    """`per_minute` ClinicalTrials.gov requests a minute across all instances, with bursts of up to
    `per_minute` like the in-process bucket."""

    def __init__(self, redis: Redis, per_minute: int, key: str = "ctgov:budget"):
        self._script = redis.register_script(_GCRA)
        self._key = key
        self._interval_ms = 60_000 / per_minute
        self._tolerance_ms = (per_minute - 1) * self._interval_ms
        self._fallback = RateLimiter(per_minute)
        self._health = _Health("the ClinicalTrials.gov rate limit")

    async def reserve(self, now_ms: float) -> float:
        """Milliseconds to wait before one more request fits; 0 means this one was granted."""
        return float(await self._script(keys=[self._key], args=[now_ms, self._interval_ms, self._tolerance_ms]))

    async def acquire(self) -> None:
        while True:
            try:
                wait_ms = await self.reserve(time.time() * 1000)
            except RedisError as exc:
                self._health.failed(exc)
                await self._fallback.acquire()
                return
            self._health.ok()
            if wait_ms <= 0:
                return
            await asyncio.sleep(wait_ms / 1000)


_MAX_CACHED_BYTES = 1_000_000  # compressed; a page of 1,000 trials is about 0.3 MB


class RedisPageCache:
    """ClinicalTrials.gov result pages, compressed, kept for a day. Keys carry the registry's data
    timestamp, so a new data release never reads an old page."""

    def __init__(self, redis: Redis, ttl_seconds: int = 24 * 3600):
        self._redis = redis
        self._ttl = ttl_seconds
        self._health = _Health("the page cache")

    @staticmethod
    def _key(key: str) -> str:
        return "ctgov:page:" + sha256(key.encode()).hexdigest()

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            blob = await self._redis.get(self._key(key))
        except RedisError as exc:
            self._health.failed(exc)
            return None
        self._health.ok()
        if not isinstance(blob, bytes):  # missing
            return None
        try:
            return json.loads(zlib.decompress(blob))
        except (zlib.error, ValueError):  # a damaged entry is a miss; the fresh page replaces it
            return None

    async def put(self, key: str, page: dict[str, Any]) -> None:
        blob = zlib.compress(json.dumps(page, separators=(",", ":")).encode())
        if len(blob) > _MAX_CACHED_BYTES:
            return
        try:
            await self._redis.set(self._key(key), blob, ex=self._ttl)
        except RedisError as exc:
            self._health.failed(exc)


# A claim by a request still running expires after this long, so an instance that dies mid-request
# does not block retries of its key for a day.
_CLAIM_TTL_SECONDS = 300


class RedisIdempotencyStore:
    """Idempotency-Keys shared by every instance: a retry that reaches another instance still replays,
    and a key in use on one instance is reported as in progress on all of them."""

    def __init__(self, redis: Redis):
        self._redis = redis
        self._health = _Health("Idempotency-Keys")

    @staticmethod
    def _key(key: str) -> str:
        return "idempotency:" + sha256(key.encode()).hexdigest()

    async def begin(self, key: str, request: QueryRequest) -> str | None:
        claim = json.dumps({"fingerprint": fingerprint(request), "run_id": None})
        try:
            for _ in range(2):  # the entry can expire between SET and GET: claim once more
                if await self._redis.set(self._key(key), claim, nx=True, ex=_CLAIM_TTL_SECONDS):
                    self._health.ok()
                    return None
                stored = await self._redis.get(self._key(key))
                if stored is None:
                    continue
                entry = json.loads(stored)
                if entry["fingerprint"] != fingerprint(request):
                    raise KeyReused(key)
                if entry["run_id"] is None:
                    raise KeyInProgress(key)
                return entry["run_id"]
        except RedisError as exc:
            self._health.failed(exc)
        return None  # answer without idempotency rather than fail the question

    async def finish(self, key: str, request: QueryRequest, run_id: str) -> None:
        entry = json.dumps({"fingerprint": fingerprint(request), "run_id": run_id})
        try:
            await self._redis.set(self._key(key), entry, ex=int(KEY_TTL.total_seconds()))
        except RedisError as exc:
            self._health.failed(exc)

    async def abandon(self, key: str) -> None:
        try:
            await self._redis.delete(self._key(key))
        except RedisError as exc:
            self._health.failed(exc)
