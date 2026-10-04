"""Async ClinicalTrials.gov API v2 client: paging, rate limiting, retries and a page cache."""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import httpx

from clinical_trials_viz.catalog import PAGE_SIZE, TRIAL_FIELDS


class UpstreamError(Exception):
    """ClinicalTrials.gov failed or returned something unusable."""


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
        self, http: httpx.AsyncClient, base_url: str, requests_per_minute: int, max_pages: int, cache_pages: int = 256
    ):
        self._http = http
        self._base = base_url.rstrip("/")
        self._limiter = RateLimiter(requests_per_minute)
        self.max_pages = max_pages
        self._cache: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self._cache_pages = cache_pages
        self._version: tuple[float, SourceVersion] | None = None
        self.requests_made = 0  # network requests, excluding cache hits

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        for attempt in range(3):
            await self._limiter.acquire()
            self.requests_made += 1
            try:
                response = await self._http.get(f"{self._base}{path}", params=params)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise UpstreamError(f"ClinicalTrials.gov unreachable: {exc}") from exc
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise UpstreamError(
                        f"ClinicalTrials.gov returned HTTP {response.status_code}: {response.text[:200]}"
                    )
            await asyncio.sleep(2**attempt)
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
        key = (data_timestamp, tuple(sorted(params.items())))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        page = await self._get("/studies", params)
        self._cache[key] = page
        if len(self._cache) > self._cache_pages:
            self._cache.popitem(last=False)
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

    async def countries(self) -> list[str]:
        """All country names the API uses for site locations."""
        body = await self._get("/stats/field/values", {"fields": "LocationCountry"})
        return [v["value"] for v in body[0]["topValues"]]
