"""`python -m verifier snapshot|verify`. Prints one JSON object on stdout.

Exit codes: 0 passed (or snapshot taken), 1 a check failed, 2 the verifier could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from verifier.patch import DiffError
from verifier.policy import NAMES, Policy
from verifier.project import VerifierError, default_dbt
from verifier.render import render
from verifier.run import diff_from_project, snapshot, verify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verifier", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="copy and build the project before any change")
    snap.add_argument("--project-dir", type=Path, required=True)
    snap.add_argument(
        "--db", type=Path, help="the project's DuckDB file (default: its profile's path)"
    )
    snap.add_argument("--work", type=Path, required=True, help="the verifier's own folder")
    snap.add_argument("--profiles-dir", type=Path)
    snap.add_argument("--target")

    ver = sub.add_parser("verify", help="check a diff against the snapshot")
    ver.add_argument("--work", type=Path, required=True)
    source = ver.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff", type=Path, help="a unified diff file")
    source.add_argument("--from-project", type=Path, help="diff this folder against the base")
    ver.add_argument(
        "--replay",
        action="store_true",
        help="re-verify a recorded diff: leave the snapshot as it was, measure every check",
    )
    ver.add_argument(
        "--policy",
        choices=NAMES,
        default="fix",
        help="fix: an agent's fix of a failed run (default); pr: any pull request",
    )
    ver.add_argument(
        "--allow-test-changes",
        action="store_true",
        help="pr policy only: a maintainer allowed this pull request to change tests",
    )

    for p in (snap, ver):
        p.add_argument("--dbt", default=None, help="the dbt executable")
        p.add_argument("--timeout", type=float, default=1500.0, help="seconds per dbt call")

    ren = sub.add_parser("render", help="a verify result as Markdown, for a pull request")
    ren.add_argument("--result", type=Path, required=True, help="the JSON verify printed")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "render":
        try:
            raw = args.result.read_text(encoding="utf-8").strip().splitlines()
            result = json.loads(raw[-1]) if raw else {"error": "the result file is empty"}
        except (OSError, ValueError) as exc:
            result = {"error": f"cannot read the result: {exc}"}
        sys.stdout.write(render(result))
        return 0
    dbt_exe = args.dbt or default_dbt()
    try:
        if args.command == "snapshot":
            result = snapshot(
                args.project_dir,
                args.db,
                args.work,
                profiles_dir=args.profiles_dir,
                target=args.target,
                dbt_exe=dbt_exe,
                timeout=args.timeout,
            )
            code = 0
        else:
            if args.diff is not None:
                diff_text = args.diff.read_bytes().decode("utf-8")
            else:
                diff_text = diff_from_project(args.work, args.from_project)
            policy = Policy(args.policy, args.allow_test_changes)
            result = verify(
                args.work,
                diff_text,
                dbt_exe=dbt_exe,
                timeout=args.timeout,
                replay=args.replay,
                policy=policy,
            )
            result["diff"] = diff_text
            code = 0 if result["passed"] else 1
    except (VerifierError, DiffError, OSError, UnicodeDecodeError, ValueError) as exc:
        result, code = {"error": f"{type(exc).__name__}: {exc}"}, 2
    sys.stdout.write(json.dumps(result) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
