"""Run an agent on the real-world cases (bench/realworld/, decision 0011): build, run,
grade, record. Called by `python -m bench.run --source realworld --set all ...` once the
caps are checked. For each case and trial:

    ~/.verimend/runs/<batch>/<case>/<trial>/case/      the built case (lib.materialize):
                                                       project/, db/, pristine/, broken/,
                                                       key/ and profiles/
    ~/.verimend/runs/<batch>/<case>/<trial>/verify/    the verifier's work folder
    ~/.verimend/runs/<batch>/<case>/<trial>/grade/     the grader's build of the result
    ~/.verimend/runs/<batch>/<case>/key/               the human fix, built twice, once

The agent works in case/project and its database only, with the task VeriMend gets on the
fault suite and nothing else: no description of the failure, no hint from the PR. The
human fix (key/), the untouched data (pristine/) and bench/realworld/ are never inside its
project, and the baseline's container holds the project alone.

One batch holds one kind of case, bug fixes or DuckDB ports, and says which in its
summary: the two are reported as separate rows and never pooled (decision 0011).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import traceback
from pathlib import Path
from typing import Any

from verifier.patch import apply_diff, parse_diff

from bench.agents import get_agent
from bench.assemble import AgentRecord, write_batch
from bench.claims import Claim, parse_verdict
from bench.claude_stream import parse_stream
from bench.faults import container
from bench.faults.dbt import run_dbt
from bench.grade import Grade
from bench.outcomes import Expected as OutcomeExpected
from bench.realworld.grade import AnswerKey, answer_key, grade_case, recheck
from bench.realworld.lib import CASES_DIR, DB_NAME, Built, Case, materialize, write_profile
from verimend.config import Settings
from verimend.hooks.caps import BatchBudget, RunCaps
from verimend.tools.verify import WorkspaceVerifier
from verimend.tools.workspace import LocalWorkspace

META_KEYS = ("batch", "set", "source", "source_commit", "git_sha", "git_dirty", "model",
             "trials", "max_usd", "per_run_usd", "max_steps", "task_ids", "max_attempts",
             "with_verifier")  # fmt: skip
GROUPS = {"bug_fix": "bug fixes", "duckdb_port": "DuckDB ports"}


class MixedKinds(ValueError):
    """A batch named cases of both kinds; they are reported apart, so run them apart."""


def group_of(task_ids: list[str], cases_dir: Path = CASES_DIR) -> str:
    """The one kind of every case in a batch, as its row label. Refuses a mix."""
    kinds = {Case.load(cases_dir / t).kind for t in task_ids}
    if len(kinds) != 1:
        raise MixedKinds(
            "bench.run: a real-world batch holds bug fixes or DuckDB ports, never both "
            f"(got {', '.join(sorted(kinds))}); run each kind with its own --tasks list"
        )
    return GROUPS[kinds.pop()]


def prepared_for(case: Case) -> Any:
    from bench.realworld.__main__ import prepared_for as cached

    return cached(case)


def install_packages(project: Path, profiles: Path) -> None:
    """dbt deps, then every installed package as a real folder. dbt links local packages
    with an absolute symlink, which would not resolve in the baseline's container."""
    deps = run_dbt(project, profiles, "deps")
    if deps.code != 0:
        raise RuntimeError(f"dbt deps failed in {project}: {deps.output[-600:]}")
    installed = project / "dbt_packages"
    for entry in sorted(installed.iterdir()) if installed.is_dir() else []:
        if entry.is_symlink():
            target = entry.resolve()
            entry.unlink()
            shutil.copytree(target, entry)


def profile_name(built: Built) -> str:
    text = (built.profiles / "profiles.yml").read_text(encoding="utf-8")
    return text.split(":", 1)[0].strip()


def build_case(root: Path, case: Case, agent: str) -> Built:
    built = materialize(case, prepared_for(case), root / "case")
    if agent == "baseline":
        # In the project, where the container's profile looks (bench/faults/container.py).
        built.db = built.db.replace(built.project / DB_NAME)
        built.profiles = write_profile(profile_name(built), built.db, root / "case" / "profiles")
    install_packages(built.project, built.profiles)
    return built


def reference_report(built: Built, cases_dir: Path = CASES_DIR) -> dict[str, Any]:
    fix = (cases_dir / built.case.case_id / "reference" / "fix.diff").read_text(encoding="utf-8")
    apply_diff(built.project, parse_diff(fix))
    return {"verdict": "fixed"}


def baseline_report(final_text: str | None) -> dict[str, Any] | None:
    claim = parse_verdict(final_text)
    return None if claim is Claim.NONE else {"verdict": str(claim)}


