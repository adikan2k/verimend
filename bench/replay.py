"""Re-verify recorded diffs with the verifier as it is now. No model calls.

    uv run python -m bench.replay --source faults --batches <a>,<b> --batch <name>
    uv run python -m bench.replay --source data-eng-bench --batches <a>,<b> --batch <name>

From each named batch, it takes the final diff of every run that claimed "fixed", with the
grader's verdict and the verifier's recorded verdict, and dedupes identical diffs. Then it
snapshots each task once and verifies every diff against that snapshot with
`verify --replay`, which leaves the snapshot as it was and measures every check. Only the
development set and the fault suite: a held-out task is refused.

The fault suite runs on this machine. The public benchmarks run through bench.run with the
`replay` agent, in each task's own container.

bench/results/<name>/ then holds:
    diffs.json           every unique diff and the runs it came from
    replay.jsonl         one line per snapshot and per verified diff
    replay-summary.json  how many recorded verdicts the replay reproduces, and the verifier
                         against the grader, before and with row accounting
    summary.json         public benchmarks only: the harness's record of the tasks, which the
                         replay agent leaves untouched (their grades are do-nothing grades)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bench.replay_task import FIVE, replay_task

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = REPO_ROOT / "bench" / "results"
# The git tree of verifier/src with the four checks, before row accounting (phase 6b's
# head). Only batches recorded with this tree can be expected to reproduce their verdicts.
FOUR_CHECKS_TREE = "e320ebfdbbbe3d2acbfe5f704796e57a1fddb512"


# ---- Collecting the recorded runs -----------------------------------------------------


def normalise(diff: str) -> str:
    return "\n".join(line.rstrip() for line in diff.replace("\r\n", "\n").split("\n")).strip()


def diff_hash(diff: str) -> str:
    return hashlib.sha256(normalise(diff).encode("utf-8")).hexdigest()


def allowed_tasks(source: str) -> set[str]:
    if source == "faults":
        from bench.faults.lib import FAULTS_DIR

        return {p.name for p in FAULTS_DIR.iterdir() if (p / "expected.json").is_file()}
    split = json.loads((REPO_ROOT / "bench" / "split.json").read_text(encoding="utf-8"))
    return set(split["sources"][source]["dev"])


def final_attempt(trace: Path) -> dict[str, Any] | None:
    """The last verified attempt of a run: its diff and the verifier's verdict on it."""
    if not trace.is_file():
        return None
    last = None
    for raw in trace.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw)
        if event.get("event") == "attempt_end" and event.get("diff") is not None:
            last = event
    return last


def verifier_tree(git_sha: str | None) -> str | None:
    if not git_sha:
        return None
    import subprocess

    done = subprocess.run(["git", "rev-parse", f"{git_sha}:verifier/src"], cwd=REPO_ROOT,
                          capture_output=True, text=True)  # fmt: skip
    return done.stdout.strip() if done.returncode == 0 else None


def collect(source: str, batches: Sequence[str], results: Path = RESULTS) -> dict[str, Any]:
    """{task: {hash: {"diff": ..., "occurrences": [...]}}} plus what was left out and why."""
    allowed = allowed_tasks(source)
    tasks: dict[str, dict[str, Any]] = {}
    skipped: Counter[str] = Counter()
    for name in batches:
        summary = json.loads((results / name / "summary.json").read_text(encoding="utf-8"))
        if summary.get("source") != source:
            raise ValueError(f"{name} is a {summary.get('source')} batch, not {source}")
        agent = summary.get("agent")
        agent = agent.get("name") if isinstance(agent, dict) else agent
        current = verifier_tree(summary.get("git_sha")) == FOUR_CHECKS_TREE
        for run in summary["runs"]:
            if run.get("claim") != "fixed":
                continue
            task = run["task"]
            if task not in allowed:
                raise ValueError(f"{task} is not in the development set or the fault suite")
            attempt = final_attempt(results / name / task / f"{run['trial']}.jsonl")
            if attempt is None:
                skipped[f"{name}: no recorded diff"] += 1
                continue
            entry = tasks.setdefault(task, {}).setdefault(
                diff_hash(attempt["diff"]), {"diff": attempt["diff"], "occurrences": []}
            )
            entry["occurrences"].append({
                "batch": name, "task": task, "trial": run["trial"], "agent": agent,
                "grader_passed": run.get("grader_passed"), "outcome": run.get("outcome"),
                "recorded_passed": attempt.get("passed"),
                "recorded_failed_check": attempt.get("failed_check"),
                "recorded_with_four_checks": current,
            })  # fmt: skip
    return {"source": source, "batches": list(batches), "tasks": tasks, "skipped": dict(skipped)}


