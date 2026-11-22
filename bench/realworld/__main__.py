"""Build and check the real-world cases. No model calls.

    uv run python -m bench.realworld build hubspot-215 [--dest ~/.verimend/realworld/hubspot-215]
    uv run python -m bench.realworld reference hubspot-215
    uv run python -m bench.realworld check [--cases hubspot-215,zendesk-290]

`build` builds one case into a folder (the first build of a case fills its cache from
the network; later builds are offline). `reference` writes reference/fix.diff from the
PR, limited to the case's files. `check` keeps a case only if:
    - two builds are identical (files, data, commits), and nothing in the project or its
      git log points at the fix;
    - the parent fails `dbt build` on exactly the nodes case.json lists;
    - the human fix (the whole fix commit) builds clean on the same data;
    - doing nothing fails the grade, and the reference passes it.
It writes bench/results/realworld-check-<date>/check.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from verifier.patch import apply_diff, parse_diff

from bench.faults.lib import remove_tree
from bench.realworld.grade import answer_key, build_copy, grade_case
from bench.realworld.lib import (
    CASES_DIR,
    Case,
    CaseError,
    Prepared,
    case_ids,
    git,
    leaks_in,
    materialize,
    prepare,
)
from verimend.config import REPO_ROOT, load_settings


def home() -> Path:
    return load_settings().verimend_home / "realworld"


def prepared_for(case: Case) -> Prepared:
    return prepare(case, home() / "cache" / case.case_id, home() / "repos")


def write_reference(case: Case) -> Path:
    """reference/fix.diff: the PR's diff, limited to the case's files."""
    repo_dir = home() / "repos" / case.repo.split("/")[1]
    prepared_for(case)  # clones the repository if needed
    diff = git(repo_dir, "diff", "--no-color", "--no-ext-diff", case.parent_sha, case.fix_sha,
               "--", *case.files)  # fmt: skip
    if not diff.strip():
        raise CaseError(f"{case.case_id}: the PR does not change {case.files}")
    out = CASES_DIR / case.case_id / "reference" / "fix.diff"
    out.parent.mkdir(exist_ok=True)
    out.write_text(diff, "utf-8", newline="\n")
    return out


def check_case(case: Case, prepared: Prepared, reference: Path, work: Path) -> dict[str, Any]:
    """Check one case from its cache and its reference diff (see the module docstring)."""
    started = time.monotonic()
    profile = str(prepared.info["profile"])
    one = materialize(case, prepared, work / "a")
    two = materialize(case, prepared, work / "b")
    result: dict[str, Any] = {
        "kind": case.kind,
        "pr": case.pr_url,
        "merged_at": case.merged_at,
        "fingerprint": one.fingerprint[:16],
        "same_build_twice": one.fingerprint == two.fingerprint,
        "leaks": leaks_in(one.project, case),
        "packages": prepared.info["packages"],
    }
    parent = build_copy(one.project, one.pristine, profile, work / "parent-build")
    result["failing"] = parent[0].failing
    result["fails_as_expected"] = tuple(parent[0].failing) == case.failing
    try:
        key = answer_key(one.key, one.pristine, profile, work / "key")
        result["fix_passes"] = True
        result["key_models"] = len(key.tables)
        result["unstable_models"] = list(key.unstable)
    except CaseError as exc:
        result["fix_passes"] = False
        result["fix_error"] = str(exc)[-800:]
        key = None
    if key is not None:
        common = {"broken": one.broken, "pristine": one.pristine, "profile": profile, "key": key}
        nothing = grade_case(final_project=one.project, work=work / "grade-none", build=parent,
                             **common)  # fmt: skip
        result["none_fails"] = not nothing.passed
        try:
            apply_diff(two.project, parse_diff(reference.read_text(encoding="utf-8")))
            graded = grade_case(final_project=two.project, work=work / "grade-reference",
                                **common)  # fmt: skip
            result["reference_passes"] = graded.passed
            result["reference_rules"] = {k: v for k, v in graded.rules.items() if not v} or (
                "all pass"
            )
        except Exception as exc:  # a diff that does not apply is a failed reference
            result["reference_passes"] = False
            result["reference_rules"] = f"{type(exc).__name__}: {exc}"[-400:]
    keys = ("same_build_twice", "fails_as_expected", "fix_passes", "none_fails",
            "reference_passes")  # fmt: skip
    result["ok"] = all(result.get(k, False) for k in keys) and not result["leaks"]
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def main(argv: list[str] | None = None) -> int:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="python -m bench.realworld", description=__doc__, formatter_class=raw
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build one case into a folder")
    build.add_argument("case")
    build.add_argument("--dest", type=Path, help="default ~/.verimend/realworld/<case>")
    reference = sub.add_parser("reference", help="write a case's reference/fix.diff")
    reference.add_argument("case")
    check = sub.add_parser("check", help="check every case (or --cases a,b)")
    check.add_argument("--cases", help="comma-separated case IDs")
    check.add_argument("--out", type=Path, help="default bench/results/realworld-check-<date>")
    args = parser.parse_args(argv)

    if args.command == "build":
        case = Case.load(CASES_DIR / args.case)
        dest = (args.dest or home() / case.case_id).expanduser().resolve()
        built = materialize(case, prepared_for(case), dest)
        print(f"{case.case_id}: {built.project} (database {built.db}, profiles {built.profiles})")
        print(f"fingerprint {built.fingerprint[:16]}; commits: {built.commits}")
        return 0
    if args.command == "reference":
        print(f"Wrote {write_reference(Case.load(CASES_DIR / args.case))}")
        return 0

    wanted = [c.strip() for c in args.cases.split(",")] if args.cases else case_ids()
    results: dict[str, Any] = {}
    for case_id in wanted:
        work = home() / "check" / case_id
        try:
            case = Case.load(CASES_DIR / case_id)
            reference = CASES_DIR / case_id / "reference" / "fix.diff"
            results[case_id] = check_case(case, prepared_for(case), reference, work)
        except CaseError as exc:
            results[case_id] = {"ok": False, "error": str(exc)[-800:]}
        remove_tree(work)
        r = results[case_id]
        print(f"{case_id}  {'ok  ' if r['ok'] else 'FAIL'}  fails {r.get('failing')}"
              f"  reference {r.get('reference_rules', r.get('error'))}"
              f"  {r.get('seconds')} s", flush=True)  # fmt: skip
    out = args.out or REPO_ROOT / "bench" / "results" / f"realworld-check-{date.today():%Y-%m-%d}"
    out.mkdir(parents=True, exist_ok=True)
    summary = {"cases": len(results), "ok": sum(1 for r in results.values() if r["ok"]),
               "results": results}  # fmt: skip
    (out / "check.json").write_text(json.dumps(summary, indent=1) + "\n", "utf-8")
    print(f"{summary['ok']} of {summary['cases']} cases ok. Wrote {out / 'check.json'}")
    return 0 if summary["ok"] == summary["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
