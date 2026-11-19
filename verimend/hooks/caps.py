"""Spend, step and attempt caps as plain functions.

Two layers:
- a run stops when it reaches its dollar cap, its step cap or its attempt cap;
- a batch (any loop of paid runs) refuses to start without a limit and a dollar cap, and
  never hands a run more money than the batch has left.
"""

from dataclasses import dataclass


class CapError(RuntimeError):
    """A paid loop was started without the caps it needs, or with unusable ones."""


@dataclass(frozen=True)
class RunCaps:
    max_usd: float
    max_steps: int
    max_attempts: int


def run_stop_reason(
    caps: RunCaps, *, spent_usd: float, steps: int, attempts: int = 0
) -> str | None:
    """Why the run must stop now, or None if it may continue.

    A cap is reached when the value equals it: step 40 of 40 is the last one allowed.
    """
    if spent_usd >= caps.max_usd:
        return f"dollar cap reached: ${spent_usd:.4f} of ${caps.max_usd:.2f}"
    if steps >= caps.max_steps:
        return f"step cap reached: {steps} of {caps.max_steps}"
    if attempts >= caps.max_attempts:
        return f"attempt cap reached: {attempts} of {caps.max_attempts}"
    return None


def require_batch_caps(limit: int | None, max_usd: float | None) -> tuple[int, float]:
    """CLAUDE.md rule 1: no paid loop without --limit and --max-usd."""
    if limit is None or max_usd is None:
        raise CapError("A paid batch needs both --limit and --max-usd.")
    if limit < 1:
        raise CapError(f"--limit must be at least 1, got {limit}.")
    if max_usd <= 0:
        raise CapError(f"--max-usd must be greater than 0, got {max_usd}.")
    return limit, max_usd


def estimate_batch_usd(runs: int, estimated_usd_per_run: float) -> float:
    return runs * estimated_usd_per_run


@dataclass
class BatchBudget:
    """Tracks what a batch has spent and decides what the next run may spend."""

    max_usd: float
    spent_usd: float = 0.0

    @property
    def remaining_usd(self) -> float:
        return max(self.max_usd - self.spent_usd, 0.0)

    def next_run_cap(self, per_run_usd: float) -> float | None:
        """Dollar cap for the next run, or None when the batch must stop.

        The cap is the smaller of the per-run cap and what is left, so the batch total
        cannot pass its own cap even if every run spends all it is allowed.
        """
        if self.remaining_usd <= 0:
            return None
        return min(per_run_usd, self.remaining_usd)

    def record(self, cost_usd: float) -> None:
        if cost_usd < 0:
            raise ValueError(f"cost cannot be negative, got {cost_usd}")
        self.spent_usd += cost_usd