def run_trial(
    batch: dict[str, Any],
    settings: Settings,
    case_id: str,
    trial: int,
    cap: float,
    keys: dict[str, AnswerKey],
) -> tuple[AgentRecord, Grade]:
    root = Path(batch["stage_dir"]) / case_id / str(trial)
    case = Case.load(CASES_DIR / case_id)
    agent = batch["agent"]
    built = build_case(root, case, agent)
    profile = profile_name(built)
    if case_id not in keys:  # the human fix built twice, once per case per batch
        keys[case_id] = answer_key(built.key, built.pristine, profile,
                                   Path(batch["stage_dir"]) / case_id / "key")  # fmt: skip
    workspace = LocalWorkspace(built.project, built.db, {"DBT_PROFILES_DIR": str(built.profiles)})
    record = AgentRecord(task=case_id, trial=trial, started=time.time(), run_cap_usd=cap)
    started = time.monotonic()
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
            report = reference_report(built)
            record.report = report
        elif agent == "baseline":
            from verimend.cli import DEFAULT_TASK

            log, agent_seconds = container.run_baseline(
                built.project,
                root,
                task=DEFAULT_TASK,
                model=batch["model"],
                run_cap_usd=cap,
                max_turns=batch["max_steps"],
                api_key=settings.require_api_key(),
                profile=profile,
                schema="main",
                category=False,
            )
            record.stream_log = log
            report = baseline_report(parse_stream(log).final_text)
        if verifier is not None and record.verifier is not None:
            try:
                record.verifier["attempts"] = [{**asyncio.run(verifier.check()), "attempt": 1}]
            except Exception as error:
                record.verifier["error"] = f"check failed: {type(error).__name__}: {error}"
    record.seconds = round(time.monotonic() - started, 1)
    if agent == "baseline":  # the agent's own time, without the watching verifier
        record.seconds = agent_seconds
    # A third build of the human fix, next to the agent's, drops what still moves.
    key = recheck(keys[case_id], built.key, built.pristine, profile, root / "grade-key")
    graded = grade_case(broken=built.broken, pristine=built.pristine, profile=profile,
                        final_project=built.project, key=key, work=root / "grade")  # fmt: skip
    keys[f"{case_id}/{trial}"] = key  # what this trial was graded on (grade-keys.json)
    tests = {rule: "pass" if ok else "fail" for rule, ok in graded.rules.items()}
    grade = Grade(case_id, trial, graded.passed, "realworld", graded.failure_mode, tests,
                  len(graded.rules))  # fmt: skip
    return record, grade


def run_batch(batch: dict[str, Any], settings: Settings) -> dict[str, Any]:
    group = group_of(batch["task_ids"])
    spec = get_agent(batch["agent"])
    if spec.name == "baseline":
        container.ensure_image()
    budget = BatchBudget(max_usd=batch["max_usd"])
    keys: dict[str, AnswerKey] = {}
    records: list[AgentRecord] = []
    grades: list[Grade] = []
    for case_id in batch["task_ids"]:
        for trial in range(1, batch["trials"] + 1):
            cap = budget.next_run_cap(batch["per_run_usd"]) if spec.model_calls else 0.0
            if cap is None:
                records.append(AgentRecord(case_id, trial, time.time(), stopped="batch_cap"))
                print(f"{case_id} trial {trial}: not started, batch cap reached", flush=True)
                continue
            try:
                record, grade = run_trial(batch, settings, case_id, trial, cap, keys)
            except Exception:
                print(f"{case_id} trial {trial}: harness error\n{traceback.format_exc()}")
                records.append(AgentRecord(case_id, trial, time.time(), stopped="harness_error",
                                           run_cap_usd=cap))  # fmt: skip
                if spec.model_calls:
                    budget.record(cap)  # never assume a failed run was cheaper than its cap
                continue
            records.append(record)
            grades.append(grade)
            print(f"{case_id} trial {trial}: grader={'pass' if grade.passed else 'fail'} "
                  f"({grade.failure_mode})", flush=True)  # fmt: skip
            if spec.model_calls:
                cost = record.agent_run.cost_usd if record.agent_run else None
                if record.stream_log is not None:
                    cost = parse_stream(record.stream_log).cost_usd
                budget.record(cost if cost is not None else cap)
    meta = {key: batch[key] for key in META_KEYS}
    meta.update(grader="realworld", group=group, spent_usd=budget.spent_usd)
    expected: dict[str, OutcomeExpected] = {t: "fix" for t in batch["task_ids"]}
    summary = write_batch(
        Path(batch["results_dir"]),
        records,
        grades,
        spec,
        meta,
        expected=[(t, n) for t in batch["task_ids"] for n in range(1, batch["trials"] + 1)],
        include_tests=True,
        expected_outcomes=expected,
    )
    graded_on = {name: {"key_models": len(k.tables), "unstable": list(k.unstable)}
                 for name, k in sorted(keys.items()) if "/" in name}  # fmt: skip
    out = Path(batch["results_dir"]) / "grade-keys.json"
    out.write_text(json.dumps(graded_on, indent=1) + "\n", encoding="utf-8", newline="\n")
    return summary
