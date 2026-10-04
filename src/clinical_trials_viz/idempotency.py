"""Idempotent `POST /v1/query`: a retried request with the same `Idempotency-Key` returns the
original response instead of running again (no extra model calls, API requests or run IDs).

Keys are stored as small JSON files beside the run records, keyed by a hash of the key, and
expire after 24 hours. A key reused for a different request is rejected; a key whose first
request is still running is reported as in progress.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from clinical_trials_viz.models.request import QueryRequest

KEY_TTL = timedelta(hours=24)
MAX_KEY_LENGTH = 255


class KeyReused(Exception):
    """The key was already used for a different request."""


class KeyInProgress(Exception):
    """The first request with this key has not finished yet."""


class _Entry(BaseModel):
    fingerprint: str
    run_id: str
    created_at: datetime


def fingerprint(request: QueryRequest) -> str:
    """Hash of the validated request, so field order and whitespace do not matter."""
    canonical = json.dumps(request.model_dump(mode="json", exclude_none=True), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, directory: Path):
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._in_flight: set[str] = set()
        self._lock = asyncio.Lock()

    def _path(self, key: str) -> Path:
        return self._dir / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def _load(self, key: str) -> _Entry | None:
        path = self._path(key)
        if not path.exists():
            return None
        entry = _Entry.model_validate_json(path.read_text())
        return entry if datetime.now(UTC) - entry.created_at < KEY_TTL else None

    async def begin(self, key: str, request: QueryRequest) -> str | None:
        """Run ID to replay for this key, or None when the caller should run (and then `finish`)."""
        async with self._lock:
            entry = self._load(key)
            if entry is not None:
                if entry.fingerprint != fingerprint(request):
                    raise KeyReused(key)
                return entry.run_id
            if key in self._in_flight:
                raise KeyInProgress(key)
            self._in_flight.add(key)
            return None

    def finish(self, key: str, request: QueryRequest, run_id: str) -> None:
        entry = _Entry(fingerprint=fingerprint(request), run_id=run_id, created_at=datetime.now(UTC))
        self._path(key).write_text(entry.model_dump_json())
        self._in_flight.discard(key)

    def abandon(self, key: str) -> None:
        """The request failed before producing a run (e.g. unknown previous_run_id); allow a retry."""
        self._in_flight.discard(key)
