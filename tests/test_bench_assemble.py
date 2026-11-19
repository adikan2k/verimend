"""bench/assemble.py: records and grades into traces and summary.json."""

import json
from pathlib import Path

from bench.agents import get_agent
from bench.assemble import AgentRecord, write_batch
from bench.grade import Grade
from bench.trace import read_trace

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
