"""Build and check the fault suite. No model calls.

    uv run python -m bench.faults build sd01 --dest ~/.verimend/faults/sd01
    uv run python -m bench.faults check [--faults sd01,lg02] [--seed 1119]

`check` proves, for every fault (SPEC section 12, phase 5):
    - two builds from the same seed are identical, and a fault that draws from the
      seeded generator differs under another seed;
    - the faulted project fails `dbt build` on exactly the nodes expected.json lists;
    - doing nothing fails the hidden checks, and the reference passes them.
It writes bench/results/faults-check-<date>/check.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from bench.faults.grade import build_copy, grade_fault
from bench.faults.lib import (
    FAULTS_DIR,
    SUITE_SEED,
    Materialized,
    fault_ids,
    materialize,
    remove_tree,
)
from bench.faults.reference import apply_reference
from verimend.config import REPO_ROOT, load_settings


def check_fault(fault_id: str, seed: int, work: Path, key_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    one = materialize(fault_id, work / "a", seed)
    two = materialize(fault_id, work / "b", seed)
    result: dict[str, Any] = {
        "kind": one.expected.kind,
        "outcome": one.expected.outcome,
        "fingerprint": one.fingerprint[:16],
        "same_seed_same_build": one.fingerprint == two.fingerprint,
        "draws_from_seed": one.rng_used,
        "commits": [line.split(" ", 1)[1] for line in one.commits],
    }
    if one.rng_used:
        other = materialize(fault_id, work / "c", seed + 1)
        result["other_seed_differs"] = other.fingerprint != one.fingerprint
    build, _ = build_copy(one.project, one.pristine, work / "faulted-build")
    result["failing"] = build.failing
    result["fails_as_expected"] = tuple(build.failing) == one.expected.failing

    nothing = grade(one, None, work / "grade-none", key_dir)
    result["none_fails"] = not nothing.passed
    report = apply_reference(two, FAULTS_DIR / fault_id)
    reference = grade(two, report, work / "grade-reference", key_dir)
    result["reference_passes"] = reference.passed
    result["reference_rules"] = {k: v for k, v in reference.rules.items() if not v} or "all pass"
    keys = ("same_seed_same_build", "other_seed_differs", "fails_as_expected", "none_fails",
            "reference_passes")  # fmt: skip
    result["ok"] = all(result.get(k, True) for k in keys)
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def grade(built: Materialized, report: dict[str, Any] | None, work: Path, key_dir: Path):
    return grade_fault(built.expected, faulted=built.faulted, pristine=built.pristine,
                       final_project=built.project, report=report, work=work,
                       key_dir=key_dir)  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="python -m bench.faults", description=__doc__, formatter_class=raw
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build one fault into a folder")
    build.add_argument("fault")
    build.add_argument("--dest", type=Path, required=True)
    build.add_argument("--seed", type=int, default=SUITE_SEED)
    check = sub.add_parser("check", help="check every fault (or --faults a,b)")
    check.add_argument("--faults", help="comma-separated fault IDs")
    check.add_argument("--seed", type=int, default=SUITE_SEED)
    check.add_argument("--out", type=Path, help="default bench/results/faults-check-<date>")
    args = parser.parse_args(argv)

    if args.command == "build":
        built = materialize(args.fault, args.dest.expanduser().resolve(), args.seed)
        print(f"{built.fault_id}: {built.project} (database {built.db})")
        print(f"fingerprint {built.fingerprint[:16]}; commits: {built.commits}")
        return 0

    wanted = [f.strip() for f in args.faults.split(",")] if args.faults else fault_ids()
    home = load_settings().verimend_home / "faults"
    results: dict[str, Any] = {}
    for fault_id in wanted:
        work = home / "check" / fault_id
        results[fault_id] = check_fault(fault_id, args.seed, work, home / "answer-key")
        remove_tree(work)
        r = results[fault_id]
        print(f"{fault_id}  {'ok  ' if r['ok'] else 'FAIL'}  {r['outcome']:8}  fails {r['failing']}"
              f"  reference {r['reference_rules']}  {r['seconds']} s")  # fmt: skip
    out = args.out or REPO_ROOT / "bench" / "results" / f"faults-check-{date.today():%Y-%m-%d}"
    out.mkdir(parents=True, exist_ok=True)
    summary = {"seed": args.seed, "faults": len(results),
               "ok": sum(1 for r in results.values() if r["ok"]), "results": results}  # fmt: skip
    (out / "check.json").write_text(json.dumps(summary, indent=1) + "\n", "utf-8")
    print(f"{summary['ok']} of {summary['faults']} faults ok. Wrote {out / 'check.json'}")
    return 0 if summary["ok"] == summary["faults"] else 1


if __name__ == "__main__":
    sys.exit(main())
