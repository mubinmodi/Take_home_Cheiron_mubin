"""Async ClinicalTrials.gov API v2 client: paging, rate limiting, retries and a page cache."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from clinical_trials_viz.breaker import CircuitBreaker, CircuitOpen
from clinical_trials_viz.catalog import PAGE_SIZE, TRIAL_FIELDS
from clinical_trials_viz.models.response import ErrorCode

MAX_ATTEMPTS = 4
RETRYABLE = frozenset({429, 500, 502, 503, 504})


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait: the server's Retry-After when given (capped at 30 s), else 2, 4, 8 s."""
    retry_after = response.headers.get("Retry-After", "")
    if retry_after.isdigit():
        return min(float(retry_after), 30.0)
    return 2.0 ** (attempt + 1)


class UpstreamError(Exception):
    """ClinicalTrials.gov failed or returned something unusable, after retries where retrying helps."""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.SOURCE_UNAVAILABLE, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _http_error(response: httpx.Response) -> UpstreamError:
    status = response.status_code
    if status == 429:
        return UpstreamError(
            "ClinicalTrials.gov rate limit reached; try again in a minute", ErrorCode.SOURCE_RATE_LIMITED
        )
    if status >= 500:
        return UpstreamError(f"ClinicalTrials.gov is failing (HTTP {status})", ErrorCode.SOURCE_UNAVAILABLE)
    return UpstreamError(
        f"ClinicalTrials.gov rejected the request (HTTP {status}): {response.text[:200]}",
        ErrorCode.SOURCE_REJECTED,
        retryable=False,
    )


class ScopeTooLarge(Exception):
    """The search matches more trials than the page cap allows; never sample silently."""

    def __init__(self, total: int, limit: int):
        super().__init__(f"{total} trials match; the limit is {limit}")
        self.total = total
        self.limit = limit


@dataclass(frozen=True)
class SourceVersion:
    api_version: str
    data_timestamp: str


@dataclass
class SearchResult:
    studies: list[dict[str, Any]]
    total: int
    complete: bool  # every page was fetched


class Limiter(Protocol):
    async def acquire(self) -> None:
        """Wait until one more request fits the budget."""
        ...


class PageCache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def put(self, key: str, page: dict[str, Any]) -> None: ...


def page_key(params: dict[str, str], data_timestamp: str) -> str:
    """Cache key of one result page: the compiled request and the registry's data timestamp, so the
    cache turns over when ClinicalTrials.gov publishes new data."""
    return f"{data_timestamp}?{urlencode(sorted(params.items()))}"


class MemoryPageCache:
    """The most recently used pages, in this process."""

    def __init__(self, max_pages: int = 256):
        self._pages: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max_pages = max_pages

    async def get(self, key: str) -> dict[str, Any] | None:
        page = self._pages.get(key)
        if page is not None:
            self._pages.move_to_end(key)
        return page

    async def put(self, key: str, page: dict[str, Any]) -> None:
        self._pages[key] = page
        if len(self._pages) > self._max_pages:
            self._pages.popitem(last=False)


_requests_in_run: ContextVar[list[int] | None] = ContextVar("ctgov_requests_in_run", default=None)


@contextmanager
def count_requests() -> Iterator[list[int]]:
    """Count the network requests made inside this block (one Run), however many Runs are in flight."""
    counter = [0]
    token = _requests_in_run.set(counter)
    try:
        yield counter
    finally:
        _requests_in_run.reset(token)


class RateLimiter:
    """Token bucket shared by all requests from this process."""

    def __init__(self, per_minute: int):
        self._interval = 60.0 / per_minute
        self._capacity = float(per_minute)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) / self._interval)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) * self._interval)


class CtGovClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        limiter: Limiter,
        max_pages: int,
        cache: PageCache | None = None,
        breaker: CircuitBreaker | None = None,
    ):
        self._http = http
        self._base = base_url.rstrip("/")
        self._limiter = limiter
        self.max_pages = max_pages
        self._cache = cache or MemoryPageCache()
        self._breaker = breaker or CircuitBreaker("ClinicalTrials.gov", failures=5, cooldown=30.0)
        self._version: tuple[float, SourceVersion] | None = None

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """One logical request (with its retries), behind the circuit breaker."""
        try:
            self._breaker.check()
        except CircuitOpen as exc:
            raise UpstreamError(
                f"ClinicalTrials.gov failed repeatedly; requests are paused for {exc.retry_in:.0f} s. "
                "Please try again shortly.",
                ErrorCode.SOURCE_UNAVAILABLE,
            ) from exc
        try:
            body = await self._attempts(path, params)
        except UpstreamError as exc:
            if exc.code is ErrorCode.SOURCE_REJECTED:
                self._breaker.success()  # it answered; the request was at fault
            else:
                self._breaker.failure()
            raise
        except BaseException:
            self._breaker.release()
            raise
        self._breaker.success()
        return body

    async def _attempts(self, path: str, params: dict[str, str] | None) -> Any:
        for attempt in range(MAX_ATTEMPTS):
            last = attempt == MAX_ATTEMPTS - 1
            await self._limiter.acquire()
            if (counter := _requests_in_run.get()) is not None:
                counter[0] += 1
            try:
                response = await self._http.get(f"{self._base}{path}", params=params)
            except httpx.TransportError as exc:
                if last:
                    raise UpstreamError(f"ClinicalTrials.gov unreachable: {type(exc).__name__}") from exc
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:  # a maintenance page or truncated body instead of JSON
                    if last:
                        raise UpstreamError(
                            "ClinicalTrials.gov returned a response that is not JSON",
                            ErrorCode.SOURCE_INVALID_RESPONSE,
                        ) from exc
                    await asyncio.sleep(2**attempt)
                    continue
            if response.status_code not in RETRYABLE or last:
                raise _http_error(response)
            await asyncio.sleep(_retry_delay(response, attempt))
        raise UpstreamError("unreachable")  # pragma: no cover

    async def version(self) -> SourceVersion:
        """API version and data timestamp; re-checked every 10 minutes (data refreshes daily)."""
        if self._version and time.monotonic() - self._version[0] < 600:
            return self._version[1]
        body = await self._get("/version")
        version = SourceVersion(body["apiVersion"], body["dataTimestamp"])
        self._version = (time.monotonic(), version)
        return version

    async def _page(self, params: dict[str, str], data_timestamp: str) -> dict[str, Any]:
        key = page_key(params, data_timestamp)
        if (page := await self._cache.get(key)) is not None:
            return page
        page = await self._get("/studies", params)
        await self._cache.put(key, page)
        return page

    async def search(
        self,
        params: dict[str, str],
        *,
        fields: list[str] | None = None,
        max_pages: int | None = None,
        allow_partial: bool = False,
    ) -> SearchResult:
        """Fetch every matching study. Raises ScopeTooLarge rather than returning a partial cohort,
        unless `allow_partial` (used only for building Clarification options)."""
        max_pages = max_pages or self.max_pages
        version = await self.version()
        base = {**params, "fields": ",".join(fields or TRIAL_FIELDS), "pageSize": str(PAGE_SIZE)}
        first = await self._page({**base, "countTotal": "true"}, version.data_timestamp)
        total = int(first.get("totalCount", 0))
        if total > max_pages * PAGE_SIZE and not allow_partial:
            raise ScopeTooLarge(total, max_pages * PAGE_SIZE)
        studies = list(first.get("studies", []))
        token = first.get("nextPageToken")
        pages = 1
        while token and pages < max_pages:
            page = await self._page({**base, "pageToken": token}, version.data_timestamp)
            studies.extend(page.get("studies", []))
            token = page.get("nextPageToken")
            pages += 1
        complete = token is None
        if not complete and not allow_partial:  # pragma: no cover - guarded by the total check above
            raise ScopeTooLarge(total, max_pages * PAGE_SIZE)
        return SearchResult(studies, total, complete)

    async def count(self, params: dict[str, str]) -> int:
        """How many studies match, without fetching them (one small request)."""
        version = await self.version()
        page = await self._page(
            {**params, "fields": "NCTId", "pageSize": "1", "countTotal": "true"}, version.data_timestamp
        )
        return int(page.get("totalCount", 0))

    async def countries(self) -> list[str]:
        """All country names the API uses for site locations."""
        body = await self._get("/stats/field/values", {"fields": "LocationCountry"})
        return [v["value"] for v in body[0]["topValues"]]
