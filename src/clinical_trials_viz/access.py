"""Access control for the hosted service (docs/hosted-deployment.md): an API key per user, and a limit
on questions per user per hour, because every question costs model calls. Locally neither is set."""

import hmac
import time
from typing import Protocol


class UserLimiter(Protocol):
    async def spend(self, user: str) -> float | None:
        """Count one question for `user`. Seconds until they may ask again when over the limit, else None."""
        ...


def hour_window(now: float) -> tuple[int, float]:
    """The current one-hour window and the seconds left in it."""
    hour = int(now // 3600)
    return hour, (hour + 1) * 3600 - now


class MemoryUserLimiter:
    """Questions per user per hour, counted in this process."""

    def __init__(self, per_hour: int):
        self._per_hour = per_hour
        self._counts: dict[tuple[str, int], int] = {}

    async def spend(self, user: str) -> float | None:
        hour, left = hour_window(time.time())
        self._counts = {k: n for k, n in self._counts.items() if k[1] == hour}  # forget earlier hours
        count = self._counts[user, hour] = self._counts.get((user, hour), 0) + 1
        return left if count > self._per_hour else None


def identify(api_key: str | None, users: dict[str, str]) -> str | None:
    """The user this key belongs to, or None. Accepts the key alone or as stored in API_KEYS
    ("name:key", which people paste from the secret). Compares against every key in constant time."""
    if api_key is None:
        return None
    sent = api_key.strip().encode()
    match = None
    for key, name in users.items():
        if hmac.compare_digest(sent, key.encode()) or hmac.compare_digest(sent, f"{name}:{key}".encode()):
            match = name
    return match
