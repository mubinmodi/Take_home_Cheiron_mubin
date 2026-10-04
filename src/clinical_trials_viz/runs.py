"""Run records: the Query Plan and response of each Run, one small JSON file per Run.

Enough for Follow-ups (`previous_run_id`) and `chart_url`. Full run bundles with raw
API responses (for replay) are deferred.
"""

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from clinical_trials_viz.models.plan import AnswerPlan, ClarifyPlan, MultiAnswerPlan, UnsupportedPlan
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.models.response import QueryResponse

_RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")


class RunRecord(BaseModel):
    run_id: str
    created_at: datetime
    request: QueryRequest
    plan: AnswerPlan | MultiAnswerPlan | ClarifyPlan | UnsupportedPlan | None
    response: QueryResponse


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


class RunStore:
    def __init__(self, directory: Path):
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path | None:
        return self._dir / f"{run_id}.json" if _RUN_ID.match(run_id) else None

    def save(self, request: QueryRequest, response: QueryResponse) -> None:
        record = RunRecord(
            run_id=response.run_id, created_at=datetime.now(UTC), request=request, plan=response.plan, response=response
        )
        path = self._path(response.run_id)
        assert path is not None
        path.write_text(record.model_dump_json(indent=2))

    def load(self, run_id: str) -> RunRecord | None:
        path = self._path(run_id)
        if path is None or not path.exists():
            return None
        return RunRecord.model_validate_json(path.read_text())
