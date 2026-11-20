"""Turn what the harness saw into traces, scored runs and summary.json.

Pure functions: the ADE-bench adapter (bench/ade/inner.py) collects an `AgentRecord` per
trial and the grader's `Grade`, and everything after that happens here, under test.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.agents import AgentSpec
from bench.claims import Claim, claim_from_report, parse_verdict
from bench.claude_stream import parse_stream
from bench.grade import Grade
from bench.outcomes import RunResult, classify, summarize
from bench.trace import Trace
from verimend.runrecord import AgentRun

REPORT_TEXT_CHARS = 2_000


@dataclass
class AgentRecord:
    """One trial as the agent side saw it."""

    task: str
    trial: int
    started: float  # epoch seconds
    seconds: float | None = None
    run_cap_usd: float | None = None
    stopped: str | None = None  # "batch_cap" when the batch had no money left for it
    stream_log: str | None = None  # the baseline's Claude Code output
    agent_run: AgentRun | None = None  # VeriMend's own record of the run


def assemble_run(
    record: AgentRecord,
    grade: Grade | None,
    spec: AgentSpec,
    meta: dict[str, Any],
    *,
    include_tests: bool = True,
) -> tuple[Trace, RunResult]:
    if record.agent_run is not None:
        return _assemble_verimend(record, record.agent_run, grade, spec, meta, include_tests)
    trace = Trace(record.task, record.trial)
    stream = parse_stream(record.stream_log) if record.stream_log is not None else None
    t = record.started
    trace.add(
        "run_start",
        t=t,
        agent=spec.name,
        model=meta.get("model") if spec.model_calls else None,
        git_sha=meta.get("git_sha"),
        source=meta.get("source"),
        source_commit=meta.get("source_commit"),
        task=record.task,
        trial=record.trial,
        caps={"max_usd": record.run_cap_usd, "max_steps": meta.get("max_steps"), "max_attempts": 1},
        tools_offered=stream.tools_offered if stream else None,
    )

    if stream is not None:
        for call in stream.tool_calls:
            trace.add(
                "tool_call",
                t=t,
                name=call.name,
                arguments=call.arguments,
                duration_s=None,  # the stream does not time tool calls
                output_chars=call.output_chars,
                is_error=call.is_error,
            )
        for message_id, prompt_tokens in stream.prompt_tokens.items():
            trace.add("model_usage", t=t, message_id=message_id, prompt_tokens=prompt_tokens)

    fixed = spec.fixed_claim()
    if record.stopped in {"batch_cap", "harness_error"}:  # the agent never ran
        claim = Claim.NONE
    elif fixed is not None:
        claim = fixed
    else:
        claim = parse_verdict(stream.final_text if stream else None)
    text = stream.final_text[-REPORT_TEXT_CHARS:] if stream else None
    trace.add("report", t=t, claim=str(claim), text=text)

    passed = bool(grade and grade.passed)
    if grade is not None:
        trace.add("grade", t=t, **grade.as_event(include_tests=include_tests))
    else:
        trace.add("grade", t=t, grader=meta.get("grader"), passed=False, failure_mode="missing")

    dollars, cost_source = _cost(spec, record, stream)
    outcome = classify(claim, passed)
    stopped = record.stopped or (stream.stop_subtype if stream else None)
    tool_calls = len(stream.tool_calls) if stream else None
    largest = stream.largest_prompt_tokens if stream else None
    end = t + (record.seconds or 0.0)
    trace.add(
        "run_end",
        t=end,
        claim=str(claim),
        grader_passed=passed,
        outcome=str(outcome),
        dollars=dollars,
        cost_source=cost_source,
        seconds=record.seconds,
        steps=stream.num_turns if stream else None,
        tool_calls=tool_calls,
        hook_blocks=0,
        largest_prompt_tokens=largest,
        stopped=stopped,
        model_usage=stream.result.get("modelUsage") if stream and stream.result else None,
    )
    result = RunResult(
        task=record.task,
        trial=record.trial,
        claim=claim,
        grader_passed=passed,
        outcome=outcome,
        dollars=dollars,
        seconds=record.seconds,
        steps=stream.num_turns if stream else None,
        tool_calls=tool_calls,
        hook_blocks=0,
        largest_prompt_tokens=largest,
        stopped=stopped,
        trace=f"{record.task}/{record.trial}.jsonl",
    )
    return trace, result


def _assemble_verimend(
    record: AgentRecord,
    run: AgentRun,
    grade: Grade | None,
    spec: AgentSpec,
    meta: dict[str, Any],
    include_tests: bool,
) -> tuple[Trace, RunResult]:
    """VeriMend's trace: its own timed tool calls and hook blocks, and its report."""
    trace = Trace(record.task, record.trial)
    t = record.started
    trace.add(
        "run_start",
        t=t,
        agent=spec.name,
        model=run.model,
        git_sha=meta.get("git_sha"),
        source=meta.get("source"),
        source_commit=meta.get("source_commit"),
        task=record.task,
        trial=record.trial,
        caps={"max_usd": record.run_cap_usd, "max_steps": meta.get("max_steps"), "max_attempts": 1},
        prompt_sha=run.prompt_sha,
        tools_offered=run.tools_offered,
    )
    for event in sorted(run.events, key=lambda e: e.get("t", 0.0)):
        data = {k: v for k, v in event.items() if k not in {"event", "t"}}
        trace.add(event["event"], t=event.get("t", t), **data)
    for message_id, prompt_tokens in run.prompt_tokens.items():
        trace.add("model_usage", t=t, message_id=message_id, prompt_tokens=prompt_tokens)

    claim = claim_from_report(run.report)
    trace.add(
        "report",
        t=t + (run.seconds or 0.0),
        claim=str(claim),
        verdict=run.report,
        diffs=run.diffs,
        text=run.final_text[-REPORT_TEXT_CHARS:] or None,
    )
    passed = bool(grade and grade.passed)
    if grade is not None:
        trace.add("grade", t=t, **grade.as_event(include_tests=include_tests))
    else:
        trace.add("grade", t=t, grader=meta.get("grader"), passed=False, failure_mode="missing")

    if run.cost_usd is not None:
        dollars, cost_source = run.cost_usd, "result_line"
    else:  # no result: charge the whole cap, never assume it was cheaper
        dollars, cost_source = float(record.run_cap_usd or 0.0), "run_cap_assumed"
    outcome = classify(claim, passed)
    stopped = record.stopped or (run.subtype if run.subtype != "success" else None)
    if run.error:
        stopped = stopped or "agent_error"
    seconds = record.seconds if record.seconds is not None else run.seconds
    trace.add(
        "run_end",
        t=t + (seconds or 0.0),
        claim=str(claim),
        grader_passed=passed,
        outcome=str(outcome),
        dollars=dollars,
        cost_source=cost_source,
        seconds=seconds,
        steps=run.num_turns,
        tool_calls=run.tool_calls,
        hook_blocks=run.hook_blocks,
        largest_prompt_tokens=run.largest_prompt_tokens,
        stopped=stopped,
        error=run.error,
        model_usage=run.model_usage,
    )
    result = RunResult(
        task=record.task,
        trial=record.trial,
        claim=claim,
        grader_passed=passed,
        outcome=outcome,
        dollars=dollars,
        seconds=seconds,
        steps=run.num_turns,
        tool_calls=run.tool_calls,
        hook_blocks=run.hook_blocks,
        largest_prompt_tokens=run.largest_prompt_tokens,
        stopped=stopped,
        trace=f"{record.task}/{record.trial}.jsonl",
    )
    return trace, result


