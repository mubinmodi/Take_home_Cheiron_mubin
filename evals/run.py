"""Score the configured planner against evals/questions.json.

    uv run python -m evals.run            # all cases
    uv run python -m evals.run cmp-01     # selected cases

Needs a model API key (see .env.example). Only the planner is called; no trial data is fetched.
"""

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from clinical_trials_viz.config import get_settings
from clinical_trials_viz.models.plan import QueryPlan
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.planner import UnconfiguredPlanner, build_planner

QUESTIONS = Path(__file__).with_name("questions.json")


def load_cases() -> list[dict[str, Any]]:
    return json.loads(QUESTIONS.read_text())["cases"]


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items() if v is not None}
    return value


def mismatches(plan: QueryPlan, expect: dict[str, Any]) -> list[str]:
    """Compare only the expected keys. Lists of names compare as sets."""
    actual = plan.model_dump(mode="json")
    problems = []
    for key, want in expect.items():
        if key == "filters":
            for fkey, fwant in want.items():
                got = actual.get("filters", {}).get(fkey)
                if _as_set(got) != _as_set(fwant):
                    problems.append(f"filters.{fkey}: expected {fwant!r}, got {got!r}")
        elif key == "compare_sides":
            got = [_norm(s) for s in actual.get("compare_sides", [])]
            if sorted(map(json.dumps, got)) != sorted(map(json.dumps, _norm(want))):
                problems.append(f"compare_sides: expected {want!r}, got {actual.get('compare_sides')!r}")
        elif _norm(actual.get(key)) != _norm(want):
            problems.append(f"{key}: expected {want!r}, got {actual.get(key)!r}")
    return problems


def _as_set(value: Any) -> Any:
    value = _norm(value)
    return frozenset(value) if isinstance(value, list) else value


async def main(selected: list[str]) -> int:
    settings = get_settings()
    planner = build_planner(settings.planner_primary, settings.planner_fallback)
    if isinstance(planner, UnconfiguredPlanner):
        print(planner.reason)
        return 2
    cases = [c for c in load_cases() if not selected or c["id"] in selected]
    by_family: dict[str, list[bool]] = defaultdict(list)
    for case in cases:
        request = QueryRequest(query=case["question"], **case.get("fields", {}))
        try:
            result = await planner.plan(request.query, request.structured_fields(), None)
            problems = mismatches(result.plan, case["expect"])
        except Exception as exc:  # a failed call is a failed case, not a crashed eval
            problems = [f"error: {exc}"]
        by_family[case["family"]].append(not problems)
        print(f"{'PASS' if not problems else 'FAIL'}  {case['id']:<16} {case['question']}")
        for p in problems:
            print(f"        {p}")
    passed = sum(sum(v) for v in by_family.values())
    print(f"\n{passed}/{len(cases)} plans correct ({passed / len(cases):.0%}) using {settings.planner_primary}")
    for family, results in sorted(by_family.items()):
        print(f"  {family:<15} {sum(results)}/{len(results)}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
