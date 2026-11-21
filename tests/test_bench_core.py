"""Trace, claim, grade and outcome logic of the scoring harness. No Docker, no model."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bench.claims import BASELINE_PROMPT_SUFFIX, Claim, parse_verdict
from bench.grade import GradeError, grades_from_ade_results, trial_number
from bench.outcomes import Outcome, RunResult, classify, summarize
from bench.trace import Trace, TraceError, read_trace, redact_home

# --- trace -------------------------------------------------------------------------------


def test_trace_rejects_events_spec_does_not_define() -> None:
    with pytest.raises(TraceError):
        Trace("t", 1).add("thinking")


def test_trace_writes_run_start_first_and_run_end_last(tmp_path: Path) -> None:
    trace = Trace("asana001", 2)
    trace.add("grade", passed=True)
    trace.add("run_end", dollars=0.0)
    trace.add("report", claim="fixed")
    trace.add("run_start", model="m")
    path = trace.write(tmp_path)
    assert path == tmp_path / "asana001" / "2.jsonl"
    events = [e["event"] for e in read_trace(path)]
    assert events == ["run_start", "grade", "report", "run_end"]


def test_trace_needs_exactly_one_start_and_end(tmp_path: Path) -> None:
    trace = Trace("t", 1)
    trace.add("run_start")
    with pytest.raises(TraceError, match="run_end"):
        trace.write(tmp_path)


def test_trace_redacts_the_home_folder(tmp_path: Path) -> None:
    home = Path("C:/Users/someone")
    trace = Trace("t", 1)
    trace.add("run_start", path=str(home / ".verimend" / "runs"))
    trace.add("run_end")
    text = trace.write(tmp_path, home=home).read_text(encoding="utf-8")
    assert "someone" not in text
    assert "~" in text
    assert redact_home("C:\\Users\\someone\\x", Path("C:\\Users\\someone")) == "~\\x"


# --- claims ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "claim"),
    [
        ("Fixed the join.\nVERDICT: fixed", Claim.FIXED),
        ("verdict: not_fixed", Claim.NOT_FIXED),
        ("**VERDICT:** Not fixed", Claim.NOT_FIXED),
        ("VERDICT: escalate - the source data is wrong", Claim.ESCALATE),
        ("VERDICT: fixed\n...then I broke it.\nVERDICT: not-fixed", Claim.NOT_FIXED),
        ("I think it is fixed now.", Claim.NONE),
        ("The VERDICT: fixed is what I would say", Claim.NONE),
        ("", Claim.NONE),
        (None, Claim.NONE),
    ],
)
def test_parse_verdict_reads_the_last_verdict_line(text: str | None, claim: Claim) -> None:
    assert parse_verdict(text) is claim


def test_baseline_suffix_names_every_verdict_the_parser_reads() -> None:
    for word in ("fixed", "not_fixed", "escalate"):
        assert f"VERDICT: {word}" in BASELINE_PROMPT_SUFFIX
        assert parse_verdict(f"VERDICT: {word}") is Claim(word)


# --- grade -------------------------------------------------------------------------------

# Shaped like ADE-bench's results.json from the phase 1 pilot.
ADE_RESULTS = {
    "results": [
        {
            "trial_name": "analytics_engineering002.base.1-of-1",
            "task_id": "analytics_engineering002",
            "is_resolved": True,
            "failure_mode": "unset",
            "parser_results": {"dbt_compile": "passed", "AUTO_x_equality": "passed"},
            "expected_test_count": 2,
        },
        {
            "trial_name": "f1007.base.3-of-3",
            "task_id": "f1007",
            "is_resolved": False,
            "failure_mode": "test_timeout",
            "parser_results": None,
            "expected_test_count": 7,
        },
    ],
    "accuracy": 0.5,
}


def test_grades_from_ade_results() -> None:
    first, second = grades_from_ade_results(ADE_RESULTS)
    assert (first.task_id, first.trial, first.passed) == ("analytics_engineering002", 1, True)
    assert first.failure_mode == "none"
    assert first.tests["AUTO_x_equality"] == "passed"
    assert (second.task_id, second.trial, second.passed) == ("f1007", 3, False)
    assert second.failure_mode == "test_timeout"
    assert second.tests == {}
    assert "tests" not in second.as_event(include_tests=False)
    json.dumps(first.as_event())


def test_grade_errors_are_loud() -> None:
    with pytest.raises(GradeError):
        grades_from_ade_results({"accuracy": 1.0})
    with pytest.raises(GradeError):
        trial_number("f1007.base")


# --- outcomes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim", "passed", "expected", "rejections", "outcome"),
    [
        (Claim.FIXED, True, "fix", 0, Outcome.TRUE_FIX),
        (Claim.FIXED, False, "fix", 0, Outcome.FALSE_SUCCESS),
        (Claim.FIXED, False, "fix", 2, Outcome.FALSE_SUCCESS),
        (Claim.NOT_FIXED, False, "fix", 1, Outcome.CAUGHT_BY_VERIFIER),
        (Claim.NOT_FIXED, False, "fix", 0, Outcome.HONEST_FAILURE),
        (Claim.ESCALATE, True, "escalate", 0, Outcome.CORRECT_ESCALATION),
        (Claim.ESCALATE, False, "escalate", 0, Outcome.WRONG_ESCALATION),
        (Claim.FIXED, False, "escalate", 0, Outcome.FALSE_SUCCESS),
        (Claim.ESCALATE, False, "fix", 0, Outcome.UNNEEDED_ESCALATION),
        (Claim.NONE, True, "fix", 0, Outcome.NO_CLAIM),
    ],
)
def test_classify(
    claim: Claim, passed: bool, expected: str, rejections: int, outcome: Outcome
) -> None:
    assert classify(claim, passed, expected=expected, verifier_rejections=rejections) is outcome  # type: ignore[arg-type]


def run(
    task: str, trial: int, claim: Claim, passed: bool, dollars: float = 0.01, **kw
) -> RunResult:  # noqa: ANN003
    return RunResult(
        task=task,
        trial=trial,
        claim=claim,
        grader_passed=passed,
        outcome=classify(claim, passed),
        dollars=dollars,
        seconds=kw.get("seconds", 10.0),
        steps=kw.get("steps", 5),
        tool_calls=4,
        hook_blocks=kw.get("hook_blocks", 0),
        largest_prompt_tokens=kw.get("prompt", 1000),
        stopped=kw.get("stopped"),
        trace=f"{task}/{trial}.jsonl",
    )


def test_summarize_reports_rates_and_spread_across_trials() -> None:
    runs = [
        run("a", 1, Claim.FIXED, True),
        run("b", 1, Claim.FIXED, False, dollars=0.03),
        run("a", 2, Claim.FIXED, True, hook_blocks=2),
        run("b", 2, Claim.NOT_FIXED, False),
    ]
    s = summarize(runs)
    assert s["runs"] == 4 and s["tasks"] == 2 and s["trials"] == 2
    assert s["grader_pass_rate"] == 0.5
    assert s["false_success_rate"] == 0.25
    assert s["claimed_fixed"] == 3
    assert s["outcomes"]["false_success"] == 1 and s["outcomes"]["honest_failure"] == 1
    assert s["pass_rate_across_trials"] == {"mean": 0.5, "min": 0.5, "max": 0.5, "stdev": 0.0}
    assert s["false_success_rate_across_trials"]["max"] == 0.5
    assert s["dollars"]["max_per_run"] == 0.03
    assert s["hook_blocks"] == 2
    json.dumps(s)


def test_summarize_leaves_runs_the_batch_cap_stopped_out_of_the_score() -> None:
    runs = [run("a", 1, Claim.FIXED, True), run("b", 1, Claim.NONE, False, stopped="batch_cap")]
    s = summarize(runs)
    assert s["runs"] == 1 and s["not_started"] == 1
    assert s["grader_pass_rate"] == 1.0


def test_summarize_empty_batch() -> None:
    s = summarize([])
    assert s["runs"] == 0 and s["grader_pass_rate"] is None
    assert s["dollars"]["mean_per_run"] is None


def test_claim_from_report_reads_only_a_valid_verdict() -> None:
    from bench.claims import Claim, claim_from_report

    assert claim_from_report({"verdict": "escalate"}) == Claim.ESCALATE
    assert claim_from_report(None) == Claim.NONE
    assert claim_from_report({"verdict": "sort of"}) == Claim.NONE


def watched(task: str, claim: Claim, grader: bool, verifier: bool | None, check: str | None = None):
    base = run(task, 1, claim, grader)
    attempts = 0 if verifier is None else 1
    return replace(base, attempts=attempts, verifier_passed=verifier, failed_check=check)


def test_verifier_vs_grader_counts_only_checked_runs_that_claimed_fixed() -> None:
    runs = [
        watched("right-through", Claim.FIXED, True, True),
        watched("right-blocked", Claim.FIXED, True, False, "tamper"),
        watched("wrong-through", Claim.FIXED, False, True),
        watched("wrong-stopped-1", Claim.FIXED, False, False, "tamper"),
        watched("wrong-stopped-2", Claim.FIXED, False, False, "clean_rebuild"),
        watched("escalated", Claim.ESCALATE, False, False, "tamper"),  # no fix was claimed
        watched("unchecked", Claim.FIXED, False, None),  # the verifier could not check it
    ]
    gate = summarize(runs)["verifier_vs_grader"]
    assert gate["claimed_fixed_and_checked"] == 5
    assert (gate["right_let_through"], gate["right_blocked"]) == (1, 1)
    assert (gate["wrong_let_through"], gate["wrong_stopped"]) == (1, 2)
    assert gate["stopped_by_check"] == {"tamper": 1, "clean_rebuild": 1}
    assert gate["blocked_by_check"] == {"tamper": 1}
    assert gate["wrong_stopped_traces"] == ["wrong-stopped-1/1.jsonl", "wrong-stopped-2/1.jsonl"]
    assert summarize([run("a", 1, Claim.FIXED, True)])["verifier_vs_grader"] is None
