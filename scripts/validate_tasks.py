"""Check every candidate task before the split: the reference solution must pass and the
do-nothing agent must fail. No model calls.

    uv run python scripts/validate_tasks.py --source ade-bench --limit 30 --max-usd 0.01

Runs before scripts/split_bench.py, straight from the benchmark's checkout, so it prints
task IDs and pass/fail only and keeps traces outside the repo: some of these tasks will be
held out. Writes bench/results/validate-<date>/validation.json, which the split reads.
"""

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from bench.tasksets import get_source, vendor_dir
from verimend.config import REPO_ROOT, load_settings

# ADE-bench marks fix-style tasks with this tag (docs/spikes.md, "Task counts").
FIX_TAG = "debugging"


def ade_candidates(tasks_dir: Path, pinned: list[str]) -> list[str]:
    """Ready tasks tagged as debugging, plus pinned ones. Reads status and tags only."""
    found: list[str] = []
    for task_yaml in sorted(tasks_dir.glob("*/task.yaml")):
        data = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
        tags = {str(t).lower() for t in data.get("tags") or []}
        if data.get("status") == "ready" and FIX_TAG in tags:
            found.append(task_yaml.parent.name)
    return sorted(set(found) | set(pinned))


def decide(reference_passed: bool | None, none_passed: bool | None) -> tuple[bool, str | None]:
    """Keep a task only if the reference passes and doing nothing fails."""
    if reference_passed is None:
        return False, "missing result"
    if not reference_passed:
        return False, "reference solution fails"
    if none_passed is None:
        return False, "missing result"
    if none_passed:
        return False, "passes without any change"
    return True, None


def run_batch(agent: str, batch: str, tasks: list[str], source: str) -> Path:
    command = [
        sys.executable, "-m", "bench.run", "--agent", agent, "--set", "vendor",
        "--source", source, "--tasks", ",".join(tasks), "--limit", str(len(tasks)),
        "--max-usd", "0.01", "--batch", batch,
    ]  # fmt: skip
    subprocess.run(command, cwd=REPO_ROOT, check=True)
    return load_settings().verimend_home / "runs" / batch / "results" / "summary.json"


def passed_by_task(summary: dict[str, Any]) -> dict[str, bool]:
    return {r["task"]: bool(r["grader_passed"]) for r in summary["runs"]}


def build_validation(
    source: str, commit: str, tasks: list[str], reference: dict[str, bool], none: dict[str, bool]
) -> dict[str, Any]:
    candidates = []
    for task in tasks:
        included, reason = decide(reference.get(task), none.get(task))
        candidates.append(
            {
                "task_id": task,
                "reference": reference.get(task),
                "none": none.get(task),
                "included": included,
                "excluded_reason": reason,
            }
        )
    return {"sources": {source: {"commit": commit, "candidates": candidates}}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check candidate tasks before the split.")
    parser.add_argument("--source", default="ade-bench")
    parser.add_argument("--pinned", type=Path, default=REPO_ROOT / "bench" / "pinned_dev.json")
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--max-usd", type=float, required=True)
    args = parser.parse_args(argv)
    if args.source != "ade-bench":
        print("validate_tasks: only ade-bench is wired up", file=sys.stderr)
        return 2

    source = get_source(args.source)
    pinned = json.loads(args.pinned.read_text(encoding="utf-8")).get(source.name, [])
    checkout = vendor_dir(source, load_settings().verimend_home)
    tasks = ade_candidates(checkout / source.tasks_subdir, pinned)[: args.limit]
    print(f"{len(tasks)} candidates: {', '.join(tasks)}", flush=True)

    stamp = date.today().isoformat()
    reference = passed_by_task(
        json.loads(run_batch("reference", f"validate-reference-{stamp}", tasks, source.name)
                   .read_text(encoding="utf-8"))
    )  # fmt: skip
    none = passed_by_task(
        json.loads(run_batch("none", f"validate-none-{stamp}", tasks, source.name)
                   .read_text(encoding="utf-8"))
    )  # fmt: skip
    validation = build_validation(source.name, source.commit, tasks, reference, none)

    out = REPO_ROOT / "bench" / "results" / f"validate-{stamp}" / "validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(validation, indent=2) + "\n", encoding="utf-8")
    for c in validation["sources"][source.name]["candidates"]:
        ref = {True: "pass", False: "fail", None: "missing"}[c["reference"]]
        nothing = {True: "pass", False: "fail", None: "missing"}[c["none"]]
        note = "" if c["included"] else f"  EXCLUDED: {c['excluded_reason']}"
        print(f"{c['task_id']:28} reference={ref:7} none={nothing:7}{note}")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
