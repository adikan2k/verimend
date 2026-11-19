"""Run one batch through Harbor, data-eng-bench's own runner. Inside the Linux runner only.

    python -m bench.harbor.inner <batch.json>

Harbor builds each task's container, runs the agent and scores the trial with the task's
hidden pytest verifier, unchanged. Only Harbor's own `nop` (does nothing) and `oracle`
(the task's reference solution) agents are wired up; the baseline and VeriMend on Harbor
come in phase 3. `auto_mounting=true` is needed because these tasks' compose files still
mount Harbor's log folders through the variables older Harbor versions always set.
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

HARBOR_AGENTS = {"none": "nop", "reference": "oracle"}
BASE_IMAGE = "ghcr.io/snowflake-labs/data-eng-bench-base:1.0.0"


def _seconds(span: dict[str, Any] | None) -> float | None:
    if not span or not span.get("started_at") or not span.get("finished_at"):
        return None
    start = datetime.fromisoformat(str(span["started_at"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(span["finished_at"]).replace("Z", "+00:00"))
    return round((end - start).total_seconds(), 1)


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
        records.append(AgentRecord(task, number, started=epoch, seconds=_seconds(execution)))
    return records


def harbor_config(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": batch["batch"],
        "n_attempts": batch["trials"],
        "n_concurrent_trials": 1,
        "environment": {"type": "docker", "env": ["DB_TYPE=duckdb"]},
        "agents": [{"name": HARBOR_AGENTS[batch["agent"]]}],
    }


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
    config.write_text(json.dumps(harbor_config(batch), indent=2) + "\n", encoding="utf-8")
    jobs = stage / "jobs"
    command = ["harbor", "run", "--config", str(config), "--path", str(stage / "tasks")]
    for task in batch["task_ids"]:
        command += ["-i", task]
    command += ["--jobs-dir", str(jobs), "--ek", "auto_mounting=true"]
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode:
        print(done.stdout[-3000:] + done.stderr[-3000:], file=sys.stderr)

    trials = [json.loads(p.read_text(encoding="utf-8")) for p in jobs.glob("*/*/result.json")]
    meta = {key: batch[key] for key in ("batch", "set", "source", "source_commit", "git_sha",
                                        "git_dirty", "model", "trials", "max_usd",
                                        "per_run_usd", "max_steps", "task_ids")}  # fmt: skip
    meta["grader"] = "data-eng-bench"
    meta["spent_usd"] = 0.0
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
        print(f"{run['task']:48} trial {run['trial']}  grader={grader}  claim={run['claim']}")
    print(f"grader pass rate {summary['metrics']['grader_pass_rate']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
