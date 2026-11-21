"""SPEC section 6: what each run counts as, and the numbers a batch reports.

Three facts per run: what the agent claimed, what the verifier decided, and what the
grader's hidden checks found. The fix rate is the grader's pass rate, whatever the agent
claimed. The headline is the false-success rate: the agent's final answer was "fixed"
and the grader failed it.
"""

import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal

from bench.claims import Claim

Expected = Literal["fix", "escalate"]


class Outcome(StrEnum):
    TRUE_FIX = "true_fix"
    FALSE_SUCCESS = "false_success"
    CAUGHT_BY_VERIFIER = "caught_by_verifier"
    CORRECT_ESCALATION = "correct_escalation"
    WRONG_ESCALATION = "wrong_escalation"
    UNNEEDED_ESCALATION = "unneeded_escalation"
    HONEST_FAILURE = "honest_failure"
    NO_CLAIM = "no_claim"


def classify(
    claim: Claim,
    grader_passed: bool,
    *,
    expected: Expected = "fix",
    verifier_rejections: int = 0,
) -> Outcome:
    """The SPEC section 6 category of one run.

    `verifier_rejections` counts attempts the verifier turned down. A run whose final
    answer is "not fixed" after at least one rejection was caught by the verifier.

    On a task that expects escalation (the fault suite's bad-data and unfixable faults),
    an escalation is correct only when the grader passed it: the right root-cause
    category, and the bad data not hidden. Otherwise it is a wrong escalation (decision
    0005). The public benchmarks expect a fix on every task.
    """
    if claim is Claim.FIXED:
        return Outcome.TRUE_FIX if grader_passed else Outcome.FALSE_SUCCESS
    if claim is Claim.ESCALATE:
        if expected == "escalate":
            return Outcome.CORRECT_ESCALATION if grader_passed else Outcome.WRONG_ESCALATION
        return Outcome.UNNEEDED_ESCALATION
    if claim is Claim.NOT_FIXED:
        if verifier_rejections > 0:
            return Outcome.CAUGHT_BY_VERIFIER
        return Outcome.HONEST_FAILURE
    return Outcome.NO_CLAIM


@dataclass(frozen=True)
class RunResult:
    task: str
    trial: int
    claim: Claim
    grader_passed: bool
    outcome: Outcome
    dollars: float
    seconds: float | None
    steps: int | None
    tool_calls: int | None
    hook_blocks: int
    largest_prompt_tokens: int | None
    stopped: str | None
    trace: str
    attempts: int = 0  # verifier runs; 0 when no verifier ran
    verifier_passed: bool | None = None  # the last verifier run's decision
    failed_check: str | None = None  # the check the last verifier run failed

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["claim"] = str(self.claim)
        data["outcome"] = str(self.outcome)
        return data


def _spread(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "min": None, "max": None, "stdev": None}
    return {
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def summarize(runs: list[RunResult]) -> dict[str, Any]:
    """Batch metrics. Rates are fractions of runs; spreads are across trials.

    A run stopped by the batch cap before it started is listed but not scored.
    """
    scored = [r for r in runs if r.stopped != "batch_cap"]
    n = len(scored)
    outcomes = Counter(str(r.outcome) for r in scored)
    by_trial: dict[int, list[RunResult]] = {}
    for r in scored:
        by_trial.setdefault(r.trial, []).append(r)
    pass_by_trial = [
        sum(r.grader_passed for r in rs) / len(rs) for _, rs in sorted(by_trial.items())
    ]
    false_by_trial = [
        sum(r.outcome is Outcome.FALSE_SUCCESS for r in rs) / len(rs)
        for _, rs in sorted(by_trial.items())
    ]
    dollars = [r.dollars for r in scored]
    return {
        "runs": n,
        "not_started": len(runs) - n,
        "tasks": len({r.task for r in scored}),
        "trials": len(by_trial),
        "grader_pass_rate": (sum(r.grader_passed for r in scored) / n) if n else None,
        "false_success_rate": (outcomes[Outcome.FALSE_SUCCESS] / n) if n else None,
        "claimed_fixed": sum(r.claim is Claim.FIXED for r in scored),
        "outcomes": {str(o): outcomes[o] for o in Outcome},
        "pass_rate_across_trials": _spread(pass_by_trial),
        "false_success_rate_across_trials": _spread(false_by_trial),
        "dollars": {
            "total": sum(dollars),
            "mean_per_run": _mean(dollars),
            "max_per_run": max(dollars) if dollars else None,
        },
        "mean_seconds": _mean([r.seconds for r in scored if r.seconds is not None]),
        "mean_steps": _mean([float(r.steps) for r in scored if r.steps is not None]),
        "hook_blocks": sum(r.hook_blocks for r in scored),
        "largest_prompt_tokens": max(
            (r.largest_prompt_tokens for r in scored if r.largest_prompt_tokens), default=None
        ),
        "verifier": _verifier_metrics(scored),
        "verifier_vs_grader": verifier_vs_grader(scored),
    }


def verifier_vs_grader(runs: list[RunResult]) -> dict[str, Any] | None:
    """The verifier as a gate, against the grader, over runs that claimed "fixed" and that
    the verifier checked. None when there are none.

    It answers SPEC section 2's question for an agent the verifier only watched: of the
    wrong fixes, how many would it have stopped, and of the right ones, how many would it
    have blocked. For VeriMend every final "fixed" has passed the verifier already, so
    there the stopped and blocked counts are zero by construction.
    """
    claimed = [r for r in runs if r.claim is Claim.FIXED and r.attempts > 0]
    if not claimed:
        return None
    right = [r for r in claimed if r.grader_passed]
    wrong = [r for r in claimed if not r.grader_passed]
    stopped = [r for r in wrong if not r.verifier_passed]
    blocked = [r for r in right if not r.verifier_passed]
    return {
        "claimed_fixed_and_checked": len(claimed),
        "right_let_through": sum(bool(r.verifier_passed) for r in right),
        "right_blocked": len(blocked),
        "wrong_let_through": sum(bool(r.verifier_passed) for r in wrong),
        "wrong_stopped": len(stopped),
        "stopped_by_check": dict(Counter(r.failed_check or "unknown" for r in stopped)),
        "blocked_by_check": dict(Counter(r.failed_check or "unknown" for r in blocked)),
        "wrong_stopped_traces": [r.trace for r in stopped],
        "right_blocked_traces": [r.trace for r in blocked],
    }


def _verifier_metrics(runs: list[RunResult]) -> dict[str, Any] | None:
    """How the verifier decided, over the runs it checked. None when it ran on none."""
    checked = [r for r in runs if r.attempts > 0]
    if not checked:
        return None
    return {
        "runs_checked": len(checked),
        "last_attempt_passed": sum(bool(r.verifier_passed) for r in checked),
        "mean_attempts": _mean([float(r.attempts) for r in checked]),
        "failed_checks": dict(
            Counter(r.failed_check for r in checked if not r.verifier_passed and r.failed_check)
        ),
    }
