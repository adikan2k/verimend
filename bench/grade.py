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
