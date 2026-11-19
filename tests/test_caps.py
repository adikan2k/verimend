import pytest

from verimend.hooks.caps import (
    BatchBudget,
    CapError,
    RunCaps,
    estimate_batch_usd,
    require_batch_caps,
    run_stop_reason,
)

CAPS = RunCaps(max_usd=0.25, max_steps=40, max_attempts=3)


def test_run_continues_below_every_cap() -> None:
    assert run_stop_reason(CAPS, spent_usd=0.24, steps=39, attempts=2) is None


def test_run_stops_at_the_dollar_cap() -> None:
    reason = run_stop_reason(CAPS, spent_usd=0.25, steps=1)
    assert reason is not None and reason.startswith("dollar cap reached")


def test_run_stops_at_the_step_cap() -> None:
    reason = run_stop_reason(CAPS, spent_usd=0.01, steps=40)
    assert reason is not None and reason.startswith("step cap reached")


def test_run_stops_at_the_attempt_cap() -> None:
    reason = run_stop_reason(CAPS, spent_usd=0.01, steps=5, attempts=3)
    assert reason is not None and reason.startswith("attempt cap reached")


@pytest.mark.parametrize(
    ("limit", "max_usd"),
    [(None, 1.0), (3, None), (None, None), (0, 1.0), (3, 0.0), (3, -1.0)],
)
def test_batch_refuses_to_start_without_usable_caps(
    limit: int | None, max_usd: float | None
) -> None:
    with pytest.raises(CapError):
        require_batch_caps(limit, max_usd)


def test_batch_accepts_usable_caps() -> None:
    assert require_batch_caps(3, 0.75) == (3, 0.75)


def test_estimate_is_runs_times_cost() -> None:
    assert estimate_batch_usd(3, 0.10) == pytest.approx(0.30)


def test_batch_never_hands_out_more_than_it_has_left() -> None:
    budget = BatchBudget(max_usd=0.60)
    assert budget.next_run_cap(0.25) == pytest.approx(0.25)
    budget.record(0.25)
    budget.record(0.25)
    assert budget.next_run_cap(0.25) == pytest.approx(0.10)


def test_batch_stops_when_the_cap_is_reached() -> None:
    budget = BatchBudget(max_usd=0.50)
    budget.record(0.25)
    budget.record(0.27)  # a run that overshot its own cap
    assert budget.remaining_usd == 0
    assert budget.next_run_cap(0.25) is None


def test_batch_rejects_a_negative_cost() -> None:
    with pytest.raises(ValueError):
        BatchBudget(max_usd=1.0).record(-0.01)
