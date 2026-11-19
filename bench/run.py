"""Run any agent against a task set, grade every trial, write traces and summary.json.

    uv run python -m bench.run --agent reference --set dev --limit 10 --max-usd 0.01
    uv run python -m bench.run --agent baseline --set dev --trials 1 --limit 7 --max-usd 1

On the host this only checks the rules, stages the tasks and starts the Linux runner;
inside it, bench/ade/inner.py hands the batch to ADE-bench's own harness. Every batch
needs --limit and --max-usd, free agents included (CLAUDE.md rule 1), and the estimate
is printed before anything starts.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from bench.agents import BASELINE_TOOLS, AgentSpec, get_agent
from bench.claims import BASELINE_PROMPT_SUFFIX
from bench.tasksets import (
    SET_NAMES,
    SPLIT_MARKER,
    Source,
    TaskSetError,
    docker_path,
    get_source,
    list_task_ids,
    require_set_allowed,
    select_tasks,
    stage_tasks,
    tasks_root,
    vendor_dir,
)
from verimend.config import REPO_ROOT, Settings, load_settings
from verimend.hooks.caps import CapError, estimate_batch_usd, require_batch_caps

RUNNER_IMAGE = "verimend-bench-runner"
VENV_VOLUME = "verimend-bench-venv"
HARBOR_VERSION = "0.26.0"  # data-eng-bench's runner; see docs/spikes.md, phase 2 addendum
AGENT_TIMEOUT_SEC = 1200
REPO_INSIDE = "/verimend"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bench.run", description="Run an agent against a task set and grade it."
    )
    parser.add_argument("--agent", required=True)
    parser.add_argument("--set", dest="set_name", required=True, choices=SET_NAMES)
    parser.add_argument("--source", default="ade-bench")
    parser.add_argument("--tasks", help="comma-separated task IDs; default: the whole set")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--limit", type=int, help="most tasks to run (required)")
    parser.add_argument("--max-usd", type=float, help="dollar cap for the batch (required)")
    parser.add_argument("--batch", help="batch name; default: <agent>-<set>-<timestamp>")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    return parser.parse_args(argv)


def plugin_sets_text() -> str:
    """ADE-bench plugin-set config for the baseline. JSON is valid YAML, so no new library.

    Same tools as ADE-bench's own baseline set; the one addition is the verdict line.
    """
    config = {
        "sets": [
            {
                "name": "verimend-baseline",
                "description": "ADE-bench baseline tools plus a verdict line",
                "default": True,
                "skills": [],
                "mcp_servers": {},
                "allowed_tools": list(BASELINE_TOOLS),
                "prompt_suffix": BASELINE_PROMPT_SUFFIX,
            }
        ]
    }
    return json.dumps(config, indent=2) + "\n"


def git_state(repo: Path) -> tuple[str, bool]:
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    return sha, bool(dirty)


def require_source_commit(source: Source, checkout: Path) -> None:
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    if head != source.commit:
        raise TaskSetError(
            f"{source.name} checkout at {checkout} is at {head or 'nothing'}, "
            f"not the pinned commit {source.commit}."
        )


def runner_command(
    *,
    home: Path,
    repo: Path,
    checkout: Path,
    batch_file: Path,
    env: Mapping[str, str],
    runner: str = "ade",
) -> list[str]:
    home_inside = docker_path(home)
    # ADE-bench is installed from its checkout next to verimend. Harbor comes from PyPI at
    # the pinned version and alone: it cannot share a venv with dbt-core 1.10 (pathspec),
    # and bench/harbor/ needs only the standard-library modules of bench/.
    if runner == "ade":
        install = f"-e . -e {REPO_INSIDE}/verifier -e {REPO_INSIDE}"
    else:
        install = f"'harbor=={HARBOR_VERSION}'"
    script = "\n".join(
        [
            "set -e",
            f"cd '{docker_path(checkout)}'",
            "uv venv /opt/venv --allow-existing -q",
            f"uv pip install -q {install}",
            f"PYTHONPATH={REPO_INSIDE} python -m bench.{runner}.inner '{docker_path(batch_file)}'",
        ]
    )
    volume = VENV_VOLUME if runner == "ade" else f"{VENV_VOLUME}-{runner}"
    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{home}:{home_inside}",
        "-v",
        f"{repo}:{REPO_INSIDE}",
        "-v",
        f"{volume}:/opt/venv",
    ]
    for key, value in env.items():
        command += ["-e", f"{key}={value}"]
    return [*command, RUNNER_IMAGE, "bash", "-c", script]


def plan_line(
    spec: AgentSpec, settings: Settings, set_name: str, source: str, tasks: int, trials: int,
    max_usd: float,
) -> str:  # fmt: skip
    runs = tasks * trials
    estimate = estimate_batch_usd(runs, spec.estimated_usd_per_run)
    model = f" on {settings.model_default}" if spec.model_calls else " (no model calls)"
    per_run = min(settings.max_usd_per_run, max_usd)
    return (
        f"{spec.name}{model}: {tasks} tasks x {trials} trials = {runs} runs from "
        f"{set_name}/{source}. Estimated ${estimate:.2f} (${spec.estimated_usd_per_run:.2f} "
        f"a run); batch cap ${max_usd:.2f}; per-run cap ${per_run:.2f}, "
        f"{settings.max_steps} steps."
    )


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    env = os.environ if env is None else env
    try:
        spec = get_agent(args.agent)
        if not spec.available:
            raise TaskSetError(spec.unavailable_reason)
        source = get_source(args.source)
        if spec.name not in source.agents:
            raise TaskSetError(f"{spec.name} is not wired up for {source.name} yet.")
        set_name = require_set_allowed(args.set_name, env=env, split_exists=SPLIT_MARKER.exists())
        limit, max_usd = require_batch_caps(args.limit, args.max_usd)
        if args.trials < 1:
            raise CapError("--trials must be at least 1")
        settings = load_settings()
        if spec.model_calls:
            settings.require_api_key()
        home = settings.verimend_home
        checkout = vendor_dir(source, home)
        root = tasks_root(set_name, source, home)
        requested = [t.strip() for t in args.tasks.split(",")] if args.tasks else None
        task_ids = select_tasks(list_task_ids(root, source), requested, limit)
    except (KeyError, TaskSetError, CapError) as error:
        print(f"bench.run: {error}", file=sys.stderr)
        return 2

    print(plan_line(spec, settings, set_name, source.name, len(task_ids), args.trials, max_usd))
    if args.dry_run:
        if set_name != "heldout":
            print("Tasks: " + ", ".join(task_ids))
        return 0

    try:
        require_source_commit(source, checkout)
    except TaskSetError as error:
        print(f"bench.run: {error}", file=sys.stderr)
        return 2
    sha, dirty = git_state(REPO_ROOT)
    batch = args.batch or f"{spec.name}-{set_name}-{datetime.now():%Y-%m-%d-%H%M%S}"
    stage = home / "runs" / batch
    stage_tasks(root, task_ids, stage)
    if spec.name == "baseline":
        (stage / "experiment_sets").mkdir()
        (stage / "experiment_sets" / "plugin-sets.yaml").write_text(plugin_sets_text(), "utf-8")
    # Runs straight from the vendor checkout happen before the split; their traces stay
    # outside the repo and leave out test names, since some of those tasks become held out.
    in_repo = set_name != "vendor"
    results_host = REPO_ROOT / "bench" / "results" / batch if in_repo else stage / "results"
    results_inside = (
        f"{REPO_INSIDE}/bench/results/{batch}" if in_repo else docker_path(stage / "results")
    )
    payload: dict[str, Any] = {
        "batch": batch,
        "agent": spec.name,
        "set": set_name,
        "source": source.name,
        "source_commit": source.commit,
        "git_sha": sha,
        "git_dirty": dirty,
        "model": settings.model_default if spec.model_calls else None,
        "trials": args.trials,
        "max_usd": max_usd,
        "per_run_usd": min(settings.max_usd_per_run, max_usd),
        "max_steps": settings.max_steps,
        "task_ids": task_ids,
        "stage_dir": docker_path(stage),
        "results_dir": results_inside,
        "include_tests": in_repo,
    }
    batch_file = stage / "batch.json"
    batch_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    subprocess.run(
        ["docker", "build", "-q", "-t", RUNNER_IMAGE, str(REPO_ROOT / "bench" / "runner")],
        check=True,
        capture_output=True,
    )
    command = runner_command(
        home=home,
        repo=REPO_ROOT,
        checkout=checkout,
        batch_file=batch_file,
        env={"DEFAULT_AGENT_TIMEOUT_SEC": str(AGENT_TIMEOUT_SEC)},
        runner=source.runner,
    )
    code = subprocess.run(command).returncode
    print(f"Results: {results_host}")
    return code


if __name__ == "__main__":
    sys.exit(main())