def _cost(spec: AgentSpec, record: AgentRecord, stream: Any) -> tuple[float, str]:
    """Dollars for the run and where the number came from.

    A paid run with no result line (it crashed or was killed) is charged its whole cap:
    the batch budget must never assume a run was cheaper than it may have been.
    """
    if not spec.model_calls or record.stopped == "batch_cap":
        return 0.0, "no_model_calls"
    cost = stream.cost_usd if stream is not None else None
    if cost is not None:
        return cost, "result_line"
    return float(record.run_cap_usd or 0.0), "run_cap_assumed"


def write_batch(
    results_dir: Path,
    records: list[AgentRecord],
    grades: list[Grade],
    spec: AgentSpec,
    meta: dict[str, Any],
    *,
    expected: list[tuple[str, int]] | None = None,
    include_tests: bool = True,
) -> dict[str, Any]:
    """Write every trace and summary.json; return the summary.

    Every expected (task, trial) gets a run, so a trial the harness failed before the
    agent started is reported as a failure instead of silently missing.
    """
    by_key = {(g.task_id, g.trial): g for g in grades}
    records = list(records)
    seen = {(r.task, r.trial) for r in records}
    for task, trial in sorted(set(expected or []) | set(by_key)):
        if (task, trial) not in seen:
            records.append(AgentRecord(task, trial, started=0.0, stopped="harness_error"))
    runs: list[RunResult] = []
    for record in sorted(records, key=lambda r: (r.task, r.trial)):
        grade = by_key.get((record.task, record.trial))
        trace, result = assemble_run(record, grade, spec, meta, include_tests=include_tests)
        trace.write(results_dir)
        runs.append(result)
    summary = {
        **meta,
        "agent": spec.name,
        "metrics": summarize(runs),
        "runs": [r.as_dict() for r in runs],
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", "utf-8")
    return summary
