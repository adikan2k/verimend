"""Run one batch through Harbor, data-eng-bench's own runner. Inside the Linux runner only.

    python -m bench.harbor.inner <batch.json>

Harbor builds each task's container, runs the agent and scores the trial with the task's
hidden pytest verifier, unchanged. `none` and `reference` are Harbor's own `nop` and
`oracle`; `verimend` and `baseline` are ours (bench/harbor/agents.py), loaded by import
path, and each leaves a record per trial that is read back here. `auto_mounting=true` is
needed because these tasks' compose files still mount Harbor's log folders through the
variables older Harbor versions always set.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from bench.agents import get_agent
from bench.assemble import AgentRecord, write_batch
from bench.grade import grades_from_harbor_trials, index_harbor_trials
from bench.harbor.ledger import Ledger
from bench.harbor.records import read_record
from verimend.runrecord import AgentRun

HARBOR_AGENTS = {"none": "nop", "reference": "oracle"}  # Harbor's own
IMPORTED_AGENTS = {  # ours, loaded by Harbor from bench/harbor/agents.py
    "verimend": "bench.harbor.agents:VerimendHarborAgent",
    "baseline": "bench.harbor.agents:BaselineHarborAgent",
}
BASE_IMAGE = "ghcr.io/snowflake-labs/data-eng-bench-base:1.0.0"


def _seconds(span: dict[str, Any] | None) -> float | None:
    if not span or not span.get("started_at") or not span.get("finished_at"):
        return None
    start = datetime.fromisoformat(str(span["started_at"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(span["finished_at"]).replace("Z", "+00:00"))
    return round((end - start).total_seconds(), 1)


TRIAL_DIR = "_trial_dir"  # added when result.json is loaded: where the trial's files are


def records_from_trials(trials: list[dict[str, Any]]) -> list[AgentRecord]:
    records: list[AgentRecord] = []
    for task, number, trial in index_harbor_trials(trials):
        execution = trial.get("agent_execution") or {}
        started = execution.get("started_at") or trial.get("started_at")
        epoch = (
            datetime.fromisoformat(str(started).replace("Z", "+00:00")).timestamp()
            if started
            else 0.0
        )
        record = AgentRecord(task, number, started=epoch, seconds=_seconds(execution))
        ours = read_record(Path(trial[TRIAL_DIR])) if trial.get(TRIAL_DIR) else None
        if ours is not None:
            record.run_cap_usd = ours.get("run_cap_usd")
            record.stopped = ours.get("stopped")
            record.stream_log = ours.get("stream_log")
            if ours.get("agent_run") is not None:
                record.agent_run = AgentRun.from_dict(ours["agent_run"])
            if ours.get("seconds") is not None:
                record.seconds = ours["seconds"]
        records.append(record)
    return records


def harbor_config(batch: dict[str, Any], batch_file: str = "") -> dict[str, Any]:
    agent = batch["agent"]
    if agent in IMPORTED_AGENTS:
        entry: dict[str, Any] = {
            "import_path": IMPORTED_AGENTS[agent],
            "kwargs": {"batch_file": batch_file},
            "override_timeout_sec": float(batch.get("agent_timeout_sec", 1200)),
        }
    else:
        entry = {"name": HARBOR_AGENTS[agent]}
    return {
        "name": batch["batch"],
        "n_attempts": batch["trials"],
        "n_concurrent_trials": 1,
        "environment": {"type": "docker", "env": ["DB_TYPE=duckdb"]},
        "agents": [entry],
    }


def load_trials(jobs: Path) -> list[dict[str, Any]]:
    trials = []
    for path in sorted(jobs.glob("*/*/result.json")):
        trial = json.loads(path.read_text(encoding="utf-8"))
        trial[TRIAL_DIR] = str(path.parent)
        trials.append(trial)
    return trials


def main(batch_path: Path) -> int:
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    spec = get_agent(batch["agent"])
    stage = Path(batch["stage_dir"])
    if subprocess.run(["docker", "image", "inspect", BASE_IMAGE], capture_output=True).returncode:
        print(
            f"bench.harbor: build {BASE_IMAGE} first (docs/spikes.md, phase 2 addendum).",
            file=sys.stderr,
        )
        return 2
    config = stage / "harbor.json"
    config.write_text(
        json.dumps(harbor_config(batch, str(batch_path)), indent=2) + "\n", encoding="utf-8"
    )
    jobs = stage / "jobs"
    command = ["harbor", "run", "--config", str(config), "--path", str(stage / "tasks")]
    for task in batch["task_ids"]:
        command += ["-i", task]
    command += ["--jobs-dir", str(jobs), "--ek", "auto_mounting=true"]
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode:
        print(done.stdout[-3000:] + done.stderr[-3000:], file=sys.stderr)

    trials = load_trials(jobs)
    meta = {key: batch[key] for key in ("batch", "set", "source", "source_commit", "git_sha",
                                        "git_dirty", "model", "trials", "max_usd",
                                        "per_run_usd", "max_steps", "task_ids")}  # fmt: skip
    meta["grader"] = "data-eng-bench"
    meta["spent_usd"] = Ledger(stage / "ledger.json", batch["max_usd"]).spent_usd()
    summary = write_batch(
        Path(batch["results_dir"]),
        records_from_trials(trials),
        grades_from_harbor_trials(trials),
        spec,
        meta,
        expected=[(t, n) for t in batch["task_ids"] for n in range(1, batch["trials"] + 1)],
        include_tests=batch["include_tests"],
    )
    for run in summary["runs"]:
        grader = "pass" if run["grader_passed"] else "fail"
        print(
            f"{run['task']:48} trial {run['trial']}  grader={grader}  claim={run['claim']}"
            f"  ${run['dollars']:.4f}"
        )
    print(f"grader pass rate {summary['metrics']['grader_pass_rate']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
