"""The Run's model-call ledger: every model attempt is recorded once, where it happens, and the caps
of harness-design ("at most 3 planning calls, plus one split call, two if the primary fails") are
enforced here and nowhere else.

The pipeline opens one ledger per Run (`run_ledger`). Planner adapters find it through the Run context
(`current_ledger`): the model wrapper records each real provider attempt, a scripted planner records
its stand-in calls. Failed attempts count; the SDK's own single retry inside one attempt does not."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic_ai import UsageLimitExceeded


class Step(StrEnum):
    SPLIT = "split"  # list the separate questions in a message
    PLANNING = "planning"  # turn each question into a Query Plan (repairs included)


CAPS = {Step.SPLIT: 2, Step.PLANNING: 3}  # split: the call and, if the primary fails, the fallback's


class CallBudgetExhausted(UsageLimitExceeded):
    """A model attempt refused because its step's calls are used up. It subclasses UsageLimitExceeded
    only so the FallbackModel does not try the next model (it falls back on provider errors only)."""


@dataclass
class CallLedger:
    _used: dict[Step, int] = field(default_factory=lambda: dict.fromkeys(Step, 0))
    _failed: dict[str, str] = field(default_factory=dict)  # model -> its outage, for the rest of the Run
    _step: Step = Step.PLANNING
    _cap: int | None = None  # a narrower cap on the current step, e.g. for one part of a Question

    def used(self, step: Step) -> int:
        return self._used[step]

    @property
    def total(self) -> int:
        return sum(self._used.values())

    def remaining(self, step: Step) -> int:
        limit = CAPS[step] if self._cap is None or step is not self._step else min(CAPS[step], self._cap)
        return max(0, limit - self._used[step])

    def require_room(self) -> None:
        """Refuse a model attempt once the current step's calls are used up."""
        if not self.remaining(self._step):
            raise CallBudgetExhausted(f"The model calls allowed for {self._step} in this question are used up.")

    def record(self) -> None:
        """Count one model attempt in the current step (refused once the step's calls are used up)."""
        self.require_room()
        self._used[self._step] += 1

    @contextmanager
    def step(self, step: Step) -> Iterator[None]:
        previous, self._step = self._step, step
        try:
            yield
        finally:
            self._step = previous

    @contextmanager
    def cap(self, calls: int) -> Iterator[None]:
        """Allow at most `calls` more attempts in the current step until the block ends."""
        previous, self._cap = self._cap, self._used[self._step] + calls
        try:
            yield
        finally:
            self._cap = previous

    def model_failed(self, model: str, reason: str) -> None:
        self._failed[model] = reason

    def failure_of(self, model: str) -> str | None:
        return self._failed.get(model)


_ledger: ContextVar[CallLedger | None] = ContextVar("run_call_ledger", default=None)


def current_ledger() -> CallLedger | None:
    return _ledger.get()


@contextmanager
def run_ledger() -> Iterator[CallLedger]:
    """Open the ledger of one Run (or reuse the one already open, e.g. when planning runs inside it)."""
    if (open_ledger := _ledger.get()) is not None:
        yield open_ledger
        return
    ledger = CallLedger()
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _ledger.reset(token)