def replay_diffs_file(collected: dict[str, Any], tasks: Sequence[str] | None) -> dict[str, Any]:
    chosen = tasks or sorted(collected["tasks"])
    return {
        task: [{"hash": h, "diff": e["diff"]} for h, e in sorted(collected["tasks"][task].items())]
        for task in chosen
        if task in collected["tasks"]
    }


# ---- Summarising --------------------------------------------------------------------


def _first_failure(checks: list[dict[str, Any]], without: str | None) -> str | None:
    return next((c["name"] for c in checks if not c["passed"] and c["name"] != without), None)


def _gate(rows: list[tuple[bool, bool]]) -> dict[str, int]:
    """(grader passed, verifier passed) pairs as the 2x2 of decision 0007."""
    return {
        "right_let_through": sum(1 for g, v in rows if g and v),
        "right_blocked": sum(1 for g, v in rows if g and not v),
        "wrong_let_through": sum(1 for g, v in rows if not g and v),
        "wrong_stopped": sum(1 for g, v in rows if not g and not v),
    }


def summarise(collected: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    by_hash = {(ln["task"], ln["hash"]): ln for ln in lines if "hash" in ln}
    snapshots = {ln["task"]: ln for ln in lines if "hash" not in ln}
    reproduced = differs = missing = 0
    v1: list[tuple[bool, bool]] = []
    v2: list[tuple[bool, bool]] = []
    newly: list[dict[str, Any]] = []
    not_reproduced: list[dict[str, Any]] = []
    not_analysed = 0
    per_diff = []
    for task, entries in sorted(collected["tasks"].items()):
        for digest, entry in sorted(entries.items()):
            line = by_hash.get((task, digest))
            result = (line or {}).get("result")
            if result is None:
                missing += len(entry["occurrences"])
                continue
            checks = result["checks"]
            before = _first_failure(checks, FIVE)
            five = next((c for c in checks if c["name"] == FIVE), None)
            stopped_by_five = five is not None and not five["passed"]
            if five is not None and "not analysed" in five["detail"]:
                not_analysed += 1
            per_diff.append({"task": task, "hash": digest, "first_failure": before,
                             "row_accounting": None if five is None else five["passed"],
                             "detail": None if five is None else five["detail"],
                             "occurrences": entry["occurrences"]})  # fmt: skip
            for occ in entry["occurrences"]:
                if occ.get("recorded_with_four_checks"):
                    same = (occ["recorded_failed_check"] or None) == before
                    reproduced += same
                    differs += not same
                    if not same:
                        not_reproduced.append({**occ, "hash": digest, "replayed": before})
                grader = bool(occ["grader_passed"])
                v1.append((grader, before is None))
                v2.append((grader, before is None and not stopped_by_five))
                if before is None and stopped_by_five:
                    newly.append({**occ, "hash": digest, "detail": five["detail"] if five else ""})
    total = reproduced + differs
    occurrences = sum(
        len(e["occurrences"]) for t in collected["tasks"].values() for e in t.values()
    )
    return {
        "source": collected["source"],
        "batches": collected["batches"],
        "skipped": collected["skipped"],
        "unique_diffs": sum(len(e) for e in collected["tasks"].values()),
        "occurrences": occurrences,
        "not_replayed": missing,
        "snapshot_errors": {
            t: s["snapshot_error"] for t, s in snapshots.items() if "snapshot_error" in s
        },  # fmt: skip
        "reproduced": {
            "same": reproduced,
            "different": differs,
            "share_different": round(differs / total, 3) if total else None,
            "note": "only runs recorded with the four checks as they are now",
            "not_reproduced": not_reproduced,
        },  # fmt: skip
        "diffs_with_something_not_analysed": not_analysed,
        "gate_four_checks": _gate(v1),
        "gate_with_row_accounting": _gate(v2),
        "newly_stopped_by_row_accounting": newly,
        "diffs": per_diff,
    }


def write_lf(path: Path, text: str) -> None:
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw]


