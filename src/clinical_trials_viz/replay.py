"""Run bundles and replay: the raw ClinicalTrials.gov responses behind a Run, kept so the Run can be
answered again offline, exactly, after the registry has moved on.

A bundle holds every API response the Run used (cached or fetched) and the country list it validated
against. Replaying feeds them to a client that never goes online and to a planner that returns the Run's
recorded plans (no model call), then compares the new answer with the recorded one."""

import asyncio
import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from clinical_trials_viz.ctgov.client import CtGovClient, RateLimiter, UpstreamError
from clinical_trials_viz.ledger import Step, run_ledger
from clinical_trials_viz.models.plan import MultiAnswerPlan, QueryPlan
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.models.response import Answer, QueryResponse
from clinical_trials_viz.planner import PlannerResult, SplitResult
from clinical_trials_viz.runs import RunRecord, RunStore


class BundleStore:
    """Run bundles as gzipped JSON files, one per Run."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json.gz"

    async def save(self, run_id: str, responses: list[dict[str, Any]], countries: list[str]) -> None:
        bundle = {"run_id": run_id, "countries": countries, "responses": responses}
        await asyncio.to_thread(self._write, run_id, json.dumps(bundle).encode())

    def _write(self, run_id: str, data: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._path(run_id).with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(data))
        tmp.replace(self._path(run_id))

    def load(self, run_id: str) -> dict[str, Any] | None:
        path = self._path(run_id)
        return json.loads(gzip.decompress(path.read_bytes())) if path.exists() else None


def _key(path: str, params: dict[str, str] | None) -> str:
    return path + "?" + json.dumps(sorted((params or {}).items()))


class ReplayClient(CtGovClient):
    """A ClinicalTrials.gov client that answers only from a bundle; a request it lacks is an error."""

    def __init__(self, bundle: dict[str, Any], max_pages: int):
        super().__init__(httpx.AsyncClient(), "https://replay.invalid/api/v2", RateLimiter(10**6), max_pages)
        self._bodies = {_key(r["path"], r["params"]): r["body"] for r in bundle["responses"]}

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        try:
            return self._bodies[_key(path, params)]
        except KeyError:
            raise UpstreamError(f"the run bundle has no response for {path} {params}") from None


class RecordedPlanner:
    """Returns the Run's recorded plans: the split it made and the plan of each part. No model call."""

    def __init__(self, record: RunRecord):
        plan = record.plan
        self.requests = plan.requests if isinstance(plan, MultiAnswerPlan) else [record.request.query]
        self.plans: list[QueryPlan] = list(plan.parts) if isinstance(plan, MultiAnswerPlan) else [plan]  # type: ignore[list-item]

    async def split(self, message: str) -> SplitResult:
        return SplitResult(list(self.requests), "recorded")

    async def plan(self, question, structured, previous_plan, *, repair=None, context=None) -> PlannerResult:
        index = self.requests.index(question) if question in self.requests else 0
        with run_ledger() as ledger, ledger.step(Step.PLANNING):
            ledger.record()
        return PlannerResult(self.plans[index], "recorded", [])


class _ReadOnlyRuns:
    """Loads earlier runs (a Follow-up's previous run) from the real store; saves nothing."""

    def __init__(self, runs: RunStore):
        self._runs = runs

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def save(self, request: QueryRequest, response: QueryResponse, user: str | None = None) -> None:
        return None

    async def load(self, run_id: str) -> RunRecord | None:
        return await self._runs.load(run_id)


# What an answer says, without what differs between any two Runs (IDs, links, times, model costs).
_VOLATILE = {"run_id", "chart_url", "timings_ms", "model_calls", "planner_model", "warnings", "additional_answers"}


def _parts(response: QueryResponse) -> Iterator[Answer]:
    yield response
    yield from response.additional_answers


def _content(answer: Answer) -> dict[str, Any]:
    content = answer.model_dump(mode="json", exclude=_VOLATILE, exclude_none=True)
    if source := content.get("source"):
        source.pop("retrieved_at", None)
        source.pop("api_requests", None)
    return content


@dataclass
class ReplayResult:
    original: QueryResponse
    replayed: QueryResponse
    differences: list[str]


async def replay(record: RunRecord, bundle: dict[str, Any], runs: RunStore, max_pages: int) -> ReplayResult:
    """Answer a recorded Run again from its bundle and recorded plans, and list what came out different."""
    from clinical_trials_viz.pipeline import Pipeline  # the pipeline imports this module

    pipeline = Pipeline(ReplayClient(bundle, max_pages), RecordedPlanner(record), _ReadOnlyRuns(runs))
    pipeline._countries = set(bundle["countries"])
    replayed = await pipeline.run(record.request)
    differences = []
    originals, again = list(_parts(record.response)), list(_parts(replayed))
    if len(originals) != len(again):
        differences.append(f"{len(originals)} part(s) recorded, {len(again)} replayed")
    for i, (a, b) in enumerate(zip(originals, again, strict=False)):
        before, after = _content(a), _content(b)
        for name in sorted(set(before) | set(after)):
            if before.get(name) != after.get(name):
                differences.append(f"part {i + 1}: {name} differs")
    return ReplayResult(record.response, replayed, differences)
