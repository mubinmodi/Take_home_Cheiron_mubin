"""Circuit breakers (docs/hosted-deployment.md): after repeated outage-type failures of one dependency,
fail fast for a cool-down instead of making every question wait through timeouts and retries.

Closed: calls go through; outage-type failures in a row are counted. Open: calls fail at once until
the cool-down ends. Then one trial call goes through (others still fail fast): success closes the
breaker, failure opens it for another cool-down. Each process keeps its own breakers.
"""

import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


class CircuitOpen(Exception):
    def __init__(self, name: str, retry_in: float):
        super().__init__(f"{name} is paused for {retry_in:.0f} s after repeated failures")
        self.name = name
        self.retry_in = retry_in


class CircuitBreaker:
    def __init__(self, name: str, failures: int, cooldown: float, clock: Callable[[], float] = time.monotonic):
        self.name = name
        self._threshold = failures
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._trial = False  # a trial call is in flight

    def check(self) -> None:
        """Raise CircuitOpen while open; after the cool-down, let one trial call through."""
        if self._opened_at is None:
            return
        elapsed = self._clock() - self._opened_at
        if elapsed < self._cooldown or self._trial:
            raise CircuitOpen(self.name, max(0.0, self._cooldown - elapsed))
        self._trial = True

    def success(self) -> None:
        """The dependency answered (an answer that rejects the request counts: it is up)."""
        if self._opened_at is not None:
            log.warning("%s answered again; circuit closed", self.name)
        self._failures, self._opened_at, self._trial = 0, None, False

    def failure(self) -> None:
        """An outage-type failure: server error, rate limit, timeout or connection error."""
        self._failures += 1
        if self._trial or (self._opened_at is None and self._failures >= self._threshold):
            log.warning("%s failed %d times in a row; failing fast for %g s", self.name, self._failures, self._cooldown)
            self._opened_at, self._trial = self._clock(), False

    def release(self) -> None:
        """The call ended without telling either way (e.g. cancelled): free the trial slot."""
        self._trial = False