# ---- Running -----------------------------------------------------------------------


def run_faults(diffs: dict[str, list[dict[str, str]]], name: str, out: Path) -> None:
    from bench.faults.lib import SUITE_SEED, materialize
    from bench.faults.run import workspace_for
    from verimend.config import load_settings
    from verimend.tools.verify import WorkspaceVerifier

    stage = load_settings().verimend_home / "runs" / name
    for task, entries in diffs.items():
        root = stage / task
        built = materialize(task, root / "fault", SUITE_SEED)
        verifier = WorkspaceVerifier(
            workspace_for(root, built.project, built.db), (root / "verify").as_posix()
        )
        asyncio.run(replay_task(verifier, task, entries, out))


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bench.replay", description="Re-verify recorded diffs; no model calls."
    )
    parser.add_argument("--source", required=True,
                        choices=("faults", "ade-bench", "data-eng-bench"))  # fmt: skip
    parser.add_argument("--batches", required=True, help="comma-separated recorded batches")
    parser.add_argument("--batch", required=True, help="name of this replay's results folder")
    parser.add_argument("--tasks", help="comma-separated task IDs; default: every task found")
    parser.add_argument("--dry-run", action="store_true", help="collect and count, run nothing")
    parser.add_argument("--summarise-only", action="store_true",
                        help="rebuild replay-summary.json from replay.jsonl")  # fmt: skip
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    batches = [b.strip() for b in args.batches.split(",") if b.strip()]
    tasks = [t.strip() for t in args.tasks.split(",")] if args.tasks else None
    try:
        collected = collect(args.source, batches)
    except (ValueError, OSError) as error:
        print(f"bench.replay: {error}", file=sys.stderr)
        return 2
    diffs = replay_diffs_file(collected, tasks)
    count = sum(len(v) for v in diffs.values())
    occurrences = sum(len(e["occurrences"]) for t in diffs for e in collected["tasks"][t].values())
    print(f"{count} unique diff(s) from {occurrences} claimed fix(es) in {len(diffs)} task(s); "
          f"left out: {collected['skipped'] or 'nothing'}. No model calls, $0.")  # fmt: skip
    for task, entries in diffs.items():
        print(f"  {task}: {len(entries)}")
    if args.dry_run:
        return 0
    out_dir = RESULTS / args.batch
    lines_path = out_dir / "replay.jsonl"
    if not args.summarise_only:
        if out_dir.exists():
            print(f"bench.replay: {out_dir} already exists", file=sys.stderr)
            return 2
        out_dir.mkdir(parents=True)
        write_lf(out_dir / "diffs.json", json.dumps(collected, indent=1))
        if args.source == "faults":
            run_faults(diffs, args.batch, lines_path)
        else:
            from bench.run import main as bench_run

            diffs_path = out_dir / "replay-diffs.json"
            write_lf(diffs_path, json.dumps(diffs))
            code = bench_run(["--agent", "replay", "--set", "dev", "--source", args.source,
                              "--tasks", ",".join(diffs), "--limit", str(len(diffs)),
                              "--max-usd", "0.01", "--batch", args.batch,
                              "--replay-diffs", str(diffs_path)])  # fmt: skip
            diffs_path.unlink(missing_ok=True)  # every diff is in diffs.json already
            if code != 0:
                print(f"bench.replay: bench.run exited {code}", file=sys.stderr)
    summary = summarise(collected, read_lines(lines_path))
    write_lf(out_dir / "replay-summary.json", json.dumps(summary, indent=1))
    print(json.dumps({k: summary[k] for k in ("reproduced", "gate_four_checks",
                                              "gate_with_row_accounting")}, indent=1))  # fmt: skip
    print(f"Results: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
