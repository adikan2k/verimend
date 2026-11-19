"""The grader's verdict on one trial, from the benchmark's own hidden checks.

Public tasks are graded by their benchmark's harness, unchanged; this module only reads
what that harness wrote. The fault suite (phase 5) adds its own grader behind the same
`Grade` shape.
"""

import re
from dataclasses import dataclass, field
from typing import Any

_TRIAL_SUFFIX = re.compile(r"\.(\d+)-of-(\d+)$")


class GradeError(ValueError):
    """A grader's output could not be read."""


@dataclass(frozen=True)
class Grade:
    task_id: str
    trial: int
    passed: bool
    grader: str
    failure_mode: str = "none"
    tests: dict[str, str] = field(default_factory=dict)
    expected_test_count: int | None = None

    def as_event(self, *, include_tests: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "grader": self.grader,
            "passed": self.passed,
            "failure_mode": self.failure_mode,
        }
        if include_tests:
            data["tests"] = self.tests
            data["expected_test_count"] = self.expected_test_count
        return data


def trial_number(trial_name: str) -> int:
    """`asana001.base.2-of-3` is trial 2."""
    match = _TRIAL_SUFFIX.search(trial_name)
    if not match:
        raise GradeError(f"cannot read a trial number from {trial_name!r}")
    return int(match.group(1))


def index_harbor_trials(trials: list[dict[str, Any]]) -> list[tuple[str, int, dict[str, Any]]]:
    """(task, trial number, result) for Harbor's per-trial result.json files.

    Harbor names trial folders with a random suffix, so trials of one task are numbered
    in the order they started.
    """
    by_task: dict[str, list[dict[str, Any]]] = {}
    for trial in trials:
        try:
            task = str(trial["task_id"]["path"]).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        except (KeyError, TypeError):
            raise GradeError("Harbor trial result has no task_id.path") from None
        by_task.setdefault(task, []).append(trial)
    indexed: list[tuple[str, int, dict[str, Any]]] = []
    for task, runs in sorted(by_task.items()):
        runs.sort(key=lambda r: str(r.get("started_at") or ""))
        indexed.extend((task, n, run) for n, run in enumerate(runs, start=1))
    return indexed


def grades_from_harbor_trials(trials: list[dict[str, Any]]) -> list[Grade]:
    """Grades from data-eng-bench's verifier, as Harbor records them.

    A trial passes only on a full reward of 1.0: the verifier scores row by row against
    the reference, and a partial score is not a fix.
    """
    grades: list[Grade] = []
    for task, number, trial in index_harbor_trials(trials):
        rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
        reward = rewards.get("reward")
        exception = (trial.get("exception_info") or {}).get("exception_type")
        valid = trial.get("reward_valid", True)
        grades.append(
            Grade(
                task_id=task,
                trial=number,
                passed=bool(valid and reward is not None and float(reward) >= 1.0),
                grader="data-eng-bench",
                failure_mode=str(exception) if exception else "none",
                tests={"reward": str(reward)},
            )
        )
    return grades


def grades_from_ade_results(results: dict[str, Any]) -> list[Grade]:
    """Grades from ADE-bench's results.json, or its BenchmarkResults dumped to a dict.

    A trial passes only if ADE-bench marked it resolved, which needs every test to pass.
    """
    try:
        trials = results["results"]
    except (KeyError, TypeError):
        raise GradeError("ADE-bench results have no 'results' list") from None
    grades: list[Grade] = []
    for trial in trials:
        tests = {str(k): str(v) for k, v in (trial.get("parser_results") or {}).items()}
        failure = str(trial.get("failure_mode") or "none")
        grades.append(
            Grade(
                task_id=str(trial["task_id"]),
                trial=trial_number(str(trial["trial_name"])),
                passed=bool(trial.get("is_resolved")),
                grader="ade-bench",
                failure_mode="none" if failure == "unset" else failure,
                tests=tests,
                expected_test_count=trial.get("expected_test_count"),
            )
        )
    return grades
