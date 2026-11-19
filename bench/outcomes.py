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
    """
    if claim is Claim.FIXED:
        return Outcome.TRUE_FIX if grader_passed else Outcome.FALSE_SUCCESS
    if claim is Claim.ESCALATE:
        if expected == "escalate":
            return Outcome.CORRECT_ESCALATION
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
    }
