"""bench/assemble.py: records and grades into traces and summary.json."""

import json
from pathlib import Path

from bench.agents import get_agent
from bench.assemble import AgentRecord, write_batch
from bench.grade import Grade
from bench.trace import read_trace
from verimend.runrecord import AgentRun

META = {"batch": "b", "model": "claude-haiku-5-5", "git_sha": "abc", "max_steps": 40}


def stream(final: str, cost: float | None) -> str:
    lines = [
        {"type": "system", "subtype": "init", "model": "m", "tools": ["Bash"]},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "usage": {"input_tokens": 10},
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            },
        },
    ]
    if cost is not None:
        lines.append({"type": "result", "subtype": "success", "num_turns": 2,
                      "total_cost_usd": cost, "result": final})  # fmt: skip
    return "\n".join(json.dumps(x) for x in lines)


def grade(task: str, trial: int, passed: bool) -> Grade:
    return Grade(task_id=task, trial=trial, passed=passed, grader="ade-bench", tests={"t": "x"})


def test_reference_runs_claim_fixed_and_cost_nothing(tmp_path: Path) -> None:
    records = [AgentRecord("a", 1, started=100.0, seconds=5.0)]
    summary = write_batch(tmp_path, records, [grade("a", 1, True)], get_agent("reference"), META)
    (only,) = summary["runs"]
    assert only["claim"] == "fixed" and only["outcome"] == "true_fix"
    assert only["dollars"] == 0.0
    assert summary["metrics"]["grader_pass_rate"] == 1.0
    events = read_trace(tmp_path / "a" / "1.jsonl")
    assert [e["event"] for e in events] == ["run_start", "report", "grade", "run_end"]
    assert events[0]["model"] is None  # no model calls, so no model recorded
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["agent"] == (
        "reference"
    )


def test_baseline_claim_cost_and_false_success(tmp_path: Path) -> None:
    records = [
        AgentRecord("a", 1, 0.0, 30.0, run_cap_usd=0.25, stream_log=stream("VERDICT: fixed", 0.02)),
        AgentRecord("b", 1, 0.0, 30.0, run_cap_usd=0.25, stream_log=stream("", None)),
    ]
    grades = [grade("a", 1, False), grade("b", 1, False)]
    summary = write_batch(tmp_path, records, grades, get_agent("baseline"), META)
    a, b = summary["runs"]
    assert a["claim"] == "fixed" and a["outcome"] == "false_success" and a["dollars"] == 0.02
    # No result line: the whole run cap is charged and the claim is none.
    assert b["claim"] == "none" and b["dollars"] == 0.25 and b["stopped"] == "no_result"
    trace = read_trace(tmp_path / "a" / "1.jsonl")
    names = [e["event"] for e in trace]
    assert names == ["run_start", "tool_call", "model_usage", "report", "grade", "run_end"]
    assert trace[0]["model"] == "claude-haiku-5-5"
    assert trace[-1]["cost_source"] == "result_line"
    assert summary["metrics"]["false_success_rate"] == 0.5


def test_missing_grade_and_batch_cap_stop(tmp_path: Path) -> None:
    records = [
        AgentRecord("a", 1, 0.0, 1.0),
        AgentRecord("b", 1, 0.0, None, stopped="batch_cap"),
    ]
    summary = write_batch(tmp_path, records, [], get_agent("baseline"), META, include_tests=False)
    a, b = summary["runs"]
    assert a["grader_passed"] is False
    assert b["stopped"] == "batch_cap" and b["dollars"] == 0.0
    assert summary["metrics"]["not_started"] == 1
    grade_event = next(e for e in read_trace(tmp_path / "a" / "1.jsonl") if e["event"] == "grade")
    assert grade_event["failure_mode"] == "missing"


