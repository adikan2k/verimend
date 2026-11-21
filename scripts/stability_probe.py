"""Does an unchanged data-eng-bench base build the same tables every time? No model calls.

    uv run python scripts/stability_probe.py --tasks dbt-fix-cac-payback-waterfall

For each task, in its own Harbor container: the verifier snapshots the base as usual (one
build plus one calibration build), then this probe builds fresh copies of the unchanged
base several more times and compares every model's row count and checksum with the
snapshot's build. Three kinds of extra build:

    plain    the unchanged base, as the snapshot built it
    noop     the base plus one new model nothing depends on (`select 1`), the smallest
             change a fix can make outside every other model's lineage
    threads1 the unchanged base with --threads 1

The output names the models that moved in each build: model IDs, row counts and whether
the contents moved, nothing else from the task. Written for decision 0004 after collateral
rejected two fixes for a model outside their lineage (rpt_rfm_analysis).
"""

# pyright: reportMissingImports=false, reportMissingModuleSource=false

import argparse
import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

PROBE_SCRIPT = r"""
import json, shutil, sys
from pathlib import Path
from verifier import checks
from verifier.project import node_statuses, read_json, table_stats
from verifier.run import _build_copy

work, dbt_exe, plan = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
state = read_json(work / "state.json")
manifest = read_json(work / "base" / "manifest.json")
out = {"threads": state["profile"].get("threads"), "calibration": state.get("unstable"),
       "models": len(state["stats"]), "builds": []}
for number, kind in enumerate(plan, 1):
    project, dbt = _build_copy(work, f"probe-{number}", state, dbt_exe, 1500.0)
    args = ["build"]
    if kind == "noop":
        (project / "models" / "zz_probe_noop.sql").write_text("select 1 as x\n", "utf-8")
    if kind == "threads1":
        args += ["--threads", "1"]
    code, _ = dbt.run(project, *args)
    stats = table_stats(project.parent / "db" / state["db_name"], manifest, node_statuses(project))
    moved = checks.unstable_models(state["stats"], stats)
    detail = {uid: [state["stats"][uid], stats.get(uid)] for uid in moved}
    out["builds"].append({"kind": kind, "exit": code, "moved": moved, "detail": detail})
    shutil.rmtree(work / f"probe-{number}", ignore_errors=True)
print(json.dumps(out))
"""

PLAN = ["plain", "plain", "plain", "noop", "noop", "threads1"]
PROBE_FILE = "/tmp/verimend-probe.py"


try:  # only inside the Linux runner, where Harbor is installed
    from harbor.agents.base import BaseAgent
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext

    from bench.harbor.agents import container_verifier
    from bench.harbor.workspace import HarborWorkspace

    class StabilityProbe(BaseAgent):
        def __init__(self, logs_dir: Path, *args: Any, **kwargs: Any) -> None:
            super().__init__(logs_dir, *args, **kwargs)

        @staticmethod
        def name() -> str:
            return "verimend-stability-probe"

        def version(self) -> str:
            return "1"

        async def setup(self, environment: BaseEnvironment) -> None:
            pass

        async def run(
            self, instruction: str, environment: BaseEnvironment, context: AgentContext
        ) -> None:
            started = time.monotonic()
            result: dict[str, Any] = {}
            workspace = await HarborWorkspace.open(environment)
            verifier = container_verifier(workspace)
            try:
                result["snapshot"] = await verifier.prepare()
                await workspace.put_file(PROBE_FILE, PROBE_SCRIPT.encode())
                argv = [*workspace.python, PROBE_FILE, verifier.work_dir, workspace.dbt[0],
                        json.dumps(PLAN)]  # fmt: skip
                done = await workspace.exec(
                    argv, env={"PYTHONPATH": str(verifier.source_dir)}, timeout=5400
                )
                lines = [x for x in done.output.splitlines() if x.startswith("{")]
                result["probe"] = json.loads(lines[-1]) if lines else None
                if not lines:
                    result["error"] = done.output[-1500:]
            except Exception as error:
                result["error"] = f"{type(error).__name__}: {error}"[:1500]
            result["seconds"] = round(time.monotonic() - started, 1)
            (self.logs_dir / "probe.json").write_text(json.dumps(result, indent=1), "utf-8")

