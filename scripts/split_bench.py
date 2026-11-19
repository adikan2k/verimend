"""Split the checked public tasks into bench/dev/ and bench/heldout/. Aditya runs this.

    uv run python scripts/split_bench.py --seed 1119 \
        --validation bench/results/validate-2026-11-19/validation.json

Claude Code writes and tests this script on fixtures only and never runs it, so it never
learns which tasks were held out (SPEC section 6, CLAUDE.md rule 2).

- Candidates are the tasks scripts/validate_tasks.py kept: the reference solution passes
  and the do-nothing agent fails.
- Per source, about 40% go to dev. Tasks in bench/pinned_dev.json go to dev first and
  count toward that 40%: their prompts already reached a model. The rest is shuffled with
  a seed derived from --seed and the source name, so each source splits independently.
- Each task folder is copied from the benchmark's checkout at its pinned commit, with the
  upstream LICENSE (and NOTICE, if any) and a SOURCE.json next to the tasks.
- bench/split.json records the seed, the dev task IDs and the held-out count. It never
  lists held-out IDs, and this script never prints them.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FRACTION = 0.4

# Kept in step with bench/tasksets.py SOURCES; the split must not import the harness so it
# can run on a machine where only the standard library and git are trusted.
SOURCE_REPOS = {
    "ade-bench": "https://github.com/dbt-labs/ade-bench",
    "data-eng-bench": "https://github.com/snowflake-labs/data-eng-bench",
}


class SplitError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSplit:
    source: str
    dev: list[str]
    heldout: list[str]
    excluded: dict[str, str]  # task -> reason


def plan_source(
    source: str,
    included: list[str],
    pinned: list[str],
    seed: int,
    fraction: float = DEFAULT_FRACTION,
    excluded: dict[str, str] | None = None,
) -> SourceSplit:
    """Pure: which included tasks go to dev and which are held out."""
    if not 0 < fraction < 1:
        raise SplitError(f"dev fraction must be between 0 and 1, got {fraction}")
    if len(set(included)) != len(included):
        raise SplitError(f"{source}: duplicate task IDs among the candidates")
    pinned_in = sorted(set(pinned) & set(included))
    n_dev = max(round(fraction * len(included)), len(pinned_in))
    rest = sorted(set(included) - set(pinned_in))
    random.Random(f"{seed}:{source}").shuffle(rest)
    take = n_dev - len(pinned_in)
    return SourceSplit(
        source=source,
        dev=sorted(pinned_in + rest[:take]),
        heldout=sorted(rest[take:]),
        excluded=dict(sorted((excluded or {}).items())),
    )


def plan_split(
    validation: dict[str, Any], pinned: dict[str, list[str]], seed: int, fraction: float
) -> list[SourceSplit]:
    plans: list[SourceSplit] = []
    for source, data in sorted(validation["sources"].items()):
        candidates = data["candidates"]
        included = [c["task_id"] for c in candidates if c["included"]]
        excluded = {c["task_id"]: c["excluded_reason"] for c in candidates if not c["included"]}
        dropped = sorted(set(pinned.get(source, [])) - set(included))
        if dropped:
            print(f"{source}: pinned but not kept by the task check: {', '.join(dropped)}")
        plans.append(
            plan_source(source, included, pinned.get(source, []), seed, fraction, excluded)
        )
    return plans


def head_commit(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def require_empty(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise SplitError(f"{path} already has content; the split runs once.")


def copy_tasks(
    task_ids: list[str], tasks_dir: Path, checkout: Path, dest: Path, meta: dict[str, Any]
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for task_id in task_ids:
        if not (tasks_dir / task_id).is_dir():
            raise SplitError(f"task folder missing in the checkout: {task_id}")
        shutil.copytree(tasks_dir / task_id, dest / task_id)
    for name in ("LICENSE", "NOTICE"):
        if (checkout / name).is_file():
            shutil.copy2(checkout / name, dest / name)
    (dest / "SOURCE.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def apply_split(
    plans: list[SourceSplit],
    validation: dict[str, Any],
    *,
    bench: Path,
    vendor: Path,
    seed: int,
    fraction: float,
    today: date,
) -> dict[str, Any]:
    marker = bench / "split.json"
    if marker.exists():
        raise SplitError(f"{marker} exists; the split runs once.")
    for plan in plans:
        require_empty(bench / "dev" / plan.source)
        require_empty(bench / "heldout" / plan.source)
    record: dict[str, Any] = {
        "seed": seed,
        "dev_fraction": fraction,
        "date": today.isoformat(),
        "sources": {},
    }
    for plan in plans:
        checkout = vendor / plan.source
        commit = validation["sources"][plan.source]["commit"]
        if head_commit(checkout) != commit:
            raise SplitError(f"{checkout} is not at the commit the task check used ({commit}).")
        meta = {"source": plan.source, "repo": SOURCE_REPOS.get(plan.source), "commit": commit,
                "copied_on": today.isoformat()}  # fmt: skip
        tasks_dir = checkout / "tasks"
        copy_tasks(plan.dev, tasks_dir, checkout, bench / "dev" / plan.source, meta)
        copy_tasks(plan.heldout, tasks_dir, checkout, bench / "heldout" / plan.source, meta)
        record["sources"][plan.source] = {
            "commit": commit,
            "dev": plan.dev,
            "heldout_count": len(plan.heldout),
            "excluded": plan.excluded,
        }
    marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    env = dict(os.environ) if env is None else env
    if env.get("CLAUDECODE"):
        print(
            "split_bench: Aditya runs the split from his own terminal, never from inside "
            "Claude Code (CLAUDECODE is set).",
            file=sys.stderr,
        )
        return 2
    parser = argparse.ArgumentParser(description="Split checked tasks into dev and held-out.")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--pinned", type=Path, default=REPO / "bench" / "pinned_dev.json")
    parser.add_argument("--fraction", type=float, default=DEFAULT_FRACTION)
    parser.add_argument("--bench", type=Path, default=REPO / "bench")
    parser.add_argument("--vendor", type=Path, default=Path.home() / ".verimend" / "vendor")
    args = parser.parse_args(argv)
    try:
        validation = json.loads(args.validation.read_text(encoding="utf-8"))
        pinned = json.loads(args.pinned.read_text(encoding="utf-8"))
        plans = plan_split(validation, pinned, args.seed, args.fraction)
        record = apply_split(
            plans,
            validation,
            bench=args.bench,
            vendor=args.vendor,
            seed=args.seed,
            fraction=args.fraction,
            today=date.today(),
        )
    except (OSError, KeyError, SplitError, subprocess.CalledProcessError) as error:
        print(f"split_bench: {error}", file=sys.stderr)
        return 2
    for source, data in record["sources"].items():
        print(
            f"{source}: {len(data['dev'])} dev, {data['heldout_count']} held out, "
            f"{len(data['excluded'])} excluded by the task check"
        )
        print(f"  dev: {', '.join(data['dev'])}")
    print(f"Wrote {args.bench / 'split.json'}. Review, then commit bench/ yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