def test_trials_the_agent_never_reached_are_reported_as_failures(tmp_path: Path) -> None:
    records = [AgentRecord("a", 1, 0.0, 1.0)]
    grades = [grade("a", 1, True), grade("b", 1, False)]
    summary = write_batch(
        tmp_path, records, grades, get_agent("reference"), META,
        expected=[("a", 1), ("b", 1), ("c", 1)],
    )  # fmt: skip
    runs = {r["task"]: r for r in summary["runs"]}
    assert set(runs) == {"a", "b", "c"}
    for task in ("b", "c"):
        # The reference agent never ran, so it claims nothing: no false success.
        assert runs[task]["stopped"] == "harness_error"
        assert runs[task]["claim"] == "none" and runs[task]["grader_passed"] is False
    assert summary["metrics"]["grader_pass_rate"] == 1 / 3


def test_traces_can_leave_test_names_out(tmp_path: Path) -> None:
    write_batch(
        tmp_path,
        [AgentRecord("a", 1, 0.0, 1.0)],
        [grade("a", 1, True)],
        get_agent("none"),
        META,
        include_tests=False,
    )
    text = (tmp_path / "a" / "1.jsonl").read_text(encoding="utf-8")
    assert '"tests"' not in text


def verimend_run(verdict: str | None, cost: float | None, **extra: object) -> AgentRun:
    report = None if verdict is None else {"verdict": verdict, "root_cause": {}}
    run = AgentRun(model="claude-haiku-5-5", prompt_sha="sha", report=report, cost_usd=cost,
                   num_turns=9, subtype="success", seconds=12.0, final_verdict=verdict,
                   prompt_tokens={"m1": 1000, "m2": 3000})  # fmt: skip
    run.events = [
        {"event": "tool_call", "t": 5.0, "name": "read_failure", "arguments": {},
         "duration_s": 3.0, "output_chars": 120, "is_error": False},
        {"event": "hook_block", "t": 6.0, "tool": "edit_model", "reason": "tests/"},
        {"event": "tool_call", "t": 7.0, "name": "report", "arguments": {},
         "duration_s": 0.0, "output_chars": 40, "is_error": False},
    ]  # fmt: skip
    for key, value in extra.items():
        setattr(run, key, value)
    return run


def test_verimend_trace_claim_and_cost_come_from_its_record(tmp_path: Path) -> None:
    records = [
        AgentRecord("a", 1, 1.0, 20.0, run_cap_usd=0.25, agent_run=verimend_run("fixed", 0.01)),
        AgentRecord("b", 1, 1.0, 20.0, run_cap_usd=0.25, agent_run=verimend_run("fixed", 0.02)),
        AgentRecord("c", 1, 1.0, 20.0, run_cap_usd=0.25, agent_run=verimend_run(None, None,
                    subtype=None, error="CLIConnectionError: gone")),  # fmt: skip
        AgentRecord("d", 1, 1.0, 20.0, run_cap_usd=0.25,
                    agent_run=verimend_run("escalate", 0.03, subtype="error_max_turns")),
    ]  # fmt: skip
    grades = [grade("a", 1, True), grade("b", 1, False), grade("c", 1, False),
              grade("d", 1, False)]  # fmt: skip
    summary = write_batch(tmp_path, records, grades, get_agent("verimend"), META)
    a, b, c, d = summary["runs"]
    assert (a["claim"], a["outcome"], a["dollars"]) == ("fixed", "true_fix", 0.01)
    assert (b["claim"], b["outcome"]) == ("fixed", "false_success")
    # No report and no result: claim none, whole cap charged, stopped says why.
    assert (c["claim"], c["dollars"], c["stopped"]) == ("none", 0.25, "agent_error")
    assert (d["claim"], d["stopped"]) == ("escalate", "error_max_turns")
    assert a["hook_blocks"] == 1 and a["tool_calls"] == 2 and a["largest_prompt_tokens"] == 3000
    events = read_trace(tmp_path / "a" / "1.jsonl")
    names = [e["event"] for e in events]
    assert names[0] == "run_start" and names[-1] == "run_end"
    assert names.count("tool_call") == 2 and names.count("hook_block") == 1
    assert names.count("model_usage") == 2
    assert events[0]["prompt_sha"] == "sha"
    (report,) = [e for e in events if e["event"] == "report"]
    assert report["verdict"]["verdict"] == "fixed"


# --- the verifier loop (phase 4) ---------------------------------------------------------