except ImportError:
    pass


def main(argv: list[str] | None = None) -> int:
    from bench.run import HARBOR_VENV_PINS, REPO_INSIDE, RUNNER_IMAGE, VENV_VOLUME
    from bench.tasksets import (
        docker_path,
        get_source,
        list_task_ids,
        select_tasks,
        stage_tasks,
        tasks_root,
        vendor_dir,
    )
    from verimend.config import REPO_ROOT, load_settings

    parser = argparse.ArgumentParser(description="Rebuild unchanged bases; no model calls.")
    parser.add_argument("--tasks", required=True, help="comma-separated dev task IDs")
    args = parser.parse_args(argv)
    source = get_source("data-eng-bench")
    home = load_settings().verimend_home
    root = tasks_root("dev", source, home)
    wanted = [t.strip() for t in args.tasks.split(",")]
    task_ids = select_tasks(list_task_ids(root, source), wanted, len(wanted))
    name = f"probe-stability-{date.today():%Y-%m-%d}"
    stage = home / "runs" / name
    stage_tasks(root, task_ids, stage)
    config = {
        "name": name,
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "environment": {"type": "docker", "env": ["DB_TYPE=duckdb"]},
        "agents": [
            {
                "import_path": "scripts.stability_probe:StabilityProbe",
                "override_timeout_sec": 7200.0,
            }  # fmt: skip
        ],
    }
    (stage / "harbor.json").write_text(json.dumps(config, indent=2), "utf-8")
    harbor = ["harbor", "run", "--config", f"'{docker_path(stage / 'harbor.json')}'",
              "--path", f"'{docker_path(stage / 'tasks')}'"]  # fmt: skip
    for task in task_ids:
        harbor += ["-i", task]
    harbor += ["--jobs-dir", f"'{docker_path(stage / 'jobs')}'", "--ek", "auto_mounting=true"]
    pins = " ".join(f"'{pin}'" for pin in HARBOR_VENV_PINS)
    script = "\n".join([
        "set -e",
        f"cd '{docker_path(vendor_dir(source, home))}'",
        "uv venv /opt/venv --allow-existing -q",
        f"uv pip install -q {pins}",
        f"PYTHONPATH={REPO_INSIDE}:{REPO_INSIDE}/verifier/src " + " ".join(harbor) + " > /dev/null",
    ])  # fmt: skip
    volume = f"{VENV_VOLUME}-{source.runner}"
    command = ["docker", "run", "--rm", "-v", "/var/run/docker.sock:/var/run/docker.sock",
               "-v", f"{home}:{docker_path(home)}", "-v", f"{REPO_ROOT}:{REPO_INSIDE}",
               "-v", f"{volume}:/opt/venv", RUNNER_IMAGE, "bash", "-c", script]  # fmt: skip
    subprocess.run(command)

    found = {}
    for path in sorted((stage / "jobs").glob("*/*/agent/probe.json")):
        task = path.parent.parent.name.rsplit("__", 1)[0]
        found[task] = json.loads(path.read_text(encoding="utf-8"))
    out = REPO_ROOT / "bench" / "results" / name / "probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"plan": PLAN, "tasks": found}, indent=1) + "\n", "utf-8")
    for task, result in found.items():
        probe = result.get("probe") or {}
        print(f"{task}: calibration {probe.get('calibration')} error {result.get('error')}")
        for build in probe.get("builds", []):
            print(f"  {build['kind']:9} exit {build['exit']}  moved {build['moved']}")
    print(f"Wrote {out.relative_to(REPO_ROOT)}")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
