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


def deb_candidates(tasks_dir: Path, pinned: list[str]) -> list[str]:
    """data-eng-bench's fix tasks: their folder names contain "fix" (19 at the pinned
    commit; its README files 16 of them under bug-fixes). Reads folder names only."""
    found = [p.parent.name for p in tasks_dir.glob("*/task.toml") if "fix" in p.parent.name]
    return sorted(set(found) | set(pinned))


CANDIDATES = {"ade-bench": ade_candidates, "data-eng-bench": deb_candidates}


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
    if args.source not in CANDIDATES:
        print(f"validate_tasks: unknown source {args.source}", file=sys.stderr)
        return 2

    source = get_source(args.source)
    pinned = json.loads(args.pinned.read_text(encoding="utf-8")).get(source.name, [])
    checkout = vendor_dir(source, load_settings().verimend_home)
    tasks = CANDIDATES[source.name](checkout / source.tasks_subdir, pinned)[: args.limit]
    print(f"{len(tasks)} candidates: {', '.join(tasks)}", flush=True)

    stamp = date.today().isoformat()
    results = {
        agent: passed_by_task(
            json.loads(
                run_batch(
                    agent, f"validate-{agent}-{source.name}-{stamp}", tasks, source.name
                ).read_text(encoding="utf-8")
            )
        )
        for agent in ("reference", "none")
    }
    validation = build_validation(
        source.name, source.commit, tasks, results["reference"], results["none"]
    )

    # One file per day; each source's check adds its own entry.
    out = REPO_ROOT / "bench" / "results" / f"validate-{stamp}" / "validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    merged = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"sources": {}}
    merged["sources"].update(validation["sources"])
    out.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    for c in validation["sources"][source.name]["candidates"]:
        ref = {True: "pass", False: "fail", None: "missing"}[c["reference"]]
        nothing = {True: "pass", False: "fail", None: "missing"}[c["none"]]
        note = "" if c["included"] else f"  EXCLUDED: {c['excluded_reason']}"
        print(f"{c['task_id']:28} reference={ref:7} none={nothing:7}{note}")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
