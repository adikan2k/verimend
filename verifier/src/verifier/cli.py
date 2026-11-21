"""`python -m verifier snapshot|verify`. Prints one JSON object on stdout.

Exit codes: 0 passed (or snapshot taken), 1 a check failed, 2 the verifier could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from verifier.patch import DiffError
from verifier.project import VerifierError, default_dbt
from verifier.run import diff_from_project, snapshot, verify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verifier", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="copy and build the project before any change")
    snap.add_argument("--project-dir", type=Path, required=True)
    snap.add_argument("--db", type=Path, required=True, help="the project's DuckDB file")
    snap.add_argument("--work", type=Path, required=True, help="the verifier's own folder")
    snap.add_argument("--profiles-dir", type=Path)
    snap.add_argument("--target")

    ver = sub.add_parser("verify", help="check a diff against the snapshot")
    ver.add_argument("--work", type=Path, required=True)
    source = ver.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff", type=Path, help="a unified diff file")
    source.add_argument("--from-project", type=Path, help="diff this folder against the base")

    for p in (snap, ver):
        p.add_argument("--dbt", default=None, help="the dbt executable")
        p.add_argument("--timeout", type=float, default=1500.0, help="seconds per dbt call")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
            result = verify(args.work, diff_text, dbt_exe=dbt_exe, timeout=args.timeout)
            result["diff"] = diff_text
            code = 0 if result["passed"] else 1
    except (VerifierError, DiffError, OSError, UnicodeDecodeError) as exc:
        result, code = {"error": f"{type(exc).__name__}: {exc}"}, 2
    sys.stdout.write(json.dumps(result) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