def attempt(n: int, passed: bool, check: str | None = None) -> dict:
    return {"attempt": n, "passed": passed, "failed_check": check, "duration_s": 3.0,
            "checks": [{"name": check or "collateral", "passed": passed, "detail": "d"}],
            "diff": "--- a/x\n+++ b/x\n"}  # fmt: skip


def test_verimend_claim_is_the_final_verdict_after_the_verifier(tmp_path: Path) -> None:
    rejected = [attempt(1, False, "masking"), attempt(2, False, "clean_rebuild"),
                attempt(3, False, "tamper")]  # fmt: skip
    records = [
        # Rejected three times: the agent said fixed, the run says not fixed. Caught.
        AgentRecord("a", 1, 1.0, 20.0, run_cap_usd=0.25,
                    agent_run=verimend_run("not_fixed", 0.02, attempts=rejected,
                                           report={"verdict": "fixed"})),
        # Passed on the second attempt, the grader still fails it: a false success.
        AgentRecord("b", 1, 1.0, 20.0, run_cap_usd=0.25,
                    agent_run=verimend_run("fixed", 0.02,
                                           attempts=[attempt(1, False, "masking"),
                                                     attempt(2, True)])),
        # The verifier could not run: a harness problem, recorded as such.
        AgentRecord("c", 1, 1.0, 20.0, run_cap_usd=0.25,
                    agent_run=verimend_run(None, 0.01, verifier_error="snapshot failed: x")),
    ]  # fmt: skip
    grades = [grade("a", 1, False), grade("b", 1, False), grade("c", 1, False)]
    meta = {**META, "max_attempts": 3}
    summary = write_batch(tmp_path, records, grades, get_agent("verimend"), meta)
    a, b, c = summary["runs"]
    assert (a["claim"], a["outcome"], a["attempts"]) == ("not_fixed", "caught_by_verifier", 3)
    assert a["verifier_passed"] is False and a["failed_check"] == "tamper"
    assert (b["claim"], b["outcome"], b["attempts"]) == ("fixed", "false_success", 2)
    assert b["verifier_passed"] is True
    assert (c["claim"], c["stopped"], c["attempts"]) == ("none", "verifier_error", 0)
    metrics = summary["metrics"]
    assert metrics["outcomes"]["caught_by_verifier"] == 1
    expected = {"runs_checked": 2, "last_attempt_passed": 1, "mean_attempts": 2.5,
                "failed_checks": {"tamper": 1}}  # fmt: skip
    assert metrics["verifier"] == expected
    events = read_trace(tmp_path / "a" / "1.jsonl")
    ends = [e for e in events if e["event"] == "attempt_end"]
    assert [e["failed_check"] for e in ends] == ["masking", "clean_rebuild", "tamper"]
    assert events[0]["caps"]["max_attempts"] == 3
    assert events[-1]["event"] == "run_end" and events[-1]["outcome"] == "caught_by_verifier"


def test_free_agents_carry_the_verifiers_decision(tmp_path: Path) -> None:
    checked = {"snapshot": {"ok": True, "base_failures": [], "nodes": 5, "duration_s": 2.0},
               "attempts": [attempt(1, True)], "error": None}  # fmt: skip
    empty = {"snapshot": checked["snapshot"], "attempts": [attempt(1, False, "empty_diff")],
             "error": None}  # fmt: skip
    records = [AgentRecord("a", 1, 1.0, 5.0, verifier=checked)]
    summary = write_batch(tmp_path, records, [grade("a", 1, True)], get_agent("reference"), META)
    (run,) = summary["runs"]
    assert run["claim"] == "fixed" and run["verifier_passed"] is True and run["attempts"] == 1
    events = read_trace(tmp_path / "a" / "1.jsonl")
    assert events[0]["verifier_snapshot"]["nodes"] == 5
    assert [e["event"] for e in events].count("attempt_end") == 1
    nothing = write_batch(tmp_path / "n", [AgentRecord("a", 1, 1.0, 5.0, verifier=empty)],
                          [grade("a", 1, False)], get_agent("none"), META)  # fmt: skip
    assert nothing["metrics"]["verifier"]["failed_checks"] == {"empty_diff": 1}
