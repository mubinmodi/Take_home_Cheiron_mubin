"""Score the configured planner against evals/questions.json.

    uv run python -m evals.run                    # all cases in questions.json
    uv run python -m evals.run cmp-01             # selected cases
    uv run python -m evals.run --heldout          # the held-out set (evals/heldout.json) instead

Needs a model API key (see .env.example). Only the planner is called; no trial data is fetched.

questions.json is the working set: the appendix questions plus adversarial cases (misspellings, brand
vs generic names, negations, unsupported questions that sound supported, conflicting structured
fields, multi-part questions with an unsupported part, vague references). heldout.json holds fresh
phrasings of every question family and is never scored by default. Score it only at the end, after
prompt and catalog work is finished; its failures must not inform prompt changes, and its questions
are never copied into the prompt (a case seen while tuning is no longer held out: replace it).

An expected filter is a value, or {"one_of": [...]} (any listed value is right, e.g. a misspelled drug
copied as written or corrected), or {"excludes": [...]} (a non-empty list holding none of these, e.g.
"not recruiting"). A part of a multi-part expectation is an answer unless it sets `kind`.
"""

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from clinical_trials_viz.config import get_settings
from clinical_trials_viz.models.plan import AnswerPlan, MultiAnswerPlan, QueryPlan
from clinical_trials_viz.models.request import QueryRequest
from clinical_trials_viz.planner import Planner, UnconfiguredPlanner, build_planner
from clinical_trials_viz.planning import plan_question

QUESTIONS = Path(__file__).with_name("questions.json")
HELDOUT = Path(__file__).with_name("heldout.json")


def load_cases(path: Path = QUESTIONS) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["cases"]


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items() if v is not None}
    return value


def mismatches(plan: QueryPlan | AnswerPlan, expect: dict[str, Any]) -> list[str]:
    """Compare only the expected keys. Lists of names compare as sets. A multi-part expectation
    ({"kind": "multi", "parts": [...]}) compares each part in order."""
    if expect.get("kind") == "multi":
        if not isinstance(plan, MultiAnswerPlan):
            return [f"kind: expected 'multi', got {plan.kind!r}"]
        if len(plan.parts) != len(expect["parts"]):
            return [f"parts: expected {len(expect['parts'])}, got {len(plan.parts)}"]
        return [f"part {i + 1} {p}" for i, (part, want) in enumerate(zip(plan.parts, expect["parts"], strict=True))
                for p in mismatches(part, {"kind": "answer", **want})]  # fmt: skip
    actual = plan.model_dump(mode="json")
    problems = []
    for key, want in expect.items():
        if key == "filters":
            for fkey, fwant in want.items():
                got = actual.get("filters", {}).get(fkey)
                if not _filter_matches(got, fwant):
                    problems.append(f"filters.{fkey}: expected {fwant!r}, got {got!r}")
        elif key == "compare_sides":
            got = [_norm(s) for s in actual.get("compare_sides", [])]
            if sorted(map(json.dumps, got)) != sorted(map(json.dumps, _norm(want))):
                problems.append(f"compare_sides: expected {want!r}, got {actual.get('compare_sides')!r}")
        elif _norm(actual.get(key)) != _norm(want):
            problems.append(f"{key}: expected {want!r}, got {actual.get(key)!r}")
    return problems


def _filter_matches(got: Any, want: Any) -> bool:
    if isinstance(want, dict) and "one_of" in want:
        return any(_as_set(got) == _as_set(option) for option in want["one_of"])
    if isinstance(want, dict) and "excludes" in want:
        return bool(got) and not _as_set(got) & _as_set(want["excludes"])
    return _as_set(got) == _as_set(want)


def _as_set(value: Any) -> Any:
    value = _norm(value)
    return frozenset(value) if isinstance(value, list) else value


async def plan_like_the_service(planner: Planner, request: QueryRequest) -> QueryPlan:
    """The service's own planning stage (split, plan, gate, repair), without retrieving any trials."""
    return (await plan_question(planner, request, None, None, set())).plan


async def main(args: list[str]) -> int:
    heldout = "--heldout" in args
    selected = [a for a in args if a != "--heldout"]
    settings = get_settings()
    planner = build_planner(settings.planner_primary, settings.planner_fallback)
    if isinstance(planner, UnconfiguredPlanner):
        print(planner.reason)
        return 2
    cases = [c for c in load_cases(HELDOUT if heldout else QUESTIONS) if not selected or c["id"] in selected]
    by_family: dict[str, list[bool]] = defaultdict(list)
    for case in cases:
        request = QueryRequest(query=case["question"], **case.get("fields", {}))
        try:
            plan = await plan_like_the_service(planner, request)
            problems = mismatches(plan, case["expect"])
        except Exception as exc:  # a failed call is a failed case, not a crashed eval
            problems = [f"error: {exc}"]
        by_family[case["family"]].append(not problems)
        print(f"{'PASS' if not problems else 'FAIL'}  {case['id']:<16} {case['question']}")
        for p in problems:
            print(f"        {p}")
    passed = sum(sum(v) for v in by_family.values())
    label = "held-out " if heldout else ""
    print(f"\n{passed}/{len(cases)} {label}plans correct ({passed / len(cases):.0%}) using {settings.planner_primary}")
    for family, results in sorted(by_family.items()):
        print(f"  {family:<15} {sum(results)}/{len(results)}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
