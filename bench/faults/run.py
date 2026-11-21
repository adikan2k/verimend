"""Run an agent on the fault suite, on this machine: build, run, grade, record.

Called by `python -m bench.run --source faults --set all ...` once the caps are checked.
For each fault and trial:

    ~/.verimend/runs/<batch>/<fault>/<trial>/fault/    the built fault (lib.materialize)
    ~/.verimend/runs/<batch>/<fault>/<trial>/profiles/ a profile pointed at its database
    ~/.verimend/runs/<batch>/<fault>/<trial>/verify/   the verifier's work folder
    ~/.verimend/runs/<batch>/<fault>/<trial>/grade/    the grader's builds

The agent works in fault/project and fault/db only, exactly as `verimend fix` works in
its sandbox, with the same task prompt. The faulted copy, the pristine database and
bench/faults/ (checks and references) are never inside its project.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from pathlib import Path
from typing import Any

from verifier.project import read_profile, write_profiles

from bench.agents import get_agent
from bench.assemble import AgentRecord, write_batch
from bench.claims import Claim, parse_category, parse_verdict
from bench.claude_stream import parse_stream
from bench.faults import container
from bench.faults.grade import grade_fault
from bench.faults.lib import FAULTS_DIR, Expected, materialize
from bench.faults.reference import apply_reference
from bench.grade import Grade
from bench.outcomes import Expected as OutcomeExpected
from verimend.config import Settings
from verimend.hooks.caps import BatchBudget, RunCaps
from verimend.tools.verify import WorkspaceVerifier
from verimend.tools.workspace import LocalWorkspace

META_KEYS = ("batch", "set", "source", "source_commit", "git_sha", "git_dirty", "model",
             "trials", "max_usd", "per_run_usd", "max_steps", "task_ids", "max_attempts",
             "with_verifier", "seed")  # fmt: skip


def workspace_for(built_root: Path, project: Path, db: Path) -> LocalWorkspace:
    profiles = write_profiles(read_profile(project, project, None), db, built_root / "profiles")
    return LocalWorkspace(project, db, {"DBT_PROFILES_DIR": str(profiles)})


def run_trial(
    batch: dict[str, Any], settings: Settings, fault_id: str, trial: int, cap: float
) -> tuple[AgentRecord, Grade]:
    root = Path(batch["stage_dir"]) / fault_id / str(trial)
    built = materialize(fault_id, root / "fault", batch["seed"])
    if batch["agent"] == "baseline":
        # In the project, where its own profile looks (bench/faults/container.py).
        built.db = built.db.replace(built.project / built.db.name)
    workspace = workspace_for(root, built.project, built.db)
    record = AgentRecord(task=fault_id, trial=trial, started=time.time(), run_cap_usd=cap)
    started = time.monotonic()
    agent = batch["agent"]
    report: dict[str, Any] | None = None
    agent_seconds: float | None = None
    if agent == "verimend":
        from verimend.agent.run import run_agent_sync  # the SDK loads only for VeriMend
        from verimend.cli import DEFAULT_TASK

        run = run_agent_sync(
            DEFAULT_TASK,
            workspace,
            model=batch["model"],
            api_key=settings.require_api_key(),
            caps=RunCaps(
                max_usd=cap, max_steps=batch["max_steps"], max_attempts=batch["max_attempts"]
            ),  # fmt: skip
            cwd=settings.verimend_home / "agent-cwd",
            output_chars=settings.tool_output_chars,
            verifier=WorkspaceVerifier(workspace, (root / "verify").as_posix()),
        )
        record.agent_run = run
        if run.final_verdict:
            report = {**(run.report or {}), "verdict": run.final_verdict}
    else:
        verifier = None
        if batch["with_verifier"]:  # snapshot before the agent acts, check after
            verifier = WorkspaceVerifier(workspace, (root / "verify").as_posix())
            record.verifier = {"snapshot": None, "attempts": [], "error": None}
            try:
                record.verifier["snapshot"] = asyncio.run(verifier.prepare())
            except Exception as error:
                record.verifier["error"] = f"snapshot failed: {type(error).__name__}: {error}"
                verifier = None
        if agent == "reference":
            report = apply_reference(built, FAULTS_DIR / fault_id)
            record.report = report
        elif agent == "baseline":
            from verimend.cli import DEFAULT_TASK

            # In a container that holds only the fault; the verifier stays on this side.
            log, agent_seconds = container.run_baseline(
                built.project,
                root,
                task=DEFAULT_TASK,
                model=batch["model"],
                run_cap_usd=cap,
                max_turns=batch["max_steps"],
                api_key=settings.require_api_key(),
            )
            record.stream_log = log
            report = baseline_report(parse_stream(log).final_text)
        # As in VeriMend's loop, the reference's escalation is not a fix and skips the
        # verifier. The baseline is only watched, so what it left is always checked.
        escalated = agent == "reference" and bool(report and report.get("verdict") == "escalate")
        if verifier is not None and record.verifier is not None and not escalated:
            try:
                record.verifier["attempts"] = [{**asyncio.run(verifier.check()), "attempt": 1}]
            except Exception as error:
                record.verifier["error"] = f"check failed: {type(error).__name__}: {error}"
    record.seconds = round(time.monotonic() - started, 1)
    if agent == "baseline":  # the agent's own time, without the watching verifier
        record.seconds = agent_seconds
    graded = grade_fault(
        built.expected,
        faulted=built.faulted,
        pristine=built.pristine,
        final_project=built.project,
        report=report,
        work=root / "grade",
        key_dir=Path(batch["key_dir"]) if batch.get("key_dir") else None,
    )
    return record, graded.as_grade(fault_id, trial)


def baseline_report(final_text: str | None) -> dict[str, Any] | None:
    """What the grader needs from the baseline's reply: its verdict line and its category
    line. None when it gave no readable verdict."""
    claim = parse_verdict(final_text)
    if claim is Claim.NONE:
        return None
    return {"verdict": str(claim), "root_cause": {"category": parse_category(final_text)}}


def run_batch(batch: dict[str, Any], settings: Settings) -> dict[str, Any]:
    spec = get_agent(batch["agent"])
    if spec.name == "baseline":
        container.ensure_image()
    budget = BatchBudget(max_usd=batch["max_usd"])
    records: list[AgentRecord] = []
    grades: list[Grade] = []
    for fault_id in batch["task_ids"]:
        for trial in range(1, batch["trials"] + 1):
            cap = budget.next_run_cap(batch["per_run_usd"]) if spec.model_calls else 0.0
            if cap is None:
                records.append(AgentRecord(fault_id, trial, time.time(), stopped="batch_cap"))
                print(f"{fault_id} trial {trial}: not started, batch cap reached", flush=True)
                continue
            try:
                record, grade = run_trial(batch, settings, fault_id, trial, cap)
            except Exception:
                print(f"{fault_id} trial {trial}: harness error\n{traceback.format_exc()}")
                records.append(AgentRecord(fault_id, trial, time.time(), stopped="harness_error",
                                           run_cap_usd=cap))  # fmt: skip
                if spec.model_calls:
                    budget.record(cap)  # never assume a failed run was cheaper than its cap
                continue
            records.append(record)
            grades.append(grade)
            if spec.model_calls:
                cost = record.agent_run.cost_usd if record.agent_run else None
                if record.stream_log is not None:
                    cost = parse_stream(record.stream_log).cost_usd
                budget.record(cost if cost is not None else cap)
    meta = {key: batch[key] for key in META_KEYS}
    meta["grader"] = "faults"
    meta["spent_usd"] = budget.spent_usd
    expected: dict[str, OutcomeExpected] = {
        f: Expected.load(FAULTS_DIR / f).outcome for f in batch["task_ids"]
    }
    return write_batch(
        Path(batch["results_dir"]),
        records,
        grades,
        spec,
        meta,
        expected=[(t, n) for t in batch["task_ids"] for n in range(1, batch["trials"] + 1)],
        include_tests=True,
        expected_outcomes=expected,
    )
